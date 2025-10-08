# src/main.py
import asyncio
import os
import json
import signal
import logging
import contextlib
from dotenv import load_dotenv
import lighter
import eth_account
import websockets

# only show our prints
logging.disable(logging.CRITICAL)
load_dotenv()

BASE_URL = os.getenv("BASE_URL")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY")
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX"))
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
WS_URL = BASE_URL.replace("https", "wss") + "/stream"

MARKET_ID = 0  # ETH

# ---------- helpers ----------


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


async def open_ws():
    ws = await websockets.connect(WS_URL, ping_interval=None)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(ws.recv(), timeout=2)  # drain hello if any
    return ws


async def subscribe_market(ws, market_id: int):
    await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{market_id}"}))
    print(f"[ws] subscribed: market_stats/{market_id} (Ctrl+C to stop)")


async def unsubscribe_market(ws, market_id: int):
    with contextlib.suppress(Exception):
        await ws.send(json.dumps({"type": "unsubscribe", "channel": f"market_stats/{market_id}"}))
        await asyncio.sleep(0.2)


def install_signal_handlers(stop: asyncio.Event):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


def handle_market_stats(msg):
    if msg.get("type") != "update/market_stats":
        return
    s = msg.get("market_stats") or {}
    # for single-market channel, market_stats is a single object, not a dict
    mid = s.get("market_id")
    mark = s.get("mark_price")
    last = s.get("last_trade_price")
    oi = s.get("open_interest")
    fr = s.get("current_funding_rate")
    print(f"[ETH] id={mid} mark={mark} last={last} oi={oi} funding={fr}")


async def stream_eth(stop: asyncio.Event):
    ws = await open_ws()
    try:
        await subscribe_market(ws, MARKET_ID)
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (asyncio.CancelledError, websockets.ConnectionClosed):
                break
            with contextlib.suppress(json.JSONDecodeError):
                handle_market_stats(json.loads(raw))
    finally:
        await unsubscribe_market(ws, MARKET_ID)
        await ws.close()
        print("[ws] closed")


async def main():
    if not (BASE_URL and ETH_PRIVATE_KEY and API_KEY_PRIVATE_KEY and API_KEY_INDEX):
        raise RuntimeError("Missing keys in .env")

    signer, api_client = await init_signer()

    stop = asyncio.Event()
    install_signal_handlers(stop)
    task = asyncio.create_task(stream_eth(stop))

    try:
        await task
    except KeyboardInterrupt:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        await signer.close()
        await api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
