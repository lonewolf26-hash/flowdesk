"""
bookmap_bridge.py â€” FlowDesk Bookmap Python Add-on
Reads live order book + trade data from Bookmap, runs iceberg and VWAP
divergence detection, then POSTs enriched snapshots to the FastAPI
bridge server every SNAPSHOT_INTERVAL_SEC seconds.

Compatible with Bookmap Python API / Python 3.7.14.

--- CONFIGURATION ---
Set TAILSCALE_IP to your Windows machine's Tailscale IP before using.
"""

import os
import json
import math
import time
import threading
import logging
import datetime as dt
from collections import deque
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# Bookmap API â€” available only inside a Bookmap add-on process
import bookmap as bm

# ===========================================================================
# CONFIGURATION â€” change TAILSCALE_IP before deploying
# ===========================================================================
TAILSCALE_IP = "127.0.0.1"   # <-- replace with e.g. 100.64.0.1
SERVER_PORT  = 8766
SERVER_URL   = "http://{}:{}/update".format(TAILSCALE_IP, SERVER_PORT)

SNAPSHOT_INTERVAL_SEC = 3       # how often to POST a snapshot
TOP_LEVELS            = 10      # depth levels to include
HTTP_TIMEOUT_SEC      = 2       # requests timeout (keep short)
WALL_COB_THRESHOLD    = 500     # minimum COB to flag a wall alert
IMBALANCE_ALERT_PCT   = 50.0    # imbalance % that triggers alert

# --- Iceberg detection ---
ICEBERG_MIN_CONFIDENCE        = 0.35
REPLENISHMENT_WINDOW_SEC      = 2.0    # max seconds between consume + reappear
REPLENISHMENT_SIZE_TOLERANCE  = 0.20   # allowed Â± ratio for size match
WALL_ABSORPTION_THRESHOLD     = 10000  # shares traded at single level
STRONG_ABSORPTION_THRESHOLD   = 50000  # shares for "strong" absorption
COB_ANOMALY_MULTIPLIER        = 3.0    # COB > 3Ã— avg surrounding â†’ anomaly

# --- VWAP divergence ---
DIVERGENCE_MIN_CONFIDENCE = 0.50
VWAP_EXT_BEARISH          = 8.0    # % above VWAP for bearish divergence
VWAP_EXT_EXHAUSTION       = 15.0   # % above VWAP for exhaustion
CVD_DRAWDOWN_BEARISH      = 10.0   # % CVD drawdown for bearish
CVD_DRAWDOWN_EXHAUSTION   = 20.0   # % CVD drawdown for exhaustion

# --- CVD ---
CVD_WINDOW_SEC       = 10   # window for legacy cvd_direction
CVD_SLOPE_WINDOW_SEC = 30   # window for linear-regression slope

# --- Logging ---
LOG_DIR = Path("C:/trading/logs")
LOG_RETENTION_DAYS = 7
# ===========================================================================

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            str(LOG_DIR / "flowdesk_bridge_{}.log".format(
                dt.datetime.now().strftime("%Y%m%d"))),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("flowdesk_bridge")


