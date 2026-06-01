"""
bookmap_bridge.py — Bookmap Python Add-on
Reads live order book + trade data from Bookmap and POSTs snapshots
to the FastAPI bridge server every SNAPSHOT_INTERVAL_SEC seconds.

Compatible with Bookmap Python API / Python 3.7.14.

--- CONFIGURATION ---
Set TAILSCALE_IP to your Windows machine's Tailscale IP before using.
"""

import time
import threading
import logging
from collections import deque, defaultdict
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# Bookmap API — available only inside a Bookmap add-on process
import bookmap as bm

# ===========================================================================
# CONFIGURATION — change TAILSCALE_IP before deploying
# ===========================================================================
TAILSCALE_IP = "TAILSCALE_IP_PLACEHOLDER"   # <-- replace with e.g. 100.64.0.1
SERVER_PORT  = 8766
SERVER_URL   = f"http://{TAILSCALE_IP}:{SERVER_PORT}/update"

SNAPSHOT_INTERVAL_SEC = 3       # how often to POST a snapshot
CVD_WINDOW_SEC        = 10      # window for CVD direction calculation
TOP_LEVELS            = 10      # depth levels to include in snapshot
HTTP_TIMEOUT_SEC      = 5       # requests timeout
WALL_COB_THRESHOLD    = 500     # minimum COB to flag as a wall alert
IMBALANCE_ALERT_PCT   = 50.0    # imbalance % that triggers alert
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bookmap_bridge")

# ---------------------------------------------------------------------------
# Per-ticker state containers
# ---------------------------------------------------------------------------
class TickerState:
    def __init__(self, ticker: str):
        self.ticker = ticker

        # Order book:  price (int, in MBP units) -> size
        self.bids: dict = {}
        self.asks: dict = {}

        # CVD tracking: deque of (timestamp_float, delta) pairs
        self.cvd_events: deque = deque()
        self.cvd_total: float = 0.0

        # Latest NBBO
        self.bid_price: float = 0.0
        self.ask_price: float = 0.0

        # Price multiplier from Bookmap (converts int price to float)
        self.price_multiplier: float = 0.01

        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    def on_depth(self, is_bid: bool, price_int: int, size: int):
        """Called from Bookmap's onDepth callback (any thread)."""
        with self._lock:
            book = self.bids if is_bid else self.asks
            if size == 0:
                book.pop(price_int, None)
            else:
                book[price_int] = size

            # Update best bid/ask
            if self.bids:
                self.bid_price = max(self.bids) * self.price_multiplier
            if self.asks:
                self.ask_price = min(self.asks) * self.price_multiplier

    def on_trade(self, price_int: int, size: int, is_bid_aggressor: bool):
        """Called from Bookmap's onTrade callback (any thread)."""
        delta = size if is_bid_aggressor else -size
        now = time.time()
        with self._lock:
            self.cvd_total += delta
            self.cvd_events.append((now, delta))

    # -----------------------------------------------------------------------
    def build_snapshot(self) -> dict:
        """Build a serialisable snapshot dict (thread-safe)."""
        with self._lock:
            pm = self.price_multiplier
            now = time.time()

            # --- CVD direction over last CVD_WINDOW_SEC seconds ---
            cutoff = now - CVD_WINDOW_SEC
            # purge old events
            while self.cvd_events and self.cvd_events[0][0] < cutoff:
                self.cvd_events.popleft()

            window_delta = sum(d for _, d in self.cvd_events)
            if window_delta > 50:
                cvd_direction = "rising"
            elif window_delta < -50:
                cvd_direction = "falling"
            else:
                cvd_direction = "flattening"

            # --- Top bid levels (descending price) ---
            sorted_bids = sorted(self.bids.items(), key=lambda x: -x[0])[:TOP_LEVELS]
            bid_levels, cumulative = [], 0
            for p_int, sz in sorted_bids:
                cumulative += sz
                bid_levels.append({
                    "price": round(p_int * pm, 4),
                    "size": sz,
                    "cumulative_cob": cumulative,
                })

            # --- Top ask levels (ascending price) ---
            sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])[:TOP_LEVELS]
            ask_levels, cumulative = [], 0
            for p_int, sz in sorted_asks:
                cumulative += sz
                ask_levels.append({
                    "price": round(p_int * pm, 4),
                    "size": sz,
                    "cumulative_cob": cumulative,
                })

            # --- Book imbalance ---
            total_bid_cob = sum(self.bids.values())
            total_ask_cob = sum(self.asks.values())
            total_cob = total_bid_cob + total_ask_cob
            imbalance_pct = (
                round((total_bid_cob - total_ask_cob) / total_cob * 100, 2)
                if total_cob > 0 else 0.0
            )

            # --- Largest walls ---
            largest_bid = max(self.bids.items(), key=lambda x: x[1], default=(0, 0))
            largest_ask = max(self.asks.items(), key=lambda x: x[1], default=(0, 0))

            # --- Alerts ---
            alerts = []
            if largest_bid[1] >= WALL_COB_THRESHOLD:
                alerts.append({
                    "level": "amber",
                    "message": f"Large bid wall @ {round(largest_bid[0]*pm,4)} "
                               f"({largest_bid[1]:,} lots)",
                })
            if largest_ask[1] >= WALL_COB_THRESHOLD:
                alerts.append({
                    "level": "amber",
                    "message": f"Large ask wall @ {round(largest_ask[0]*pm,4)} "
                               f"({largest_ask[1]:,} lots)",
                })
            if abs(imbalance_pct) >= IMBALANCE_ALERT_PCT:
                side = "bid-heavy" if imbalance_pct > 0 else "ask-heavy"
                alerts.append({
                    "level": "red",
                    "message": f"Book imbalance {abs(imbalance_pct):.1f}% {side}",
                })
            # CVD divergence: price rising but CVD falling (or vice-versa)
            if cvd_direction == "falling" and self.bid_price > 0:
                alerts.append({
                    "level": "amber",
                    "message": "CVD divergence: price holding but CVD falling",
                })

            return {
                "ticker": self.ticker,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "bid": round(self.bid_price, 4),
                "ask": round(self.ask_price, 4),
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
                "cvd": round(self.cvd_total, 2),
                "cvd_direction": cvd_direction,
                "book_imbalance_pct": imbalance_pct,
                "largest_bid_wall": {
                    "price": round(largest_bid[0] * pm, 4),
                    "cob": largest_bid[1],
                },
                "largest_ask_wall": {
                    "price": round(largest_ask[0] * pm, 4),
                    "cob": largest_ask[1],
                },
                "alerts": alerts,
            }


