# src/main.py
import asyncio
import os
import json
import time
import contextlib
import logging
from typing import Tuple, Dict, List
from dotenv import load_dotenv
import websockets
import lighter
import eth_account

# ---------- config ----------
logging.disable(logging.CRITICAL)
load_dotenv()

BASE_URL = os.getenv("BASE_URL")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY")
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX") or 0)
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
WS_URL = BASE_URL.replace("https", "wss") + "/stream"

MARKET_ID = 0
PRICE_SCALE = 100
BASE_SCALE = 10000
MAX_SLIPPAGE = 0.01  # 1%

MARGIN = 10.0      # margin in USD
LEVERAGE = 20.0      # => exposure = 200 USD

SHORT = True         # is_ask=True  -> sell/short
LONG = False        # is_ask=False -> buy/long
ORDER = SHORT        # toggle

TP_USD = 0.10        # +$0.10 PnL
SL_USD = 0.10        # -$0.10 PnL

# ---------- utils ----------


def next_coi() -> int:
    return int(time.time() * 1000)


def to_int_price(p: float) -> int:
    return int(round(p * PRICE_SCALE))


def from_int_price(pi: int) -> float:
    return pi / PRICE_SCALE


def base_amount_from_notional_usd(notional_usd: float, price: float) -> int:
    size_eth = notional_usd / price
    return max(1, int(round(size_eth * BASE_SCALE)))


def qty_eth_from_base_int(base_amt_int: int) -> float:
    return base_amt_int / BASE_SCALE

# ---------- helpers ----------


async def init_signer() -> Tuple[lighter.SignerClient, lighter.ApiClient, int, lighter.TransactionApi]:
    if not (BASE_URL and ETH_PRIVATE_KEY and API_KEY_PRIVATE_KEY and API_KEY_INDEX):
        raise RuntimeError("Missing keys in .env")
    api_client = lighter.ApiClient(
        configuration=lighter.Configuration(host=BASE_URL))
    l1 = eth_account.Account.from_key(ETH_PRIVATE_KEY).address
    resp = await lighter.AccountApi(api_client).accounts_by_l1_address(l1_address=l1)
    if not resp.sub_accounts:
        raise RuntimeError(f"No sub_accounts for {l1}")
    account_index = resp.sub_accounts[0].index
    signer = lighter.SignerClient(
        url=BASE_URL,
        private_key=API_KEY_PRIVATE_KEY,
        account_index=account_index,
        api_key_index=API_KEY_INDEX,
    )
    tx_api = lighter.TransactionApi(api_client)
    print(
        f"[signer] ready | account_index={account_index} api_key_index={API_KEY_INDEX}")
    return signer, api_client, account_index, tx_api


async def get_mark_price_once(market_id: int) -> float:
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=2)
        await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{market_id}"}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") != "update/market_stats":
                continue
            s = msg.get("market_stats") or {}
            if s.get("market_id") != market_id:
                continue
            mark = s.get("mark_price")
            if mark:
                price = float(mark)
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"type": "unsubscribe", "channel": f"market_stats/{market_id}"}))
                return price


def compute_prices(
    *,
    mark: float,
    order_is_ask: bool,
    base_amt_int: int,
    max_slippage: float,
    tp_usd: float,
    sl_usd: float,
) -> Dict[str, int | bool]:
    """Return entry_price_int, tp_price_int, sl_price_int and sides for TP/SL (is_ask booleans)."""
    mark_int = to_int_price(mark)

    # IOC cap to emulate market
    entry_price_int = int(round(mark_int * (1 + max_slippage))) if not order_is_ask \
        else int(round(mark_int * (1 - max_slippage)))

    qty_eth = qty_eth_from_base_int(base_amt_int)
    if qty_eth <= 0:
        raise RuntimeError(
            "Computed qty is zero; increase exposure or check scales.")

    # PnL targets -> price deltas
    dp_tp = tp_usd / qty_eth
    dp_sl = sl_usd / qty_eth

    if not order_is_ask:  # LONG
        tp_price_int = to_int_price(mark + dp_tp)
        sl_price_int = to_int_price(mark - dp_sl)
        tp_is_ask = sl_is_ask = True   # sell to exit long
    else:                 # SHORT
        tp_price_int = to_int_price(mark - dp_tp)
        sl_price_int = to_int_price(mark + dp_sl)
        tp_is_ask = sl_is_ask = False  # buy to exit short

    return {
        "entry_price_int": entry_price_int,
        "tp_price_int": tp_price_int,
        "sl_price_int": sl_price_int,
        "tp_is_ask": tp_is_ask,
        "sl_is_ask": sl_is_ask,
    }


