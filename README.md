# FlowDesk

Live order book + signal detection from Bookmap (Windows/TOS) → FastAPI bridge → Mac dashboard.

```
[Bookmap + TOS]
      │
bookmap_bridge.py   ← IcebergDetector + VWAPDivergenceDetector
      │  POST /update (every 3s)
bookmap_server.py   ← FastAPI, port 8766
      │  GET /watchlist
flowdesk.html       ← RelVolDetector + TRIPLE_CONFLUENCE
      (Mac Chrome, via Tailscale)
```

---

## Signal Stack

| Layer | Signal | Trigger |
|-------|--------|---------|
| **Order Book** | `ICEBERG_LIKELY` | 2 replenishments ≤2 s |
| | `ICEBERG_CONFIRMED` | 3+ replenishments or 50K absorbed |
| | `ICEBERG_ABSORPTION` | >10K shares at single level |
| | `ICEBERG_COB_ANOMALY` | COB >3× surrounding avg |
| **VWAP** | `BEARISH_DIVERGENCE` | Price >8% above VWAP + CVD falling |
| | `BEARISH_EXHAUSTION` | Above + >15% ext + CVD −20% + vol dry |
| | `VWAP_RECLAIM_FAILURE` | Bounced toward VWAP, CVD still falling |
| | `VWAP_MAGNET` | >10% extended, CVD flat >5 min |
| **RelVol** | `VOLUME_SURGE` | Adjusted RelVol ≥3× |
| | `VOLUME_SPIKE` | Adjusted RelVol ≥5× |
| | `VOLUME_EXPLOSION` | Adjusted RelVol ≥10× |
| | `VOLUME_CLIMAX` | Was >5×, now −30% + price off highs |
| **Composite** | `CONFLUENCE` | Iceberg + VWAP divergence simultaneous |
| | `TRIPLE_CONFLUENCE` | RelVol + Iceberg + VWAP div, all conf ≥0.60 |

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `bookmap_server.py` | Windows | FastAPI bridge — receives + serves snapshots |
| `requirements.txt` | Windows | Python deps for server |
| `bookmap_bridge.py` | Bookmap add-on (Python 3.7.14) | Order book, CVD, VWAP, IcebergDetector, VWAPDivergenceDetector |
| `flowdesk.html` | Mac Chrome | Full dashboard — Alpaca + Bookmap + RelVolDetector + TRIPLE_CONFLUENCE |
| `alpaca_watchlist.html` | Mac Chrome | Original minimal dashboard (kept for reference) |
| `start_trading.bat` | Windows | One-click launcher |
| `logs/` | Windows | Daily JSON logs for icebergs, divergences, bridge |

---

## Prerequisites

- **Python 3.7.14** — for Bookmap add-on (download from python.org)
- **Python 3.9+** — for FastAPI server (any modern Python)
- **Bookmap** with Python add-on support and TOS data feed
- **Tailscale** on both Windows and Mac
- **Alpaca** paper trading account (free at alpaca.markets)
- TWS / IBKR for execution (optional, launched by `start_trading.bat`)

---

## Installation

### Step 1 — Install Tailscale

1. Download from https://tailscale.com/download
2. Install on **both** Windows trading machine and Mac
3. Sign in to the **same** Tailscale account on both
4. On Windows, run `tailscale ip -4` — note your `100.x.x.x` address

### Step 2 — Replace `TAILSCALE_IP_PLACEHOLDER`

Global find-replace `TAILSCALE_IP_PLACEHOLDER` → your actual Tailscale IP in:

| File | Variable |
|------|----------|
| `bookmap_bridge.py` | `TAILSCALE_IP = "..."` (line ~33) |
| `flowdesk.html` | `const TAILSCALE_IP = "..."` (line ~5 of script) |
| `start_trading.bat` | `set TAILSCALE_IP=...` |

### Step 3 — FastAPI server (Windows)

```bat
cd C:\trading
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Test it:
```bat
uvicorn bookmap_server:app --host 0.0.0.0 --port 8766
```
Open `http://localhost:8766/health` — you should see the FlowDesk health page.
Run `curl http://localhost:8766/ping` → `{"status":"ok"}` confirms connectivity.

**Firewall:** Allow port 8766 inbound on the Tailscale network adapter in Windows Defender Firewall.

### Step 4 — Bookmap add-on virtualenv (Python 3.7.14)

```bat
:: Install Python 3.7.14 to C:\Python37
C:\Python37\python.exe -m venv C:\trading\bm_venv
C:\trading\bm_venv\Scripts\activate
pip install requests
```

Edit `bookmap_bridge.py` top section:
- `TAILSCALE_IP` — your Windows Tailscale IP
- `LOG_DIR` — path where logs should be written (default `C:/trading/logs`)

### Step 5 — Install Bookmap add-on

1. Open Bookmap
2. **Add-ons → Manage Add-ons → Add Python Add-on**
3. Script path: `C:\trading\bookmap_bridge.py`
4. Python interpreter: `C:\trading\bm_venv\Scripts\python.exe`
5. Enable — watch the add-on log panel for `Tracking new ticker: XXX`

