# src/main.py
import asyncio
import os
import json
import time
import contextlib
import logging
from collections import deque
from typing import Literal, Optional, List, Tuple

from dotenv import load_dotenv
import websockets
import lighter
import eth_account
import statistics
import math

# ---------- config ----------
logging.disable(logging.CRITICAL)
load_dotenv()

BASE_URL = os.getenv("BASE_URL")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY")
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX"))
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")
WS_URL = BASE_URL.replace("https", "wss") + "/stream"

PRICE_SCALE = 100
BASE_SCALE = 10000

EMA_FAST_MIN = 20.0
EMA_SLOW_MIN = 50.0

MARKET_ID = 0  # ETH

MAX_SLIPPAGE = 0.001  # 0.1%

MARGIN = 50.0  # size = 50 USD
LEVERAGE = 20.0  # exposure (x20) = 1000 USD

MIN_TP_USD = 0.10  # 0.10 USD Take Profit
MIN_SL_USD = 0.10  # 0.10 USD Stop Loss

DRY_RUN = True  # True = Mock Trades / False = Live Trade

PRINT_INTERVAL = 1.0  # Tick print interval

# ---------- derived constants ----------


def tau_from_period_minutes(N_minutes: float) -> float:
    k = 2.0 / (N_minutes + 1.0)
    return -60.0 / math.log(1.0 - k)


TAU_FAST = tau_from_period_minutes(EMA_FAST_MIN)
TAU_SLOW = tau_from_period_minutes(EMA_SLOW_MIN)
BIAS_EPS_FRAC = 0.0002  # band for LONG/SHORT vs NEUTRAL

TICK_WINDOW = 150               # tick history
Z_LOOKBACK = 24                 # z-score window
SLOPE_LOOKBACK = 3              # short momentum window
Z_LO = 0.7                      # allow more entries
Z_HI = 4.5
Z_STRONG = 2.0
Z_EXHAUST = 3.8                 # spike threshold to watch for reversals

TICKRATE_SPIKE_MULT = 1.10      # mild burst gate
TICKRATE_FADE_MULT = 0.95       # “burst is fading”

HITRUN_LEN = 8
HITRUN_MIN = 3

REGIME_MIN_STRENGTH = 0.00025   # ~2.5 bps EMA spread/price
REGIME_STRONG_STRENGTH = 0.00045  # ~4.5 bps EMA spread/price

STREAM_GAP_SEC = 1.0  # stream handling

# volatility-adaptive exits
SIGMA_WINDOW = 50
TP_SIGMA = 1.10
SL_SIGMA = 1.00

# ---------- globals ----------
MARK_PRICE: Optional[float] = None
BIAS: Literal["LONG", "SHORT", "NEUTRAL"] = "NEUTRAL"
POSITION_SIDE: Literal["LONG", "SHORT", "FLAT"] = "FLAT"
ENTRY_MARK: float = 0.0
ENTRY_QTY_ETH: float = 0.0
ENTRY_TIME: float = 0.0
EMA_FAST_VAL: Optional[float] = None
EMA_SLOW_VAL: Optional[float] = None
EMA_FAST_SLOPE: float = 0.0
EMA_SLOW_SLOPE: float = 0.0
LAST_TICK_TS: Optional[float] = None

TRADE_COUNT = 0
CUMULATIVE_PNL = 0.0
LAST_TICK_PRINT = 0.0

# state for acceleration/burst
_last_slope: float = 0.0
_last_rate_ratio: float = 1.0

# ---------- utils ----------


def now_ms() -> int:
    return int(time.time() * 1000)


def to_int_price(p: float) -> int:
    return int(round(p * PRICE_SCALE))


def base_amount_from_notional_usd(notional_usd: float, price: float) -> int:
    size_eth = max(1e-9, notional_usd / max(price, 1e-9))
    return max(1, int(round(size_eth * BASE_SCALE)))


def qty_eth_from_base_int(base_amt_int: int) -> float:
    return base_amt_int / BASE_SCALE


