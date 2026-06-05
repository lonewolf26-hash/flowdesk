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

import yfinance as yf

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
    # --- Required core fields (all versions) ---
    ticker: str
    timestamp: str          # ISO-8601

    # --- Price ---
    bid: float = 0.0
    ask: float = 0.0
    price: Optional[float] = None

    # --- Book levels (accept both old and new field names) ---
    bid_levels: list = []   # compat alias
    ask_levels: list = []   # compat alias
    bids: list = []         # new name
    asks: list = []         # new name

    # --- CVD (all fields optional so old add-on still works) ---
    cvd: float = 0.0
    cvd_direction: str = "flattening"   # legacy
    cvd_slope: Optional[str] = None     # new: "rising"/"falling"/"flat"
    cvd_peak: Optional[float] = None
    cvd_drawdown_pct: Optional[float] = None

    # --- VWAP (new) ---
    vwap: Optional[float] = None
    price_vwap_ext: Optional[float] = None

    # --- Book imbalance (both naming conventions) ---
    book_imbalance_pct: float = 0.0
    book_imbalance: Optional[float] = None

    # --- Walls ---
    largest_bid_wall: dict = {}
    largest_ask_wall: dict = {}

    # --- Signals (new) ---
    icebergs: list = []
    vwap_divergence: Optional[dict] = None
    confluence_alert: Optional[dict] = None

    # --- Day range (new) ---
    day_high: Optional[float] = None
    day_low: Optional[float] = None

    # --- Alerts ---
    alerts: list = []

    class Config:
        extra = "allow"   # accept any additional fields from future add-on versions

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
            ts        = snap.get("timestamp", "—")
            cvd       = snap.get("cvd", 0)
            cvd_slope = snap.get("cvd_slope") or snap.get("cvd_direction", "—")
            imb       = snap.get("book_imbalance_pct", 0)
            bid       = snap.get("bid", "—")
            ask       = snap.get("ask", "—")
            n_alerts  = len(snap.get("alerts") or [])
            vwap_ext  = snap.get("price_vwap_ext")
            n_icebergs = len(snap.get("icebergs") or [])
            div       = snap.get("vwap_divergence") or {}
            div_sig   = div.get("signal", "")
            div_conf  = div.get("confidence", 0)
            confluence = snap.get("confluence_alert") or {}
            conf_sig  = confluence.get("signal", "")

            dir_color  = {"rising": "#27ae60", "falling": "#e74c3c"}.get(cvd_slope, "#f39c12")
            dir_arrow  = {"rising": "↑", "falling": "↓"}.get(cvd_slope, "→")
            imb_color  = "#27ae60" if imb >= 0 else "#e74c3c"

            # VWAP extension cell
            if vwap_ext is not None:
                ext_color = "#e74c3c" if vwap_ext >= 8 else ("#27ae60" if vwap_ext <= -8 else "#8a8fa8")
                ext_cell  = '<span style="color:{}">{:+.1f}%</span>'.format(ext_color, vwap_ext)
            else:
                ext_cell = '<span style="color:#4a6a5a">—</span>'

            # Iceberg cell
            if n_icebergs:
                ice_cell = '<span style="color:#00bcd4">🐋 {}</span>'.format(n_icebergs)
            else:
                ice_cell = '<span style="color:#3a4050">—</span>'

            # Divergence cell
            div_colors = {
                "BEARISH_EXHAUSTION": "#ff3333", "BEARISH_DIVERGENCE": "#e74c3c",
                "BULLISH_EXHAUSTION": "#00e676", "BULLISH_DIVERGENCE": "#27ae60",
                "VWAP_RECLAIM_FAILURE": "#f39c12", "VWAP_MAGNET": "#4a90d9",
                "CONFLUENCE": "#ffd700",
            }
            if conf_sig:
                div_display = conf_sig
                div_color   = div_colors.get(conf_sig, "#ffd700")
            elif div_sig:
                div_display = "{} {:.0f}%".format(div_sig, div_conf * 100)
                div_color   = div_colors.get(div_sig, "#8a8fa8")
            else:
                div_display, div_color = "—", "#3a4050"
            div_cell = '<span style="color:{};font-size:11px">{}</span>'.format(
                div_color, div_display)

            alert_badge = (
                '<span style="background:#e74c3c22;color:#ff6b6b;'
                'border:1px solid #e74c3c66;border-radius:4px;'
                'padding:1px 6px;font-size:11px">{} alert{}</span>'.format(
                    n_alerts, "s" if n_alerts != 1 else "")
                if n_alerts else
                '<span style="color:#4a6a4a;font-size:11px">—</span>'
            )

            rows += """
            <tr>
              <td style="color:#e2e5ed;font-weight:700;font-size:15px">{ticker}</td>
              <td style="color:#4a90d9">{bid} / {ask}</td>
              <td style="color:{dc}">{da} {cvd:+.0f}</td>
              <td style="color:{ic}">{imb:+.1f}%</td>
              <td>{ext}</td>
              <td>{ice}</td>
              <td>{div}</td>
              <td style="color:#8a8fa8;font-size:10px">{ts}</td>
              <td>{alerts}</td>
            </tr>""".format(
                ticker=ticker, bid=bid, ask=ask,
                dc=dir_color, da=dir_arrow, cvd=cvd,
                ic=imb_color, imb=imb,
                ext=ext_cell, ice=ice_cell, div=div_cell,
                ts=ts, alerts=alert_badge,
            )

        ticker_section = """
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Bid / Ask</th><th>CVD</th>
              <th>Imbalance</th><th>VWAP Ext</th>
              <th>Icebergs</th><th>Signal</th>
              <th>Last Snapshot</th><th>Alerts</th>
            </tr>
          </thead>
          <tbody>{}</tbody>
        </table>""".format(rows)
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
    data = snapshot.dict()
    _snapshots[snapshot.ticker] = data
    # Build a concise log line including new signal fields when present
    n_icebergs = len(data.get("icebergs") or [])
    div_sig    = (data.get("vwap_divergence") or {}).get("signal", "")
    conf_sig   = (data.get("confluence_alert") or {}).get("signal", "")
    extras = " ".join(filter(None, [
        "{}ice".format(n_icebergs) if n_icebergs else "",
        div_sig,
        conf_sig,
    ]))
    log.info("Updated %-8s  CVD=%+.0f  ext=%+.1f%%  imb=%.1f%%  %s",
             snapshot.ticker,
             snapshot.cvd,
             snapshot.price_vwap_ext or 0.0,
             snapshot.book_imbalance_pct,
             extras)
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