# ---------------------------------------------------------------------------
# Log helpers â€” daily JSON line files, 7-day rotation
# ---------------------------------------------------------------------------
def _log_json(prefix, record):
    """Append a JSON record to today's log file. Never raises."""
    try:
        date_str = dt.datetime.now().strftime("%Y%m%d")
        path = LOG_DIR / "{}_{}.json".format(prefix, date_str)
        with open(str(path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        _cleanup_old_logs(prefix)
    except Exception as exc:
        log.error("_log_json error: %s", exc)


def _cleanup_old_logs(prefix):
    """Delete log files older than LOG_RETENTION_DAYS. Never raises."""
    try:
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for p in LOG_DIR.glob("{}_*.json".format(prefix)):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ET time helpers (no pytz required)
# ---------------------------------------------------------------------------
_ET_OFFSET = dt.timedelta(hours=-4)   # EDT; change to -5 after daylight saving ends

def _now_et():
    return dt.datetime.now(dt.timezone.utc) + _ET_OFFSET

def _et_day_key():
    return _now_et().strftime("%Y%m%d")

def _is_after_market_open():
    n = _now_et()
    return (n.hour, n.minute) >= (9, 30)


# ---------------------------------------------------------------------------
# Tiny linear regression (no numpy) for CVD slope
# ---------------------------------------------------------------------------
def _linear_slope(xs, ys):
    """Return slope of least-squares line through (xs, ys).
    Returns 0.0 if fewer than 2 points or degenerate case."""
    n = len(xs)
    if n < 2:
        return 0.0
    sx = sum(xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


# ===========================================================================
# IcebergDetector
# ===========================================================================
class _LevelHistory(object):
    """Per-price-level state for iceberg detection."""
    __slots__ = [
        "initial_size", "last_size", "consumed_at", "consumed_size",
        "replenishment_count", "total_traded", "first_seen_time",
    ]

    def __init__(self, size):
        self.initial_size = size
        self.last_size = size
        self.consumed_at = None       # float timestamp
        self.consumed_size = 0
        self.replenishment_count = 0
        self.total_traded = 0
        self.first_seen_time = time.time()


class IcebergDetector(object):
    """
    Detects hidden institutional iceberg orders via four signals:

    1. Replenishment  â€” level consumed then reappears within 2 s Â±20% size
    2. Trade vs size  â€” total traded >> initial displayed size
    3. Absorption     â€” >10 K shares at a level with minimal price movement
    4. COB anomaly    â€” COB at level > 3Ã— avg of surrounding 5 levels

    NOT thread-safe â€” must be called while TickerState._lock is held.
    """

    def __init__(self, ticker, price_multiplier):
        self.ticker = ticker
        self.pm = price_multiplier
        self._bid_hist = {}    # price_int -> _LevelHistory
        self._ask_hist = {}
        self._icebergs = {}    # price_int -> iceberg dict (includes _private keys)

    # -----------------------------------------------------------------------
    def on_depth(self, is_bid, price_int, size, surrounding_avg_cob=0):
        """Update level history and re-evaluate iceberg confidence."""
        hist = self._bid_hist if is_bid else self._ask_hist
        now = time.time()

        if price_int not in hist:
            if size > 0:
                hist[price_int] = _LevelHistory(size)
            return

        lh = hist[price_int]
        prev = lh.last_size

        if size == 0 and prev > 0:
            # Level was consumed
            lh.consumed_at = now
            lh.consumed_size = prev
            lh.last_size = 0
        elif size > 0 and prev == 0 and lh.consumed_at is not None:
            # Level reappeared â€” check replenishment pattern
            elapsed = now - lh.consumed_at
            if elapsed <= REPLENISHMENT_WINDOW_SEC and lh.consumed_size > 0:
                ratio = float(size) / float(lh.consumed_size)
                lo = 1.0 - REPLENISHMENT_SIZE_TOLERANCE
                hi = 1.0 + REPLENISHMENT_SIZE_TOLERANCE
                if lo <= ratio <= hi:
                    lh.replenishment_count += 1
            lh.last_size = size
        else:
            lh.last_size = size

        self._evaluate(is_bid, price_int, lh, surrounding_avg_cob)

    def on_trade(self, price_int, size, is_bid_aggressor):
        """Track trades at specific price levels (buy aggressor hits asks)."""
        hist = self._ask_hist if is_bid_aggressor else self._bid_hist
        if price_int in hist:
            hist[price_int].total_traded += size

    def purge_far_levels(self, mid_price_int):
        """Remove icebergs where price has moved >5% away."""
        if not mid_price_int:
            return
        for price_int in list(self._icebergs.keys()):
            dist = abs(price_int - mid_price_int)
            if float(dist) / float(mid_price_int) > 0.05:
                del self._icebergs[price_int]

    def get_active(self):
        """Return list of public iceberg dicts (confidence >= threshold)."""
        return [
            {k: v for k, v in ib.items() if not k.startswith("_")}
            for ib in self._icebergs.values()
            if ib["confidence"] >= ICEBERG_MIN_CONFIDENCE
        ]

    # -----------------------------------------------------------------------
    def _evaluate(self, is_bid, price_int, lh, surrounding_avg_cob):
        """Compute confidence and update/create iceberg record."""
        conf = 0.0

        # Signal 1 â€” Replenishment
        if lh.replenishment_count >= 3:
            conf += 0.40
        elif lh.replenishment_count >= 2:
            conf += 0.20

        # Signal 2 â€” Trade vs displayed
        if lh.initial_size > 0:
            if lh.total_traded > lh.initial_size * 5:
                conf += 0.20 + 0.25   # hidden size + absorption confirmed
            elif lh.total_traded > lh.initial_size * 2:
                conf += 0.20

        # Signal 3 â€” Absorption
        if lh.total_traded >= WALL_ABSORPTION_THRESHOLD:
            conf += 0.25

        # Signal 4 â€” COB anomaly
        if surrounding_avg_cob > 0 and lh.last_size > surrounding_avg_cob * COB_ANOMALY_MULTIPLIER:
            conf += 0.15

        conf = min(1.0, conf)

        if conf < ICEBERG_MIN_CONFIDENCE:
            if price_int in self._icebergs:
                del self._icebergs[price_int]
            return

        # Classify signal
        if lh.replenishment_count >= 3 or lh.total_traded > STRONG_ABSORPTION_THRESHOLD:
            signal = "ICEBERG_CONFIRMED"
        elif lh.total_traded >= WALL_ABSORPTION_THRESHOLD:
            signal = "ICEBERG_ABSORPTION"
        elif surrounding_avg_cob > 0 and lh.last_size > surrounding_avg_cob * COB_ANOMALY_MULTIPLIER:
            signal = "ICEBERG_COB_ANOMALY"
        elif lh.replenishment_count >= 2:
            signal = "ICEBERG_LIKELY"
        else:
            signal = "ICEBERG_HIDDEN_SIZE"

        side = "bid" if is_bid else "ask"
        price_f = round(price_int * self.pm, 4)
        now_str = _now_et().strftime("%H:%M:%S")
        is_new = price_int not in self._icebergs

        if is_new:
            self._icebergs[price_int] = {
                "price": price_f,
                "side": side,
                "signal": signal,
                "confidence": round(conf, 2),
                "total_traded": lh.total_traded,
                "replenishment_count": lh.replenishment_count,
                "first_seen": now_str,
                "last_seen": now_str,
                "implication": "SUPPORT" if is_bid else "RESISTANCE",
                "_price_int": price_int,
            }
        else:
            ib = self._icebergs[price_int]
            ib["confidence"] = round(conf, 2)
            ib["signal"] = signal
            ib["total_traded"] = lh.total_traded
            ib["replenishment_count"] = lh.replenishment_count
            ib["last_seen"] = now_str

        # Log newly confirmed icebergs
        if is_new and signal == "ICEBERG_CONFIRMED":
            record = {k: v for k, v in self._icebergs[price_int].items()
                      if not k.startswith("_")}
            record["ticker"] = self.ticker
            record["logged_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _log_json("flowdesk_icebergs", record)
            log.info("ICEBERG_CONFIRMED  %s  side=%s  price=%.4f  conf=%.2f",
                     self.ticker, side, price_f, conf)


# ===========================================================================
# VWAPDivergenceDetector
# ===========================================================================
class VWAPDivergenceDetector(object):
    """
    Detects five divergence signals between price, CVD, and VWAP:

    BEARISH_DIVERGENCE    â€” price >8% above VWAP + CVD falling
    BEARISH_EXHAUSTION    â€” above + price >15% + CVD -20% + vol exhaustion
    BULLISH_DIVERGENCE    â€” price >8% below VWAP + CVD rising
    BULLISH_EXHAUSTION    â€” below + price >15% + CVD recovering + vol exhaustion
    VWAP_RECLAIM_FAILURE  â€” price bounced toward VWAP but CVD still falling
    VWAP_MAGNET           â€” extended >10% with opposing/flat CVD >5 min

    NOT thread-safe â€” must be called while TickerState._lock is held.
    """

    _IMPLICATION = {
        "BEARISH_DIVERGENCE":   "FADE_LONG",
        "BEARISH_EXHAUSTION":   "FADE_LONG",
        "BULLISH_DIVERGENCE":   "COVER_SHORT",
        "BULLISH_EXHAUSTION":   "COVER_SHORT",
        "VWAP_RECLAIM_FAILURE": "FADE_BOUNCE",
        "VWAP_MAGNET":          "REVERSION",
    }

    def __init__(self, ticker):
        self.ticker = ticker
        self.divergence = None       # active divergence dict or None
        self._last_logged = None     # avoid duplicate log lines
        # VWAP reclaim tracking
        self._below_vwap_since = None
        self._bounce_high = None
        # VWAP magnet tracking
        self._extended_since = None  # time price first exceeded 10% ext

    def evaluate(
        self,
        price,
        vwap,
        price_vwap_ext,
        cvd,
        cvd_peak,
        cvd_drawdown_pct,
        cvd_slope,
        vol_last_60,
        vol_prior_60,
        iceberg_ask_prices,    # list of active ask-side iceberg prices
    ):
        """
        Compute divergence signal from current state values.
        Returns the divergence dict or None.
        """
        if vwap == 0 or price == 0:
            self.divergence = None
            return None

        now = time.time()
        now_str = _now_et().strftime("%H:%M:%S")

        # --- VWAP reclaim failure tracking ---
        if price < vwap:
            if self._below_vwap_since is None:
                self._below_vwap_since = now
                self._bounce_high = price
            else:
                if price > self._bounce_high:
                    self._bounce_high = price
        else:
            self._below_vwap_since = None
            self._bounce_high = None

        vwap_reclaim_failure = (
            self._below_vwap_since is not None
            and self._bounce_high is not None
            and self._bounce_high >= vwap * 0.98
            and price < vwap
            and cvd_slope == "falling"
        )

        # --- VWAP magnet tracking ---
        if abs(price_vwap_ext) >= 10.0:
            if self._extended_since is None:
                self._extended_since = now
        else:
            self._extended_since = None

        vwap_magnet = (
            self._extended_since is not None
            and (now - self._extended_since) >= 300   # extended >5 minutes
            and cvd_slope in ("flat", "flattening")
        )

        # --- Volume exhaustion ---
        vol_exhaustion = (
            vol_last_60 > 0
            and vol_prior_60 > 0
            and vol_last_60 < vol_prior_60 * 0.5
        )

        # --- Signal selection (priority order) ---
        signal = None
        conf = 0.0

        if price_vwap_ext >= VWAP_EXT_BEARISH and cvd_slope == "falling":
            conf = 0.0
            conf += 0.25
            if price_vwap_ext >= VWAP_EXT_EXHAUSTION:
                conf += 0.15
            if cvd_drawdown_pct >= CVD_DRAWDOWN_BEARISH:
                conf += 0.25
            if cvd_drawdown_pct >= CVD_DRAWDOWN_EXHAUSTION:
                conf += 0.15
            if vol_exhaustion:
                conf += 0.20
            if vwap_reclaim_failure:
                conf += 0.20
            if iceberg_ask_prices:
                conf += 0.15
            conf = min(1.0, conf)
            if conf >= DIVERGENCE_MIN_CONFIDENCE:
                if (vol_exhaustion
                        and price_vwap_ext >= VWAP_EXT_EXHAUSTION
                        and cvd_drawdown_pct >= CVD_DRAWDOWN_EXHAUSTION):
                    signal = "BEARISH_EXHAUSTION"
                else:
                    signal = "BEARISH_DIVERGENCE"

        elif price_vwap_ext <= -VWAP_EXT_BEARISH and cvd_slope == "rising":
            cvd_recovery_pct = 0.0
            if cvd_peak < 0 and cvd > cvd_peak and cvd_peak != 0:
                cvd_recovery_pct = (cvd - cvd_peak) / abs(cvd_peak) * 100.0
            conf = 0.0
            conf += 0.25
            if abs(price_vwap_ext) >= VWAP_EXT_EXHAUSTION:
                conf += 0.15
            if cvd_recovery_pct >= CVD_DRAWDOWN_BEARISH:
                conf += 0.25
            if cvd_recovery_pct >= CVD_DRAWDOWN_EXHAUSTION:
                conf += 0.15
            if vol_exhaustion:
                conf += 0.20
            conf = min(1.0, conf)
            if conf >= DIVERGENCE_MIN_CONFIDENCE:
                if (vol_exhaustion
                        and abs(price_vwap_ext) >= VWAP_EXT_EXHAUSTION
                        and cvd_recovery_pct >= CVD_DRAWDOWN_EXHAUSTION):
                    signal = "BULLISH_EXHAUSTION"
                else:
                    signal = "BULLISH_DIVERGENCE"

        elif vwap_reclaim_failure:
            signal = "VWAP_RECLAIM_FAILURE"
            conf = 0.70

        elif vwap_magnet:
            signal = "VWAP_MAGNET"
            conf = 0.65

        if signal is None:
            self.divergence = None
            return None

        vwap_dist_cents = round(abs(price - vwap) * 100)
        self.divergence = {
            "signal": signal,
            "confidence": round(conf, 2),
            "price": price,
            "vwap": vwap,
            "price_vwap_ext": round(price_vwap_ext, 2),
            "cvd_current": cvd,
            "cvd_peak": cvd_peak,
            "cvd_drawdown_pct": round(cvd_drawdown_pct, 1),
            "cvd_slope": cvd_slope,
            "vwap_distance_cents": vwap_dist_cents,
            "detected_at": now_str,
            "implication": self._IMPLICATION.get(signal, "MONITOR"),
            "suggested_target": round(vwap, 4),
        }

        # Log on signal change only
        if signal != self._last_logged:
            self._last_logged = signal
            record = dict(self.divergence)
            record["ticker"] = self.ticker
            record["logged_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            record["price_30min_later"] = None
            record["outcome"] = "inconclusive"
            _log_json("flowdesk_divergence", record)
            log.info("DIVERGENCE  %s  signal=%s  conf=%.2f  ext=%.1f%%",
                     self.ticker, signal, conf, price_vwap_ext)

        return self.divergence


# ===========================================================================
# TickerState â€” extended from original with VWAP, CVD history, detectors
# ===========================================================================
class TickerState(object):
    """
    Holds all per-ticker state updated by Bookmap callbacks.
    Extended from original to include:
      - VWAP calculation
      - CVD peak / drawdown / slope
      - Volume history for pace
      - IcebergDetector and VWAPDivergenceDetector instances
      - Daily reset at 9:30 ET
    """

    def __init__(self, ticker):
        self.ticker = ticker

        # Price multiplier from Bookmap (int price â†’ float)
        self.price_multiplier = 0.01

        # --- Order book (price_int â†’ size) ---
        self.bids = {}
        self.asks = {}

        # --- NBBO ---
        self.bid_price = 0.0
        self.ask_price = 0.0

        # --- CVD ---
        self.cvd_total = 0.0
        self.cvd_peak = 0.0           # highest CVD seen today
        self.cvd_valley = 0.0         # lowest CVD seen today (for bullish recovery)
        self.cvd_events = deque()     # (timestamp, delta) for legacy direction window
        self.cvd_history = deque()    # (timestamp, cvd_cumulative) for slope calc

        # --- VWAP ---
        self._cum_pv = 0.0   # Î£(price Ã— volume)
        self._cum_v  = 0.0   # Î£(volume)
        self.vwap = 0.0

        # --- Volume history (timestamp, size) for 60s/prior-60s calc ---
        self.vol_history = deque()

        # --- Day's range ---
        self.day_high = 0.0
        self.day_low  = float("inf")

        # --- Daily reset tracking ---
        self._day_key = _et_day_key()

        # --- Detectors (share this state, called under self._lock) ---
        self.iceberg = IcebergDetector(ticker, self.price_multiplier)
        self.divergence = VWAPDivergenceDetector(ticker)

        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    def _check_daily_reset(self):
        """Reset day-level accumulators at start of new ET trading day."""
        dk = _et_day_key()
        if dk != self._day_key and _is_after_market_open():
            log.info("Daily reset for %s  (new day: %s)", self.ticker, dk)
            self._day_key = dk
            self.cvd_total = 0.0
            self.cvd_peak  = 0.0
            self.cvd_valley = 0.0
            self.cvd_events.clear()
            self.cvd_history.clear()
            self._cum_pv = 0.0
            self._cum_v  = 0.0
            self.vwap = 0.0
            self.vol_history.clear()
            self.day_high = 0.0
            self.day_low  = float("inf")

    # -----------------------------------------------------------------------
    def on_depth(self, is_bid, price_int, size):
        """Called from Bookmap's onDepth callback (any thread)."""
        with self._lock:
            book = self.bids if is_bid else self.asks
            if size == 0:
                book.pop(price_int, None)
            else:
                book[price_int] = size

            # Update NBBO
            if self.bids:
                self.bid_price = max(self.bids) * self.price_multiplier
            if self.asks:
                self.ask_price = min(self.asks) * self.price_multiplier

            # Compute surrounding average COB for COB-anomaly signal
            surrounding_avg = self._surrounding_avg_cob(is_bid, price_int)

            # Feed iceberg detector
            self.iceberg.on_depth(is_bid, price_int, size, surrounding_avg)

    def on_trade(self, price_int, size, is_bid_aggressor):
        """Called from Bookmap's onTrade callback (any thread)."""
        delta = size if is_bid_aggressor else -size
        now = time.time()
        price_f = price_int * self.price_multiplier

        with self._lock:
            self._check_daily_reset()

            # CVD
            self.cvd_total += delta
            self.cvd_events.append((now, delta))
            self.cvd_history.append((now, self.cvd_total))
            if self.cvd_total > self.cvd_peak:
                self.cvd_peak = self.cvd_total
            if self.cvd_total < self.cvd_valley:
                self.cvd_valley = self.cvd_total

            # VWAP
            self._cum_pv += price_f * size
            self._cum_v  += size
            if self._cum_v > 0:
                self.vwap = self._cum_pv / self._cum_v

            # Volume history
            self.vol_history.append((now, size))

            # Day range
            if price_f > self.day_high:
                self.day_high = price_f
            if price_f < self.day_low:
                self.day_low = price_f

            # Feed iceberg detector â€” buy aggressor hits asks
            self.iceberg.on_trade(price_int, size, is_bid_aggressor)

    # -----------------------------------------------------------------------
    def _surrounding_avg_cob(self, is_bid, price_int):
        """Compute average size of 5 levels surrounding price_int."""
        book = self.bids if is_bid else self.asks
        prices = sorted(book.keys())
        if len(prices) < 2:
            return 0
        idx = prices.index(price_int) if price_int in prices else -1
        if idx < 0:
            return 0
        lo = max(0, idx - 2)
        hi = min(len(prices), idx + 3)
        neighbors = [book[p] for p in prices[lo:hi] if p != price_int]
        return sum(neighbors) / len(neighbors) if neighbors else 0

    # -----------------------------------------------------------------------
    def build_snapshot(self):
        """Build a serialisable snapshot dict (thread-safe)."""
        with self._lock:
            pm = self.price_multiplier
            now = time.time()

            # --- Daily reset check ---
            self._check_daily_reset()

            # --- CVD direction (legacy 10s window) ---
            cutoff10 = now - CVD_WINDOW_SEC
            while self.cvd_events and self.cvd_events[0][0] < cutoff10:
                self.cvd_events.popleft()
            window_delta = sum(d for _, d in self.cvd_events)
            if window_delta > 50:
                cvd_direction = "rising"
            elif window_delta < -50:
                cvd_direction = "falling"
            else:
                cvd_direction = "flattening"

            # --- CVD slope (30s linear regression) ---
            cutoff30 = now - CVD_SLOPE_WINDOW_SEC
            while self.cvd_history and self.cvd_history[0][0] < cutoff30:
                self.cvd_history.popleft()
            if len(self.cvd_history) >= 2:
                xs = [t - self.cvd_history[0][0] for t, _ in self.cvd_history]
                ys = [v for _, v in self.cvd_history]
                slope = _linear_slope(xs, ys)
                if slope > 100:
                    cvd_slope = "rising"
                elif slope < -100:
                    cvd_slope = "falling"
                else:
                    cvd_slope = "flat"
            else:
                cvd_slope = "flat"

            # --- CVD drawdown ---
            cvd_drawdown_pct = 0.0
            if self.cvd_peak > 0 and self.cvd_total < self.cvd_peak:
                cvd_drawdown_pct = (self.cvd_peak - self.cvd_total) / self.cvd_peak * 100.0

            # --- VWAP extension ---
            price_vwap_ext = 0.0
            mid = (self.bid_price + self.ask_price) / 2.0 if self.ask_price > 0 else self.bid_price
            if self.vwap > 0 and mid > 0:
                price_vwap_ext = (mid - self.vwap) / self.vwap * 100.0

            # --- Volume pace (last 60s vs prior 60s) ---
            cutoff60  = now - 60
            cutoff120 = now - 120
            while self.vol_history and self.vol_history[0][0] < cutoff120:
                self.vol_history.popleft()
            vol_last_60  = sum(s for t, s in self.vol_history if t >= cutoff60)
            vol_prior_60 = sum(s for t, s in self.vol_history if t < cutoff60)

            # --- Top bid levels (descending price) ---
            sorted_bids = sorted(self.bids.items(), key=lambda x: -x[0])[:TOP_LEVELS]
            bid_levels, cumulative = [], 0
            for p_int, sz in sorted_bids:
                cumulative += sz
                bid_levels.append({
                    "price": round(p_int * pm, 4),
                    "size": sz,
                    "cob": cumulative,
                    "cumulative_cob": cumulative,   # backward compat alias
                })

            # --- Top ask levels (ascending price) ---
            sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])[:TOP_LEVELS]
            ask_levels, cumulative = [], 0
            for p_int, sz in sorted_asks:
                cumulative += sz
                ask_levels.append({
                    "price": round(p_int * pm, 4),
                    "size": sz,
                    "cob": cumulative,
                    "cumulative_cob": cumulative,
                })

            # --- Book imbalance ---
            total_bid_cob = sum(self.bids.values())
            total_ask_cob = sum(self.asks.values())
            total_cob = total_bid_cob + total_ask_cob
            if total_cob > 0:
                book_imbalance = round(
                    (total_bid_cob - total_ask_cob) / float(total_cob), 4)
                book_imbalance_pct = round(book_imbalance * 100, 2)
            else:
                book_imbalance = 0.0
                book_imbalance_pct = 0.0

            # --- Largest walls ---
            largest_bid = max(self.bids.items(), key=lambda x: x[1]) \
                if self.bids else (0, 0)
            largest_ask = max(self.asks.items(), key=lambda x: x[1]) \
                if self.asks else (0, 0)

            # --- Purge and collect icebergs ---
            mid_int = int(mid / pm) if pm > 0 and mid > 0 else 0
            self.iceberg.purge_far_levels(mid_int)
            active_icebergs = self.iceberg.get_active()

            ask_iceberg_prices = [
                ib["price"] for ib in active_icebergs if ib["side"] == "ask"
            ]

            # --- VWAP divergence ---
            div_result = self.divergence.evaluate(
                price=mid,
                vwap=round(self.vwap, 4),
                price_vwap_ext=price_vwap_ext,
                cvd=round(self.cvd_total, 2),
                cvd_peak=round(self.cvd_peak, 2),
                cvd_drawdown_pct=cvd_drawdown_pct,
                cvd_slope=cvd_slope,
                vol_last_60=vol_last_60,
                vol_prior_60=vol_prior_60,
                iceberg_ask_prices=ask_iceberg_prices,
            )

            # --- Confluence detection ---
            confluence_alert = None
            if active_icebergs and div_result:
                best_ib_conf = max(ib["confidence"] for ib in active_icebergs)
                div_conf = div_result["confidence"]
                combined_conf = min(1.0, max(best_ib_conf, div_conf) + 0.15)
                best_ib = max(active_icebergs, key=lambda x: x["confidence"])
                confluence_alert = {
                    "signal": "CONFLUENCE",
                    "confidence": round(combined_conf, 2),
                    "message": "CONFLUENCE: ICEBERG + VWAP DIVERGENCE @ ${:.4f}".format(
                        best_ib["price"]),
                    "iceberg_signal": best_ib["signal"],
                    "divergence_signal": div_result["signal"],
                }

            # --- Alerts list (original + new) ---
            alerts = []
            if largest_bid[1] >= WALL_COB_THRESHOLD:
                alerts.append({
                    "level": "amber",
                    "message": "Large bid wall @ {} ({:,} lots)".format(
                        round(largest_bid[0] * pm, 4), largest_bid[1]),
                })
            if largest_ask[1] >= WALL_COB_THRESHOLD:
                alerts.append({
                    "level": "amber",
                    "message": "Large ask wall @ {} ({:,} lots)".format(
                        round(largest_ask[0] * pm, 4), largest_ask[1]),
                })
            if abs(book_imbalance_pct) >= IMBALANCE_ALERT_PCT:
                side_lbl = "bid-heavy" if book_imbalance_pct > 0 else "ask-heavy"
                alerts.append({
                    "level": "red",
                    "message": "Book imbalance {:.1f}% {}".format(
                        abs(book_imbalance_pct), side_lbl),
                })
            if cvd_direction == "falling" and self.bid_price > 0:
                alerts.append({
                    "level": "amber",
                    "message": "CVD divergence: price holding but CVD falling",
                })
            if confluence_alert:
                alerts.append({
                    "level": "red",
                    "message": confluence_alert["message"],
                })

            return {
                # --- Core (backward compat) ---
                "ticker": self.ticker,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "bid": round(self.bid_price, 4),
                "ask": round(self.ask_price, 4),
                "price": round(mid, 4),
                # --- Book (new field names + compat aliases) ---
                "bids": bid_levels,
                "asks": ask_levels,
                "bid_levels": bid_levels,   # compat
                "ask_levels": ask_levels,   # compat
                # --- CVD ---
                "cvd": round(self.cvd_total, 2),
                "cvd_peak": round(self.cvd_peak, 2),
                "cvd_drawdown_pct": round(cvd_drawdown_pct, 2),
                "cvd_slope": cvd_slope,
                "cvd_direction": cvd_direction,   # compat
                # --- VWAP ---
                "vwap": round(self.vwap, 4),
                "price_vwap_ext": round(price_vwap_ext, 2),
                # --- Book imbalance ---
                "book_imbalance": book_imbalance,
                "book_imbalance_pct": book_imbalance_pct,   # compat
                # --- Walls ---
                "largest_bid_wall": {
                    "price": round(largest_bid[0] * pm, 4),
                    "cob": largest_bid[1],
                },
                "largest_ask_wall": {
                    "price": round(largest_ask[0] * pm, 4),
                    "cob": largest_ask[1],
                },
                # --- Signals ---
                "icebergs": active_icebergs,
                "vwap_divergence": div_result,
                "confluence_alert": confluence_alert,
                # --- Day range ---
                "day_high": round(self.day_high, 4),
                "day_low": round(self.day_low, 4) if self.day_low < float("inf") else 0.0,
                # --- Alerts ---
                "alerts": alerts,
            }


# ---------------------------------------------------------------------------
# Global ticker registry
# ---------------------------------------------------------------------------
_tickers = {}          # alias -> TickerState
_tickers_lock = threading.Lock()


def _get_or_create(alias):
    # type: (str) -> TickerState
    with _tickers_lock:
        if alias not in _tickers:
            _tickers[alias] = TickerState(alias)
            log.info("Tracking new ticker: %s", alias)
        return _tickers[alias]


# ---------------------------------------------------------------------------
# HTTP poster â€” daemon thread, never crashes Bookmap
# ---------------------------------------------------------------------------
def _post_loop():
    session = requests.Session()
    while True:
        time.sleep(SNAPSHOT_INTERVAL_SEC)
        with _tickers_lock:
            tickers_copy = list(_tickers.values())

        for state in tickers_copy:
            try:
                snapshot = state.build_snapshot()
                resp = session.post(SERVER_URL, json=snapshot,
                                    timeout=HTTP_TIMEOUT_SEC)
                resp.raise_for_status()
                log.debug("Posted %s  CVD=%+.0f  vwap_ext=%+.1f%%",
                          state.ticker, snapshot["cvd"],
                          snapshot.get("price_vwap_ext", 0))
            except requests.exceptions.ConnectionError:
                log.warning("Cannot reach server at %s â€” will retry", SERVER_URL)
            except requests.exceptions.Timeout:
                log.warning("POST timeout for %s", state.ticker)
            except Exception as exc:
                # NEVER let an exception propagate out of this thread
                log.error("Unexpected post error for %s: %s", state.ticker, exc)


# ---------------------------------------------------------------------------
# Bookmap Python Add-on entry point
# ---------------------------------------------------------------------------
class BookmapBridge(bm.BookmapAddOn):
    """
    FlowDesk Bookmap Add-On.
    Streams order book + CVD + iceberg + VWAP divergence to FastAPI bridge.
    """

    def initialize(self, addon):
        self.addon = addon
        log.info("FlowDesk BookmapBridge initializing â€” server: %s", SERVER_URL)

        addon.addInstrumentListener(self._on_instrument)

        poster = threading.Thread(
            target=_post_loop, name="flowdesk-poster", daemon=True)
        poster.start()
        log.info("Poster thread started (interval=%ds)", SNAPSHOT_INTERVAL_SEC)

    # -----------------------------------------------------------------------
    def _on_instrument(self, alias, full_name, is_trading, instrument_multiplier):
        """Called when a new instrument becomes active in Bookmap."""
        state = _get_or_create(alias)
        pm = instrument_multiplier if instrument_multiplier else 0.01
        state.price_multiplier = pm
        state.iceberg.pm = pm    # keep detector in sync
        log.info("Instrument: alias=%s  name=%s  multiplier=%s",
                 alias, full_name, pm)

        self.addon.addDepthListener(alias, self._make_depth_cb(alias))
        self.addon.addTradeListener(alias, self._make_trade_cb(alias))

    def _make_depth_cb(self, alias):
        def on_depth(price, is_bid, size):
            try:
                _get_or_create(alias).on_depth(is_bid, price, size)
            except Exception as exc:
                log.error("onDepth error %s: %s", alias, exc)
        return on_depth

    def _make_trade_cb(self, alias):
        def on_trade(price, size, is_bid_aggressor, extra_info=None):
            try:
                _get_or_create(alias).on_trade(price, size, is_bid_aggressor)
            except Exception as exc:
                log.error("onTrade error %s: %s", alias, exc)
        return on_trade


# ---------------------------------------------------------------------------
# Bookmap calls this to get the add-on instance
# ---------------------------------------------------------------------------
def createAddOn():
    return BookmapBridge()


# ---------------------------------------------------------------------------
# Setup instructions (printed on direct execution, not inside Bookmap)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("""
FlowDesk â€” Bookmap Bridge Setup
================================

1. Install Python 3.7.14 from https://python.org
   Recommended path: C:\\Python37

2. Create a dedicated virtualenv for Bookmap:
   C:\\Python37\\python.exe -m venv C:\\trading\\bm_venv
   C:\\trading\\bm_venv\\Scripts\\activate
   pip install requests

3. Edit this file â€” replace 127.0.0.1 with your
   Windows machine's Tailscale IP (run: tailscale ip -4)

4. In Bookmap:
   Add-ons â†’ Manage Add-ons â†’ Add Python Add-on
   Script path:      C:\\trading\\bookmap_bridge.py
   Python path:      C:\\trading\\bm_venv\\Scripts\\python.exe
   Enable the add-on and watch the log panel.

5. Log files are written to: {}
""".format(LOG_DIR))