def median(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def rolling_sigma(prices: deque, lookback: int) -> float:
    n = len(prices)
    if n < max(8, lookback // 3):
        if n < 4:
            return 0.25
        arr = list(prices)
        m = sum(arr) / len(arr)
        mad = median([abs(x - m) for x in arr])
        return mad * 1.4826
    window = list(prices)[-lookback:]
    m = sum(window) / len(window)
    var = sum((x - m) ** 2 for x in window) / max(1, len(window))
    return math.sqrt(var)

# ---------- order helper ----------


async def send_market_order(
    *,
    signer: lighter.SignerClient,
    market_id: int,
    is_ask: bool,
    base_amount_int: int,
    ideal_price: float,
    max_slippage: float = MAX_SLIPPAGE,
) -> str:
    ideal_price_int = to_int_price(ideal_price)
    coi = now_ms()
    _, tx_hash, err = await signer.create_market_order_if_slippage(
        market_index=market_id,
        client_order_index=coi,
        base_amount=base_amount_int,
        max_slippage=max_slippage,
        is_ask=is_ask,
        ideal_price=ideal_price_int,
    )
    if err:
        raise RuntimeError(f"market order error: {err}")
    return str(tx_hash)

# ---------- EMA + bias ----------


def update_tick_emas_and_bias(price: float, ts_sec: float) -> Literal["LONG", "SHORT", "NEUTRAL"]:
    global EMA_FAST_VAL, EMA_SLOW_VAL, LAST_TICK_TS, EMA_FAST_SLOPE, EMA_SLOW_SLOPE
    if LAST_TICK_TS is None:
        LAST_TICK_TS = ts_sec
        EMA_FAST_VAL = EMA_SLOW_VAL = price
        EMA_FAST_SLOPE = EMA_SLOW_SLOPE = 0.0
        return "NEUTRAL"

    dt = max(1e-6, ts_sec - LAST_TICK_TS)
    LAST_TICK_TS = ts_sec
    a_fast = 1.0 - math.exp(-dt / TAU_FAST)
    a_slow = 1.0 - math.exp(-dt / TAU_SLOW)

    prev_fast = EMA_FAST_VAL
    prev_slow = EMA_SLOW_VAL

    EMA_FAST_VAL = (1 - a_fast) * EMA_FAST_VAL + a_fast * price  # type: ignore
    EMA_SLOW_VAL = (1 - a_slow) * EMA_SLOW_VAL + a_slow * price  # type: ignore

    EMA_FAST_SLOPE = EMA_FAST_VAL - prev_fast  # type: ignore
    EMA_SLOW_SLOPE = EMA_SLOW_VAL - prev_slow  # type: ignore

    diff = EMA_FAST_VAL - EMA_SLOW_VAL  # type: ignore
    band = BIAS_EPS_FRAC * price
    if diff > band:
        return "LONG"
    if diff < -band:
        return "SHORT"
    return "NEUTRAL"


def regime_strength(price: float) -> float:
    if EMA_FAST_VAL is None or EMA_SLOW_VAL is None or price <= 0:
        return 0.0
    return abs((EMA_FAST_VAL - EMA_SLOW_VAL) / price)

# ---------- features & signals ----------


def tick_features(tick_prices: deque, tick_times: deque) -> Tuple[float, float, float, int]:
    """
    Returns (z, slope, rate_ratio, safeTicks)
    """
    mark = tick_prices[-1]
    z = 0.0
    if len(tick_prices) >= Z_LOOKBACK:
        window = list(tick_prices)[-Z_LOOKBACK:]
        m = sum(window) / len(window)
        var = sum((x - m) ** 2 for x in window) / max(1, len(window))
        std = math.sqrt(var) if var > 0 else 0.0
        z = (mark - m) / (std + 1e-9)

    if len(tick_prices) > SLOPE_LOOKBACK:
        slope = tick_prices[-1] - tick_prices[-(SLOPE_LOOKBACK + 1)]
    else:
        slope = 0.0

    now = time.time()
    one_sec = sum(1 for t in tick_times if now - t <= 1.0)
    sec_counts = [sum(1 for t in tick_times if now - (i + 1)
                      < t <= now - i) for i in range(10)]
    med_rate = median(sec_counts) or 1.0
    rate_ratio = one_sec / med_rate

    safe = 0
    if len(tick_times) >= 2:
        recent = list(tick_times)[-HITRUN_LEN:]
        for i in range(1, len(recent)):
            if (recent[i] - recent[i - 1]) <= STREAM_GAP_SEC:
                safe += 1
    safe_display = min(HITRUN_LEN, safe if HITRUN_LEN > 1 else safe)

    return z, slope, rate_ratio, safe_display


def hitrun_counts(tick_prices: deque) -> Tuple[int, int]:
    ups = downs = 0
    if len(tick_prices) >= HITRUN_LEN:
        w = list(tick_prices)[-HITRUN_LEN:]
        for i in range(1, len(w)):
            if w[i] > w[i - 1]:
                ups += 1
            elif w[i] < w[i - 1]:
                downs += 1
    return ups, downs


def exhaustion_guard_for_chase(side: str, z: float, slope: float, rate_ratio: float, last_slope: float, last_rate_ratio: float) -> bool:
    """
    Prevents chasing tail-end spikes in the *same* direction.
    """
    if abs(z) >= Z_EXHAUST:
        accel = slope - last_slope
        burst_fading = rate_ratio < max(
            TICKRATE_FADE_MULT * last_rate_ratio, 1.0)
        if side == "LONG":
            if accel < 0 or burst_fading:
                return True
        else:
            if accel > 0 or burst_fading:
                return True
    return False


def flow_score(slope: float, accel: float, rate_ratio: float) -> float:
    """
    Positive => up-flow; Negative => down-flow.
    Combines short-term slope, acceleration, and burstiness.
    """
    score = 0.0
    # slope weight
    score += 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    # acceleration weight
    score += 0.7 if accel > 0 else (-0.7 if accel < 0 else 0.0)
    # burst / liquidity taking
    if rate_ratio > 1.05:
        score += 0.5
    elif rate_ratio < 0.95:
        score -= 0.5
    # EMA-fast slope gives light bias
    score += 0.6 if EMA_FAST_SLOPE > 0 else (-0.6 if EMA_FAST_SLOPE <
                                             0 else 0.0)
    return score


def tick_signal(*, bias, tick_prices, tick_times):
    """
    HFT-friendly:
    - Primary momentum entries with light gates.
    - Reversal entries when flow flips hard (turning wrong-side shorts into longs).
    - No cooldowns.
    """
    global _last_slope, _last_rate_ratio

    if len(tick_prices) < max(24, Z_LOOKBACK + 2, SLOPE_LOOKBACK + 1):
        return "NONE", "not enough ticks"

    z, slope, rate_ratio, _ = tick_features(tick_prices, tick_times)
    ups, downs = hitrun_counts(tick_prices)
    accel = slope - _last_slope
    rs = regime_strength(tick_prices[-1])

    # light regime filter (kept loose to preserve trade frequency)
    if rs < REGIME_MIN_STRENGTH:
        _last_slope, _last_rate_ratio = slope, rate_ratio
        return "NONE", f"chop (rs={rs:.5f})"

    # base conditions
    cond_rate = rate_ratio >= TICKRATE_SPIKE_MULT or (
        ups >= HITRUN_MIN or downs >= HITRUN_MIN)

    # ----- 1) REVERSAL LOGIC (flip when flow turns) -----
    fs = flow_score(slope, accel, rate_ratio)
    # thresholds: require z modestly in the same direction as flow
    if fs >= 2.0 and z >= max(1.0, Z_LO):
        # up-flow; avoid chasing exhaustion to upside
        if not exhaustion_guard_for_chase("LONG", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
            _last_slope, _last_rate_ratio = slope, rate_ratio
            return "LONG", f"REVERSAL fs={fs:.2f} z={z:.2f} slope={slope:.4f} acc={accel:.4f} rate×={rate_ratio:.2f} rs={rs:.5f}"
    if fs <= -2.0 and z <= -max(1.0, Z_LO):
        # down-flow; avoid chasing exhaustion to downside
        if not exhaustion_guard_for_chase("SHORT", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
            _last_slope, _last_rate_ratio = slope, rate_ratio
            return "SHORT", f"REVERSAL fs={fs:.2f} z={z:.2f} slope={slope:.4f} acc={accel:.4f} rate×={rate_ratio:.2f} rs={rs:.5f}"

    # ----- 2) MOMENTUM WITH BIAS (kept permissive) -----
    # allow trades even if bias is opposite when flow is strong; otherwise bias-weighted
    if (bias == "LONG" or fs > 0.8) and z >= Z_LO and slope > 0 and cond_rate:
        if not exhaustion_guard_for_chase("LONG", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
            _last_slope, _last_rate_ratio = slope, rate_ratio
            return "LONG", f"MOM f+ z={z:.2f} slope={slope:.4f} acc={accel:.4f} fs={fs:.2f} rate×={rate_ratio:.2f} rs={rs:.5f}"
    if (bias == "SHORT" or fs < -0.8) and z <= -Z_LO and slope < 0 and cond_rate:
        if not exhaustion_guard_for_chase("SHORT", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
            _last_slope, _last_rate_ratio = slope, rate_ratio
            return "SHORT", f"MOM f- z={z:.2f} slope={slope:.4f} acc={accel:.4f} fs={fs:.2f} rate×={rate_ratio:.2f} rs={rs:.5f}"

    # ----- 3) STRONG OVERRIDE (trend burst) -----
    if rs >= REGIME_STRONG_STRENGTH and rate_ratio >= TICKRATE_SPIKE_MULT:
        if z >= Z_STRONG and slope > 0 and accel > 0:
            if not exhaustion_guard_for_chase("LONG", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
                _last_slope, _last_rate_ratio = slope, rate_ratio
                return "LONG", f"OVR+ z={z:.2f} slope={slope:.4f} acc={accel:.4f} rate×={rate_ratio:.2f} rs={rs:.5f}"
        if z <= -Z_STRONG and slope < 0 and accel < 0:
            if not exhaustion_guard_for_chase("SHORT", z, slope, rate_ratio, _last_slope, _last_rate_ratio):
                _last_slope, _last_rate_ratio = slope, rate_ratio
                return "SHORT", f"OVR- z={z:.2f} slope={slope:.4f} acc={accel:.4f} rate×={rate_ratio:.2f} rs={rs:.5f}"

    _last_slope, _last_rate_ratio = slope, rate_ratio
    return "NONE", f"z={z:.2f} slope={slope:.4f} acc={accel:.4f} fs={fs:.2f} rate×={rate_ratio:.2f} rs={rs:.5f}"


def unrealized_pnl_usd(side, entry_mark, mark, qty_eth):
    move = (mark - entry_mark) if side == "LONG" else (entry_mark - mark)
    return move * qty_eth

# ---------- WebSocket loop ----------


async def ws_hft_loop(market_id, signer):
    global MARK_PRICE, POSITION_SIDE, ENTRY_MARK, ENTRY_QTY_ETH, ENTRY_TIME, BIAS
    global TRADE_COUNT, CUMULATIVE_PNL, LAST_TICK_PRINT

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=10,   # a bit tighter
                ping_timeout=8,
                close_timeout=5,
                max_size=None,
            ) as ws:
                # proactive heartbeat (manual pings)
                async def _heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(6)
                            await ws.ping()
                    except Exception:
                        pass

                hb_task = asyncio.create_task(_heartbeat())

                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=2)

                await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{market_id}"}))
                print("[ws] subscribed to market_stats")
                backoff = 1.0

                tick_prices: deque = deque(maxlen=TICK_WINDOW)
                tick_times: deque = deque(maxlen=600)
                last_recv_ts = time.time()

                while True:
                    raw = await ws.recv()
                    now_ts = time.time()

                    # note stream gaps (monitoring only)
                    if now_ts - last_recv_ts > STREAM_GAP_SEC:
                        pass
                    last_recv_ts = now_ts

                    msg = json.loads(raw)
                    if msg.get("type") != "update/market_stats":
                        continue
                    s = msg.get("market_stats") or {}
                    if s.get("market_id") != market_id:
                        continue
                    mp = s.get("mark_price")
                    if mp is None:
                        continue

                    ts = now_ts
                    MARK_PRICE = float(mp)
                    tick_prices.append(MARK_PRICE)
                    tick_times.append(ts)
                    BIAS = update_tick_emas_and_bias(MARK_PRICE, ts)

                    # features for printing
                    z, slope, rate_ratio, safe = tick_features(
                        tick_prices, tick_times)
                    sigma = rolling_sigma(tick_prices, SIGMA_WINDOW)
                    rs = regime_strength(MARK_PRICE)

                    # print every N seconds
                    if ts - LAST_TICK_PRINT >= PRINT_INTERVAL:
                        if POSITION_SIDE == "FLAT":
                            print(f"[tick] mark={MARK_PRICE:.2f} | bias={BIAS} | z={z:.2f} "
                                  f"slope={slope:.4f} rate×={rate_ratio:.2f} | σ=${sigma:.3f} safeTicks={safe}/{HITRUN_LEN}")
                        else:
                            pnl_live = unrealized_pnl_usd(
                                POSITION_SIDE, ENTRY_MARK, MARK_PRICE, ENTRY_QTY_ETH)
                            print(f"[tick] mark={MARK_PRICE:.2f} | {POSITION_SIDE} entry={ENTRY_MARK:.2f} "
                                  f"pnl=${pnl_live:+.3f} | bias={BIAS} | z={z:.2f} slope={slope:.4f} "
                                  f"rate×={rate_ratio:.2f} | σ=${sigma:.3f} rs={rs:.5f} safeTicks={safe}/{HITRUN_LEN}")
                        LAST_TICK_PRINT = ts

                    # Exit logic (volatility-adaptive, no cooldowns)
                    if POSITION_SIDE != "FLAT":
                        qty = ENTRY_QTY_ETH
                        pnl = unrealized_pnl_usd(
                            POSITION_SIDE, ENTRY_MARK, MARK_PRICE, qty)

                        tp_usd_dyn = max(MIN_TP_USD, TP_SIGMA * sigma * qty)
                        sl_usd_dyn = max(MIN_SL_USD, SL_SIGMA * sigma * qty)
                        hit_tp, hit_sl = pnl >= tp_usd_dyn, pnl <= -sl_usd_dyn

                        if hit_tp or hit_sl:
                            TRADE_COUNT += 1
                            CUMULATIVE_PNL += pnl
                            pnl_pct = (pnl / MARGIN) * 100
                            side_exit_is_ask = (POSITION_SIDE == "LONG")
                            base_int = int(round(qty * BASE_SCALE))

                            print(f"[exit] #{TRADE_COUNT} {POSITION_SIDE}→FLAT @ {MARK_PRICE:.2f} "
                                  f"pnl=${pnl:+.3f} ({pnl_pct:+.2f}%) "
                                  f"CUM={CUMULATIVE_PNL:+.3f} {'(TP)' if hit_tp else '(SL)'}")

                            if not DRY_RUN:
                                try:
                                    await send_market_order(
                                        signer=signer,
                                        market_id=market_id,
                                        is_ask=side_exit_is_ask,
                                        base_amount_int=base_int,
                                        ideal_price=MARK_PRICE,
                                    )
                                except Exception as e:
                                    print(f"[exit] market send error: {e!r}")

                            POSITION_SIDE, ENTRY_QTY_ETH, ENTRY_MARK, ENTRY_TIME = "FLAT", 0.0, 0.0, 0.0

                    # Entry logic (no cooldowns, reversal-aware)
                    if POSITION_SIDE == "FLAT":
                        side, reason = tick_signal(
                            bias=BIAS, tick_prices=tick_prices, tick_times=tick_times)
                        if side != "NONE":
                            notional = MARGIN * LEVERAGE
                            base_int = base_amount_from_notional_usd(
                                notional, MARK_PRICE)
                            qty_eth = qty_eth_from_base_int(base_int)
                            is_ask = (side == "SHORT")

                            print(f"[enter] {side} bias={BIAS} mark≈{MARK_PRICE:.2f} "
                                  f"size≈{qty_eth:.6f} ETH | {reason}")

                            if not DRY_RUN:
                                try:
                                    await send_market_order(
                                        signer=signer,
                                        market_id=market_id,
                                        is_ask=is_ask,
                                        base_amount_int=base_int,
                                        ideal_price=MARK_PRICE,
                                    )
                                except Exception as e:
                                    print(f"[enter] market send error: {e!r}")
                                    continue

                            POSITION_SIDE, ENTRY_MARK, ENTRY_QTY_ETH, ENTRY_TIME = side, MARK_PRICE, qty_eth, time.time()

        except (websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                websockets.exceptions.InvalidStatus,
                asyncio.TimeoutError) as e:
            print(f"[ws] disconnected: {e!r} — reconnecting in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 32.0)
            continue
        except Exception as e:
            print(
                f"[ws] unexpected error: {e!r} — reconnecting in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 32.0)
            continue
        finally:
            with contextlib.suppress(Exception):
                for task in asyncio.all_tasks():
                    if task.get_coro().__name__ == "_heartbeat":
                        task.cancel()

# ---------- init ----------


async def init_signer():
    if not (BASE_URL and ETH_PRIVATE_KEY and API_KEY_PRIVATE_KEY and API_KEY_INDEX is not None):
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

# ---------- main ----------


async def main():
    signer, api_client, account_index, tx_api = await init_signer()
    try:
        await ws_hft_loop(market_id=MARKET_ID, signer=signer)
    finally:
        await signer.close()
        await api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