### Step 6 — Dashboard (Mac)

1. Open `flowdesk.html` in Chrome  
   (or `python3 -m http.server 8765` and visit `http://localhost:8765/flowdesk.html`)
2. Fill in Alpaca keys at the top of the `<script>` block:
   ```js
   const ALPACA_KEY_ID     = "PKxxxx";
   const ALPACA_SECRET_KEY = "xxxx";
   ```
3. Type tickers in the input bar and press **Enter**
4. RelVol avg-volume is fetched automatically from Alpaca historical bars on first load

### Step 7 — Edit `start_trading.bat`

Update paths at the top of the file:
```bat
set BOOKMAP_PATH=C:\Program Files\Bookmap\Bookmap.exe
set TWS_PATH=C:\Jts\tws.exe
set PYTHON_VENV=C:\trading\venv\Scripts\python.exe
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set DASHBOARD_PATH=C:\trading\flowdesk.html
```
Double-click `start_trading.bat` (Run as Administrator) at session start.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ping` | GET | `{"status":"ok"}` — lightweight connectivity test |
| `/health` | GET | HTML health page (browser) or JSON (curl/API) |
| `/update` | POST | Receive snapshot from Bookmap add-on |
| `/snapshot/{ticker}` | GET | Latest snapshot for one ticker |
| `/watchlist` | GET | All active snapshots |
| `/snapshot/{ticker}` | DELETE | Remove from memory |

### Extended snapshot schema (v2)

```json
{
  "ticker": "ANY",
  "timestamp": "2025-01-01T14:32:11Z",
  "price": 4.19,
  "bid": 4.18,
  "ask": 4.20,
  "bids": [{"price":4.18,"size":1500,"cob":45000}, "...top 10"],
  "asks": [{"price":4.20,"size":800, "cob":12000}, "...top 10"],
  "cvd": 820013,
  "cvd_peak": 1065209,
  "cvd_drawdown_pct": 23.0,
  "cvd_slope": "falling",
  "vwap": 3.89,
  "price_vwap_ext": 7.71,
  "book_imbalance": -0.20,
  "largest_bid_wall": {"price": 3.60, "cob": 165800},
  "largest_ask_wall": {"price": 5.00, "cob": 222566},
  "icebergs": [{
    "price": 4.90, "side": "ask", "signal": "ICEBERG_CONFIRMED",
    "confidence": 0.85, "total_traded": 52400,
    "replenishment_count": 5, "first_seen": "14:32:11",
    "last_seen": "14:38:44", "implication": "RESISTANCE"
  }],
  "vwap_divergence": {
    "signal": "BEARISH_DIVERGENCE", "confidence": 0.78,
    "price_vwap_ext": 7.71, "cvd_drawdown_pct": 23.0,
    "cvd_slope": "falling", "suggested_target": 3.89,
    "implication": "FADE_LONG"
  },
  "confluence_alert": {
    "signal": "CONFLUENCE", "confidence": 0.93,
    "message": "CONFLUENCE: ICEBERG + VWAP DIVERGENCE @ $4.90"
  },
  "alerts": [{"level": "red", "message": "..."}]
}
```

---

## Log Files

All logs are written to `C:\trading\logs\` on Windows.
Files older than 7 days are deleted automatically.

| File pattern | Contents |
|---|---|
| `flowdesk_bridge_YYYYMMDD.log` | Bridge heartbeat, instrument events, errors |
| `flowdesk_icebergs_YYYYMMDD.json` | One JSON line per `ICEBERG_CONFIRMED` detection |
| `flowdesk_divergence_YYYYMMDD.json` | One JSON line per new divergence signal |

The divergence log includes `price_30min_later` and `outcome` fields (initially `null`/`"inconclusive"`) for future backtesting.

---

## Troubleshooting

**Dashboard shows "Bookmap offline"**
- Verify server is running: `curl http://TAILSCALE_IP:8766/ping`
- Check Windows Firewall allows port 8766 on Tailscale adapter
- Check `logs/flowdesk_server.log` for server errors

**Bookmap add-on not posting**
- Open Bookmap add-on log panel — look for connection errors
- Confirm `TAILSCALE_IP` in `bookmap_bridge.py` is set correctly
- Verify `requests` is installed in `bm_venv`

**CVD always 0**
- CVD accumulates from `onTrade()` — requires live market hours
- No trades = no CVD movement; test during market hours

**RelVol shows "—"**
- Avg daily volume fetch requires valid Alpaca keys
- Check browser console for API errors

**TRIPLE_CONFLUENCE never fires**
- All three signals need confidence ≥ 0.60 simultaneously
- Most common on high-momentum names 30–90 min after open
- Check that Bookmap add-on is running and sending iceberg data

**Port 8766 conflict**
- Change `SERVER_PORT` in `bookmap_bridge.py` and `--port` in uvicorn command
- Update `BOOKMAP_PORT` in `flowdesk.html`