# ---------------------------------------------------------------------------
# Global registry of active TickerState objects
# ---------------------------------------------------------------------------
_tickers: dict = {}   # alias -> TickerState
_tickers_lock = threading.Lock()


def _get_or_create(alias: str) -> TickerState:
    with _tickers_lock:
        if alias not in _tickers:
            _tickers[alias] = TickerState(alias)
            log.info("Tracking new ticker: %s", alias)
        return _tickers[alias]


# ---------------------------------------------------------------------------
# HTTP poster — runs in its own daemon thread
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
                resp = session.post(SERVER_URL, json=snapshot, timeout=HTTP_TIMEOUT_SEC)
                resp.raise_for_status()
                log.debug("Posted %s  CVD=%+.0f", state.ticker, snapshot["cvd"])
            except requests.exceptions.ConnectionError:
                log.warning("Cannot reach server at %s — will retry", SERVER_URL)
            except requests.exceptions.Timeout:
                log.warning("POST timeout for %s", state.ticker)
            except Exception as exc:
                # Never let an exception bubble up and crash the Bookmap process
                log.error("Unexpected error posting %s: %s", state.ticker, exc)


# ---------------------------------------------------------------------------
# Bookmap Python Add-on entry point
# ---------------------------------------------------------------------------
class BookmapBridge(bm.BookmapAddOn):
    """Bookmap Python Add-On: streams order book + CVD to FastAPI bridge."""

    def initialize(self, addon):
        self.addon = addon
        log.info("BookmapBridge initializing — server: %s", SERVER_URL)

        # Register listeners
        addon.addInstrumentListener(self._on_instrument)

        # Start HTTP poster thread (daemon — dies with Bookmap process)
        poster = threading.Thread(target=_post_loop, name="bookmap-poster", daemon=True)
        poster.start()
        log.info("Poster thread started")

    # -----------------------------------------------------------------------
    def _on_instrument(self, alias, full_name, is_trading, instrument_multiplier):
        """Called when a new instrument becomes active in Bookmap."""
        state = _get_or_create(alias)
        state.price_multiplier = instrument_multiplier if instrument_multiplier else 0.01
        log.info("Instrument: alias=%s  name=%s  multiplier=%s",
                 alias, full_name, state.price_multiplier)

        # Register per-instrument callbacks
        self.addon.addDepthListener(alias, self._make_depth_cb(alias))
        self.addon.addTradeListener(alias, self._make_trade_cb(alias))

    def _make_depth_cb(self, alias: str):
        def on_depth(price: int, is_bid: bool, size: int):
            try:
                _get_or_create(alias).on_depth(is_bid, price, size)
            except Exception as exc:
                log.error("onDepth error %s: %s", alias, exc)
        return on_depth

    def _make_trade_cb(self, alias: str):
        def on_trade(price: int, size: int, is_bid_aggressor: bool,
                     extra_info=None):
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
