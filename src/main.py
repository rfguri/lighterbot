# src/main.py
import asyncio
import os
import json
import signal
import logging
import contextlib
import time
from dotenv import load_dotenv
import websockets
import lighter
import eth_account

logging.disable(logging.CRITICAL)
load_dotenv()

BASE_URL = os.getenv("BASE_URL")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY")
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX"))
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
WS_URL = BASE_URL.replace("https", "wss") + "/stream"

MARKET_ID = 0
PRICE_SCALE = 100
BASE_SCALE = 10000
MAX_SLIPPAGE = 0.01  # 1%

MARGIN = 10.0      # margin in USD
LEVERAGE = 20.0    # assumed leverage => exposure = MARGIN * LEVERAGE

SHORT = True      # is_ask=True  -> sell/short
LONG = False     # is_ask=False -> buy/long
ORDER = SHORT     # toggle long/short

TP_USD = 1.00     # +$1 from entry mark
SL_USD = 1.00     # -$1 from entry mark


def next_coi() -> int:
    return int(time.time() * 1000)


def to_int_price(p: float) -> int:
    return int(round(p * PRICE_SCALE))


def from_int_price(pi: int) -> float:
    return pi / PRICE_SCALE


def base_amount_from_notional_usd(notional_usd: float, price: float) -> int:
    size_eth = notional_usd / price
    return max(1, int(round(size_eth * BASE_SCALE)))


async def init_signer():
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
    print(
        f"[signer] ready | account_index={account_index} api_key_index={API_KEY_INDEX}")
    return signer, api_client, account_index


async def get_mark_price_once(market_id: int) -> float:
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=2)  # drain hello if any
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
                    await asyncio.sleep(0.1)
                return price


async def main():
    if not (BASE_URL and ETH_PRIVATE_KEY and API_KEY_PRIVATE_KEY and API_KEY_INDEX):
        raise RuntimeError("Missing keys in .env")

    signer, api_client, account_index = await init_signer()
    tx_api = lighter.TransactionApi(api_client)

    try:
        # --- price + sizing ---
        mark = await get_mark_price_once(MARKET_ID)
        mark_int = to_int_price(mark)
        notional_usd = MARGIN * LEVERAGE                 # $10 × 20x = $200 exposure
        base_amt_int = base_amount_from_notional_usd(notional_usd, mark)

        # Entry IOC limit to emulate market with slippage cap
        entry_price_int = int(round(mark_int * (1 + MAX_SLIPPAGE))
                              ) if ORDER is LONG else int(round(mark_int * (1 - MAX_SLIPPAGE)))

        # TP/SL absolute-dollar offsets from current mark
        if ORDER is LONG:
            tp_price_int = to_int_price(mark + TP_USD)   # profit above
            sl_price_int = to_int_price(mark - SL_USD)   # stop below
            tp_is_ask = sl_is_ask = True                 # sell to exit long
        else:
            tp_price_int = to_int_price(mark - TP_USD)   # profit below
            sl_price_int = to_int_price(mark + SL_USD)   # stop above
            tp_is_ask = sl_is_ask = False                # buy to exit short

        print(f"[plan] side={'LONG' if ORDER is LONG else 'SHORT'} "
              f"mark≈{mark:.2f} entry_cap≈{from_int_price(entry_price_int):.2f} "
              f"TP≈{from_int_price(tp_price_int):.2f} SL≈{from_int_price(sl_price_int):.2f} "
              f"base_int={base_amt_int} notional≈${notional_usd:.2f}")

        # --- nonces for batch ---
        next_nonce = await tx_api.next_nonce(account_index=account_index, api_key_index=API_KEY_INDEX)
        nonce = next_nonce.nonce

        # --- COIs grouped (unique) ---
        coi_base = next_coi()
        coi_entry = coi_base
        coi_tp = coi_base + 1
        coi_sl = coi_base + 2

        # --- sign entry (IOC LIMIT) ---
        entry_info, err = signer.sign_create_order(
            market_index=MARKET_ID,
            client_order_index=coi_entry,
            base_amount=base_amt_int,
            price=entry_price_int,
            is_ask=ORDER,  # False=BUY/long, True=SELL/short
            order_type=signer.ORDER_TYPE_LIMIT,
            time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,  # IOC = instant execute
            reduce_only=False,
            trigger_price=0,
            nonce=nonce
        )
        nonce += 1
        if err:
            raise RuntimeError(f"sign entry error: {err}")

        # --- sign FULL-SIZE TP (reduce-only, 100%) ---
        tp_info, err = signer.sign_create_order(
            market_index=MARKET_ID,
            client_order_index=coi_tp,
            base_amount=base_amt_int,               # full size
            price=tp_price_int,
            is_ask=tp_is_ask,
            order_type=signer.ORDER_TYPE_TAKE_PROFIT_LIMIT,
            time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=True,
            trigger_price=tp_price_int,
            nonce=nonce
        )
        nonce += 1
        if err:
            raise RuntimeError(f"sign TP error: {err}")

        # --- sign FULL-SIZE SL (reduce-only, 100%) ---
        sl_info, err = signer.sign_create_order(
            market_index=MARKET_ID,
            client_order_index=coi_sl,
            base_amount=base_amt_int,               # full size
            price=sl_price_int,
            is_ask=sl_is_ask,
            order_type=signer.ORDER_TYPE_STOP_LOSS_LIMIT,
            time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=True,
            trigger_price=sl_price_int,
            nonce=nonce
        )
        nonce += 1
        if err:
            raise RuntimeError(f"sign SL error: {err}")

        # --- batch send (entry + TP + SL) ---
        tx_types = json.dumps([
            signer.TX_TYPE_CREATE_ORDER,
            signer.TX_TYPE_CREATE_ORDER,
            signer.TX_TYPE_CREATE_ORDER
        ])
        tx_infos = json.dumps([entry_info, tp_info, sl_info])
        tx_hashes = await tx_api.send_tx_batch(tx_types=tx_types, tx_infos=tx_infos)
        print(f"[batch] sent OK: {tx_hashes}")

    finally:
        await signer.close()
        await api_client.close()


if __name__ == "__main__":
    asyncio.run(main())
