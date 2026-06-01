# FlowDesk

Live order book data from Bookmap (Windows/TOS) → FastAPI bridge → Mac dashboard.

```
[Bookmap + TOS] → bookmap_bridge.py → POST /update → bookmap_server.py
                                                             ↓
                                         GET /snapshot/{ticker}
                                                             ↓
                                       alpaca_watchlist.html (Mac)
```

---

## Files

| File | Where it runs | Purpose |
|------|--------------|---------|
| `bookmap_server.py` | Windows | FastAPI bridge — receives + serves snapshots |
| `requirements.txt` | Windows | Python deps for the server |
| `bookmap_bridge.py` | Bookmap add-on | Reads order book + CVD, POSTs to server |
| `alpaca_watchlist.html` | Mac browser | Dashboard — Alpaca + Bookmap panels |
| `start_trading.bat` | Windows | One-click launcher for all services |

---

## Setup

### Step 1 — Install Tailscale on both machines

1. Download Tailscale from https://tailscale.com/download
2. Install on Windows trading machine and Mac
3. Sign in to the same Tailscale account on both
4. On Windows, run:
   ```
   tailscale ip -4
   ```
   Note the `100.x.x.x` address — this is your `TAILSCALE_IP`.

### Step 2 — Replace `TAILSCALE_IP_PLACEHOLDER`

Do a global find-replace of `TAILSCALE_IP_PLACEHOLDER` → your actual Tailscale IP in:
- `bookmap_bridge.py` (line near top: `TAILSCALE_IP = "..."`)
- `alpaca_watchlist.html` (line near top: `const TAILSCALE_IP = "..."`)

### Step 3 — Windows: set up the FastAPI server

```bat
:: In a Windows terminal (not WSL)
cd C:\trading
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Test the server starts:
```bat
uvicorn bookmap_server:app --host 0.0.0.0 --port 8766
```
Visit http://localhost:8766/health — should return `{"status":"ok",...}`.

**Firewall:** Allow port 8766 on Windows Defender Firewall for the Tailscale network adapter.

### Step 4 — Windows: set up Bookmap add-on virtualenv (Python 3.7.14)

Bookmap requires Python 3.7.14 for its add-on API. Install it separately:

```bat
:: Download Python 3.7.14 from python.org, install to C:\Python37
C:\Python37\python.exe -m venv C:\trading\bm_venv
C:\trading\bm_venv\Scripts\activate
pip install requests
```

### Step 5 — Install the Bookmap add-on

1. Open Bookmap
2. Go to **Add-ons → Manage add-ons → Add Python add-on**
3. Point it to `C:\trading\bookmap_bridge.py`
4. Set Python interpreter to `C:\trading\bm_venv\Scripts\python.exe`
5. Enable the add-on — it will appear in the Bookmap add-on panel
6. Watch the add-on log panel for `Tracking new ticker: XXX` messages

### Step 6 — Mac dashboard

1. Open `alpaca_watchlist.html` in a browser (or serve via `python3 -m http.server 8765`)
2. Fill in `ALPACA_KEY_ID` and `ALPACA_SECRET` with your Alpaca paper trading keys
3. Edit `WATCHLIST` array to match the instruments open in Bookmap
4. The Bookmap panel below each card updates every 3 seconds

### Step 7 — Edit `start_trading.bat`

Update these paths in the file to match your installation:
```bat
set BOOKMAP_PATH=C:\Program Files\Bookmap\Bookmap.exe
set TWS_PATH=C:\Jts\tws.exe
set PYTHON_VENV=C:\trading\venv\Scripts\python.exe
set SERVER_SCRIPT=C:\trading\bookmap_server.py
```
Then double-click `start_trading.bat` (Run as Administrator) at the start of each session.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server uptime + ticker count |
| `/update` | POST | Receive snapshot from Bookmap add-on |
| `/snapshot/{ticker}` | GET | Latest snapshot for one ticker |
| `/watchlist` | GET | All active ticker snapshots |
| `/snapshot/{ticker}` | DELETE | Remove a ticker from memory |

### Snapshot schema

```json
{
  "ticker": "AAPL",
  "timestamp": "2025-01-01T14:30:00Z",
  "bid": 189.50,
  "ask": 189.51,
  "bid_levels": [
    { "price": 189.50, "size": 200, "cumulative_cob": 200 },
    ...
  ],
  "ask_levels": [...],
  "cvd": 1450.0,
  "cvd_direction": "rising",
  "book_imbalance_pct": 12.5,
  "largest_bid_wall": { "price": 189.00, "cob": 2500 },
  "largest_ask_wall": { "price": 190.00, "cob": 1800 },
  "alerts": [
    { "level": "amber", "message": "Large bid wall @ 189.00 (2,500 lots)" }
  ]
}
```

---

## Troubleshooting

**Dashboard shows "No Bookmap data"**
- Check `bookmap_server.py` is running on Windows
- Ping `http://<TAILSCALE_IP>:8766/health` from Mac Terminal
- Ensure Windows Firewall allows port 8766 inbound on Tailscale adapter

**Bookmap add-on not posting data**
- Check Bookmap's add-on log panel for errors
- Verify `TAILSCALE_IP` in `bookmap_bridge.py` matches Windows Tailscale IP
- Make sure `requests` is installed in the Bookmap Python venv

**CVD always 0**
- Trades must occur — CVD only accumulates from `onTrade()` callbacks
- If market is closed, there will be no trades; test during market hours

**Port 8766 conflict**
- Change `SERVER_PORT` in `bookmap_bridge.py` and `--port` in the uvicorn command
- Update `BOOKMAP_URL` in `alpaca_watchlist.html` accordingly
