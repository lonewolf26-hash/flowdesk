"""
bookmap_server.py — FastAPI bridge server (Windows)
Receives order book snapshots from Bookmap add-on and serves them to the Mac dashboard.

Run with:
    uvicorn bookmap_server:app --host 0.0.0.0 --port 8766 --reload
"""

import time
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bookmap_server")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="FlowDesk", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    # Allow localhost dev + any Tailscale 100.x.x.x address
    allow_origins=[
        "http://localhost",
        "http://localhost:8765",
        "http://localhost:8766",
        "http://127.0.0.1:8765",
        "http://127.0.0.1:8766",
    ],
    allow_origin_regex=r"http://100\.\d+\.\d+\.\d+(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory store  { ticker: snapshot_dict }
# ---------------------------------------------------------------------------
_snapshots: Dict[str, dict] = {}
_server_start: float = time.time()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BookLevel(BaseModel):
    price: float
    size: int
    cumulative_cob: int

class Alert(BaseModel):
    level: str          # "red" | "amber"
    message: str

class Snapshot(BaseModel):
    ticker: str
    timestamp: str      # ISO-8601
    bid: float
    ask: float
    bid_levels: list    # list of BookLevel dicts
    ask_levels: list
    cvd: float
    cvd_direction: str  # "rising" | "falling" | "flattening"
    book_imbalance_pct: float   # 0-100
    largest_bid_wall: dict      # {price, cob}
    largest_ask_wall: dict
    alerts: list        # list of Alert dicts

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "app": "FlowDesk",
        "status": "ok",
        "uptime_seconds": round(time.time() - _server_start, 1),
        "tickers_tracked": len(_snapshots),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/update")
def update_snapshot(snapshot: Snapshot):
    """Receive a snapshot dict from the Bookmap add-on."""
    _snapshots[snapshot.ticker] = snapshot.dict()
    log.info("Updated %-8s  CVD=%+.0f  imbalance=%.1f%%",
             snapshot.ticker, snapshot.cvd, snapshot.book_imbalance_pct)
    return {"status": "ok", "ticker": snapshot.ticker}


@app.get("/snapshot/{ticker}")
def get_snapshot(ticker: str):
    """Return the latest snapshot for a single ticker."""
    key = ticker.upper()
    if key not in _snapshots:
        raise HTTPException(status_code=404, detail=f"No data for {key}")
    return _snapshots[key]


@app.get("/watchlist")
def get_watchlist():
    """Return all active ticker snapshots as a dict keyed by ticker."""
    return _snapshots


@app.delete("/snapshot/{ticker}")
def delete_snapshot(ticker: str):
    """Remove a ticker from the in-memory store (admin use)."""
    key = ticker.upper()
    if key not in _snapshots:
        raise HTTPException(status_code=404, detail=f"No data for {key}")
    del _snapshots[key]
    return {"status": "deleted", "ticker": key}
