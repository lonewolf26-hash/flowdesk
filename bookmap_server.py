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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
@app.get("/ping")
def ping(): return {"status": "ok"}


@app.get("/health", response_class=HTMLResponse)
def health(request: Request):
    # JSON for API clients (curl, scripts); HTML for browsers
    wants_json = "application/json" in request.headers.get("accept", "")
    if wants_json:
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "app": "FlowDesk",
            "status": "ok",
            "uptime_seconds": round(time.time() - _server_start, 1),
            "tickers_tracked": len(_snapshots),
            "server_time": datetime.now(timezone.utc).isoformat(),
        })

    uptime_sec = time.time() - _server_start
    hours, rem   = divmod(int(uptime_sec), 3600)
    minutes, sec = divmod(rem, 60)
    uptime_str   = f"{hours}h {minutes}m {sec}s"
    server_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build per-ticker rows
    if _snapshots:
        rows = ""
        for ticker, snap in sorted(_snapshots.items()):
            ts       = snap.get("timestamp", "—")
            cvd      = snap.get("cvd", 0)
            cvd_dir  = snap.get("cvd_direction", "—")
            imb      = snap.get("book_imbalance_pct", 0)
            bid      = snap.get("bid", "—")
            ask      = snap.get("ask", "—")
            n_alerts = len(snap.get("alerts", []))

            dir_color = {"rising": "#27ae60", "falling": "#e74c3c"}.get(cvd_dir, "#f39c12")
            dir_arrow = {"rising": "↑", "falling": "↓"}.get(cvd_dir, "→")
            imb_color = "#27ae60" if imb >= 0 else "#e74c3c"
            alert_badge = (
                f'<span style="background:#e74c3c22;color:#ff6b6b;'
                f'border:1px solid #e74c3c66;border-radius:4px;'
                f'padding:1px 6px;font-size:11px">{n_alerts} alert{"s" if n_alerts!=1 else ""}</span>'
                if n_alerts else
                '<span style="color:#4a6a4a;font-size:11px">no alerts</span>'
            )

            rows += f"""
            <tr>
              <td style="color:#e2e5ed;font-weight:700;font-size:15px">{ticker}</td>
              <td style="color:#4a90d9">{bid} / {ask}</td>
              <td style="color:{dir_color}">{dir_arrow} {cvd:+.0f}</td>
              <td style="color:{imb_color}">{imb:+.1f}%</td>
              <td style="color:#8a8fa8;font-size:11px">{ts}</td>
              <td>{alert_badge}</td>
            </tr>"""

        ticker_section = f"""
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Bid / Ask</th><th>CVD</th>
              <th>Imbalance</th><th>Last Snapshot</th><th>Alerts</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""
    else:
        ticker_section = """
        <div class="no-data">
          No snapshots yet — waiting for Bookmap add-on to connect and send data.
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta http-equiv="refresh" content="5"/>
  <title>FlowDesk — Health</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d0d0f; color: #e2e5ed;
      font-family: "JetBrains Mono", "Fira Code", Consolas, monospace;
      font-size: 13px; padding: 32px;
    }}
    h1 {{ color: #4a90d9; font-size: 20px; margin-bottom: 4px; }}
    .sub {{ color: #8a8fa8; font-size: 11px; margin-bottom: 28px; }}
    .cards {{
      display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px;
    }}
    .card {{
      background: #16181d; border: 1px solid #2a2d36;
      border-radius: 8px; padding: 14px 20px; min-width: 160px;
    }}
    .card-label {{ color: #8a8fa8; font-size: 10px; text-transform: uppercase;
                   letter-spacing: .08em; margin-bottom: 6px; }}
    .card-value {{ font-size: 18px; font-weight: 700; }}
    .green {{ color: #27ae60; }} .blue {{ color: #4a90d9; }}
    .white {{ color: #e2e5ed; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      text-align: left; color: #8a8fa8; font-size: 10px;
      text-transform: uppercase; letter-spacing: .08em;
      padding: 6px 12px; border-bottom: 1px solid #2a2d36;
    }}
    td {{ padding: 9px 12px; border-bottom: 1px solid #1a1c22; }}
    tr:last-child td {{ border-bottom: none; }}
    .no-data {{
      color: #8a8fa8; font-style: italic; padding: 16px 0;
    }}
    .refresh-note {{
      margin-top: 20px; color: #4a6a8a; font-size: 11px;
    }}
  </style>
</head>
<body>
  <h1>FlowDesk</h1>
  <p class="sub">Health check &nbsp;·&nbsp; auto-refreshes every 5 seconds</p>

  <div class="cards">
    <div class="card">
      <div class="card-label">Status</div>
      <div class="card-value green">● Live</div>
    </div>
    <div class="card">
      <div class="card-label">Uptime</div>
      <div class="card-value white">{uptime_str}</div>
    </div>
    <div class="card">
      <div class="card-label">Active Tickers</div>
      <div class="card-value blue">{len(_snapshots)}</div>
    </div>
    <div class="card">
      <div class="card-label">Server Time</div>
      <div class="card-value white" style="font-size:13px">{server_time}</div>
    </div>
  </div>

  {ticker_section}

  <p class="refresh-note">Page refreshes automatically every 5 s</p>
</body>
</html>"""
    return HTMLResponse(content=html)


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