async def compute_transactions(
    *,
    signer: lighter.SignerClient,
    tx_api: lighter.TransactionApi,
    account_index: int,
    market_id: int,
    order_is_ask: bool,
    base_amt_int: int,
    prices: Dict[str, int | bool],
) -> List[str]:
    """Do nonces & COIs, sign the three orders, and return the signed infos list."""
    # nonces
    next_nonce = await tx_api.next_nonce(account_index=account_index, api_key_index=API_KEY_INDEX)
    nonce = next_nonce.nonce

    # COIs
    coi_base = next_coi()
    coi_entry = coi_base
    coi_tp = coi_base + 1
    coi_sl = coi_base + 2

    # entry
    entry_info, err = signer.sign_create_order(
        market_index=market_id,
        client_order_index=coi_entry,
        base_amount=base_amt_int,
        price=prices["entry_price_int"],
        is_ask=order_is_ask,
        order_type=signer.ORDER_TYPE_LIMIT,
        time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
        reduce_only=False,
        trigger_price=0,
        nonce=nonce
    )
    nonce += 1
    if err:
        raise RuntimeError(f"sign entry error: {err}")

    # tp
    tp_info, err = signer.sign_create_order(
        market_index=market_id,
        client_order_index=coi_tp,
        base_amount=base_amt_int,
        price=prices["tp_price_int"],
        is_ask=prices["tp_is_ask"],  # type: ignore[arg-type]
        order_type=signer.ORDER_TYPE_TAKE_PROFIT_LIMIT,
        time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
        reduce_only=True,
        trigger_price=prices["tp_price_int"],
        nonce=nonce
    )
    nonce += 1
    if err:
        raise RuntimeError(f"sign TP error: {err}")

    # sl
    sl_info, err = signer.sign_create_order(
        market_index=market_id,
        client_order_index=coi_sl,
        base_amount=base_amt_int,
        price=prices["sl_price_int"],
        is_ask=prices["sl_is_ask"],  # type: ignore[arg-type]
        order_type=signer.ORDER_TYPE_STOP_LOSS_LIMIT,
        time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
        reduce_only=True,
        trigger_price=prices["sl_price_int"],
        nonce=nonce
    )
    if err:
        raise RuntimeError(f"sign SL error: {err}")

    return [entry_info, tp_info, sl_info]


async def batch_tx(tx_api: lighter.TransactionApi, infos: List[str]):
    tx_types = json.dumps(
        [lighter.SignerClient.TX_TYPE_CREATE_ORDER] * len(infos))
    tx_infos = json.dumps(infos)
    return await tx_api.send_tx_batch(tx_types=tx_types, tx_infos=tx_infos)

# ---------- main ----------


async def main():
    signer, api_client, account_index, tx_api = await init_signer()
    try:
        # 1) mark + sizing
        mark = await get_mark_price_once(MARKET_ID)
        notional_usd = MARGIN * LEVERAGE
        base_amt_int = base_amount_from_notional_usd(notional_usd, mark)

        # 2) compute prices
        prices = compute_prices(
            mark=mark,
            order_is_ask=ORDER,
            base_amt_int=base_amt_int,
            max_slippage=MAX_SLIPPAGE,
            tp_usd=TP_USD,
            sl_usd=SL_USD,
        )
        print(
            f"[plan] side={'SHORT' if ORDER else 'LONG'} mark≈{mark:.2f} "
            f"entry_cap≈{from_int_price(prices['entry_price_int']):.2f} "
            f"TP≈{from_int_price(prices['tp_price_int']):.2f} "
            f"SL≈{from_int_price(prices['sl_price_int']):.2f} "
            f"base_int={base_amt_int} notional≈${notional_usd:.2f}"
        )

        # 3) sign transactions (entry + TP + SL)
        infos = await compute_transactions(
            signer=signer,
            tx_api=tx_api,
            account_index=account_index,
            market_id=MARKET_ID,
            order_is_ask=ORDER,
            base_amt_int=base_amt_int,
            prices=prices,
        )

        # 4) batch submit
        tx_hashes = await batch_tx(tx_api, infos)
        print(f"[batch] sent OK: {tx_hashes}")

    finally:
        await signer.close()
        await api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