# ---------------------------------------------------------------------------
# Float / Short Interest endpoint  (via yfinance)
# ---------------------------------------------------------------------------
def _format_shares(n):
    if not n:
        return "Unknown"
    if n >= 1e9:
        return f"{n/1e9:.2f}B"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}K"
    return str(int(n))


@app.get("/float/{symbol}")
async def get_float_data(symbol: str):
    """Return float shares, short interest %, and days-to-cover via yfinance."""
    try:
        ticker_obj = yf.Ticker(symbol.upper())
        info = ticker_obj.info

        float_shares      = info.get("floatShares")
        si_pct            = info.get("shortPercentOfFloat")
        dtc               = info.get("shortRatio")
        shares_outstanding = info.get("sharesOutstanding")

        return {
            "symbol":             symbol.upper(),
            "float":              _format_shares(float_shares),
            "float_raw":          float_shares,
            "si_pct":             f"{si_pct * 100:.1f}%" if si_pct else "Unknown",
            "dtc":                f"{dtc:.1f}" if dtc else "Unknown",
            "shares_outstanding": _format_shares(shares_outstanding),
            "source":             "yahoo_finance",
        }
    except Exception as e:
        log.warning("Float fetch failed for %s: %s", symbol, e)
        return {
            "symbol":  symbol.upper(),
            "float":   "Unknown",
            "si_pct":  "Unknown",
            "dtc":     "Unknown",
            "error":   str(e),
        }
