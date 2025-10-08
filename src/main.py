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
MAX_SLIPPAGE = 0.001  # 1%

MARGIN = 10.0
LEVERAGE = 20.0

SHORT = True
LONG = False

ORDER = SHORT


def next_coi() -> int:
    return int(time.time() * 1000)


def to_int_price(p: float) -> int:
    return int(round(p * PRICE_SCALE))


def base_amount_from_notional_usd(notional_usd: float, price: float) -> int:
    size_eth = notional_usd / price
    return int(round(size_eth * BASE_SCALE))


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
    return signer, api_client


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
                    await asyncio.sleep(0.1)
                return price


async def main():
    if not (BASE_URL and ETH_PRIVATE_KEY and API_KEY_PRIVATE_KEY and API_KEY_INDEX):
        raise RuntimeError("Missing keys in .env")

    signer, api_client = await init_signer()
    try:
        # --- get current price
        mark = await get_mark_price_once(MARKET_ID)
        ideal_price_int = to_int_price(mark)

        # --- compute notional from desired margin × assumed leverage
        notional_usd = MARGIN * LEVERAGE  # e.g., $10 × 20x = $200
        base_amt_int = base_amount_from_notional_usd(notional_usd, mark)

        # --- Open
        coi = next_coi()
        print(f"[open] Create notional=${notional_usd:.2f} (margin≈${MARGIN:.2f}) "
              f"base_int={base_amt_int} mark≈{mark:.2f} slippage=1% coi={coi}")

        _, tx_hash, err = await signer.create_market_order_if_slippage(
            market_index=MARKET_ID,
            client_order_index=coi,
            base_amount=base_amt_int,
            max_slippage=MAX_SLIPPAGE,
            is_ask=ORDER,
            ideal_price=ideal_price_int
        )
        if err:
            print(f"[open] error: {err}")
        else:
            print(f"[open] submitted tx_hash={tx_hash}")

        print("[run] Ctrl+C to CLOSE position (sell same size)")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        # --- Close
        mark_exit = await get_mark_price_once(MARKET_ID)
        ideal_price_int_exit = to_int_price(mark_exit)
        print(
            f"[close] SELL base_int={base_amt_int} mark≈{mark_exit:.2f} slippage=1% coi={coi}")

        _, tx_hash, err = await signer.create_market_order_if_slippage(
            market_index=MARKET_ID,
            client_order_index=coi,
            base_amount=base_amt_int,
            max_slippage=MAX_SLIPPAGE,
            is_ask=not ORDER,
            ideal_price=ideal_price_int_exit
        )
        if err:
            print(f"[close] error: {err}")
        else:
            print(f"[close] submitted tx_hash={tx_hash}")

    finally:
        await signer.close()
        await api_client.close()
        print("[done] closed")

if __name__ == "__main__":
    asyncio.run(main())
