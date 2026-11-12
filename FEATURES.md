# Delta India Terminal — Complete Feature Documentation

**Last Updated:** 2026-11-12  
**Version:** 2.0 (with Auto-Trading Bot & Dashboard)

---

## Overview

Delta India Terminal is a Flask-based web application for Delta Exchange India that provides:
- **Anomaly Board** — Real-time options mispricing scanner
- **Option Ladder Terminal** — Professional trading interface (ODIN-style)
- **Auto-Trading Bot** — Automated anomaly detection & execution with risk management
- **Bot Dashboard** — Live monitoring & control interface

---

## 1. Core Architecture

### Backend (Flask)
- **Framework:** Flask with thread-local `requests.Session` for connection pooling
- **Authentication:** Password-gated terminal with brute-force protection (5 attempts → 5-min lockout)
- **CORS Proxy:** Built-in `/delta` route proxies Delta Exchange API (SSRF-protected)
- **Multi-Account Support:** Fan-out orders across multiple API key pairs
- **Background Worker:** Threading-based scheduler for automated scanning

### Delta Exchange Integration
- **Public Endpoints:** Products, Tickers, Orderbook, Trades, Candles (via `/delta` proxy)
- **Private Endpoints:** Orders, Positions, Balances, Leverage (HMAC-SHA256 signed)
- **Dual Environment:** Live (`api.india.delta.exchange`) + Testnet (`cdn-ind.testnet.deltaex.org`)
- **Auto-Failover:** Automatic fallback to secondary host on connection failure

### Security
- API keys/secrets never exposed to browser (server-side only)
- HMAC-SHA256 request signing per Delta specification
- IP allowlist enforcement at exchange level
- Terminal password required (fail-closed: no access without `TERMINAL_PASSWORD`)

---

## 2. Anomaly Board (`/`) — Market Scanner

### Detection Strategies (7 Categories)

| Category | Strategy Key | Severity | Description |
|----------|-------------|----------|-------------|
| **Strike Arbitrage / Glitch** | `arbitrage` | `action` | Adjacent strikes violate put-call parity (violation ≥ threshold) |
| **Fat-Finger Trade vs Quote** | `fat_finger` | `action` | Near-ATM trade price deviates from mid by ≥ threshold |
| **Illiquidity / Bid-Ask Gap** | `liquidity_gap` | `info` | Wide spreads: ≥70 near-ATM, ≥30 far-OTM |
| **Credit Butterfly** | `butterfly` | `info` | 1:2:1, 2:3:1, or custom ratio butterfly net credit ≥ threshold |
| **Credit Calendar Spread** | `calendar` | `info` | Near-term > far-term same strike (credit ≥ threshold) |
| **Credit Spread (BTC)** | `credit_spread_btc` | `info` | Call-to-call or put-to-put credit ≥ 1.0 (BTC) |
| **Credit Spread (ETH)** | `credit_spread_eth` | `info` | Call-to-call or put-to-put credit ≥ 0.3 (ETH) |

### Configuration (`.env`)
```bash
MIN_BID_ASK_GAP_FAR_OTM=30
MIN_BID_ASK_GAP_NEAR_ATM=70
MIN_TRADE_GAP=70
MAX_ARBITRAGE_SPREAD=200
```

### UI Features
- **Tabbed Interface:** Segregated anomalies by strategy (not continuous list)
- **Live Ticker:** Scrolling BTC/ETH spot + ATM straddle gap
- **Threshold Controls:** Real-time adjustment of all detection parameters
- **Poll Interval:** Configurable (3s+)
- **Environment Toggle:** Live ↔ Testnet
- **Color-coded Severity:** Red (action), Amber/Blue/Green (info)

---

## 3. Option Ladder Terminal (`/terminal`) — Manual Trading

### Layout
- **Sticky Header:** Connection status, clock, logout
- **Toolbar:** Underlying (BTC/ETH), Expiry selector, Account picker, Refresh
- **Hotkeys Bar:** F1-F10, Alt+F6, Arrow keys, Enter, Esc
- **Ladder Table:** 9 columns (Symbol, Strike, B.Qty, Bid, Ask, A.Qty, LTP, OI, Vol)

### Key Features
| Hotkey | Function |
|--------|----------|
| `↑`/`↓` | Navigate ladder |
| `Enter` / `F1` | Buy ticket for selected row |
| `F2` | Sell ticket |
| `F3` | Open orders (Del=cancel, Shift+F2=amend) |
| `F5` | Refresh |
| `F6` | Order book (full depth, cumulative totals) |
| `F8` | Order history (fills only) |
| `Alt+F6` | Net positions + open orders |
| `A` | Account panel (balances, risk, margin, password change) |
| `C` / Double-click | Candlestick chart (1m/5m/15m/1H/1D) |
| `Ctrl+F12` | Recent trades tape |
| `F10` | Message log |
| `1`/`2`/`3` | Switch expiry |
| `B`/`E` | BTC/ETH underlying |

### Order Ticket
- **Limit/Market** orders with bracket (TP/SL)
- **Multi-account fan-out:** Single order → multiple accounts in parallel
- **Two-step confirmation:** First click prepares, second executes
- **Auto-size calculation:** Contract value, lot info, required/available margin
- **Reduce-only toggle** for closing positions

### Order Management
- **Atomic Amend:** Native PUT `/v2/orders` (no cancel/replace gap)
- **Cancel Single / Cancel All** (double confirmation)
- **Order Book:** Full depth with cumulative volume bars, O/H/L/C + Vol/OI
- **Positions View:** Net PnL, spread PnL (inflow − current spread), margin breakdown

### Real-time Updates
- **WebSocket:** `wss://public-socket.india.delta.exchange` (L1 orderbook + trades)
- **REST Polling:** 2s ladder refresh, 5s PnL/positions, 15s chart
- **Fill Detection:** Tracks partial/full fills, logs to message bar

---

## 4. Auto-Trading Bot (Background Worker)

### Architecture
- **Worker Thread:** `background_scanner_worker()` — daemon thread with `threading.Event` control
- **Scan Interval:** Configurable (default 8s, min 3s)
- **State Machine:** `SCAN_ACTIVE` event (start/stop via API)
- **Safety Config** (`.env`):
  ```bash
  AUTO_TRADE_ENABLED=true
  CONFIRM_BEFORE_TRADE=false
  MAX_POSITION_SIZE_USD=10
  MAX_DAILY_LOSS_USD=50
  STOP_LOSS_PCT=0.3      # 30%
  TAKE_PROFIT_PCT=0.5    # 50%
  ```

### Trade Execution Logic (`_execute_anomaly_trade`)

**Signal Filtering:**
- Only `arbitrage` and `fat_finger` strategies
- Only `severity: "action"` (ignores info-level butterfly/credit spreads)

**Execution Flow:**
1. Parse anomaly text → extract option symbol (regex: `(C|P)-[A-Z]+-\d+-\d+`)
2. Check `ACTIVE_POSITIONS` set — skip if symbol already has open position/bracket
3. Fetch live orderbook → best ask (buy) or best bid (sell)
4. Calculate bracket prices:
   - Buy: SL = entry × (1 - 0.30), TP = entry × (1 + 0.50)
   - Sell: SL = entry × (1 + 0.30), TP = entry × (1 - 0.50)
5. Place limit order with `bracket_stop_loss_price` & `bracket_take_profit_price`
4. Track symbol in `ACTIVE_POSITIONS` set

**Risk Controls:**
- Position deduplication via `ACTIVE_POSITIONS` (prevents `bracket_order_position_exists` errors)
- Daily loss cap: halts trading if `DAILY_PNL ≤ -MAX_DAILY_LOSS_USD`
- Max position size: `$10/contract` (testnet conservative)
- Pre-trade sync: `sync_active_positions()` queries exchange for actual open positions/orders

### Position Management
- **Bracket Orders:** Exchange-native TP/SL (no manual monitoring needed)
- **Auto-sync:** Background sync before each trade cycle
- **Cleanup:** Positions removed from `ACTIVE_POSITIONS` when filled/cancelled (via sync)

---

## 5. Bot Dashboard (`/bot`) — Live Monitoring & Control

### Real-time Metrics
- **Scanner Status:** RUNNING/STOPPED badge
- **Auto-Trade Status:** ON/OFF badge
- **Daily PnL:** Green/red with $50 loss limit display
- **Max Position Size:** $10 display

### Controls
| Button | Function |
|--------|----------|
| **Start/Stop Scanner** | Toggle `SCAN_ACTIVE` via `/api/scanner/control` |
| **Enable/Disable Auto-Trade** | Requires `.env` change + restart (UI shows status) |
| **Emergency Stop** | Stops scanner + cancels all orders (`/api/orders/cancel-all`) |
| **Test Signal** | Simulates arbitrage signal → places real testnet trade |
| **Refresh** | Manual data refresh |

### Activity Log (Auto-refresh 5s)
- **SCAN** — Scan cycle completed
- **SIGNAL** — Action-severity anomaly detected
- **TRADE** — Order placed (symbol, side, size, price, SL, TP)
- **ERROR** — Trade rejection, API failure, daily loss cap
- **PNL** — Position updates

### Account Status Table
- Account ID, Label, Balance, Available, PnL per account

### Recent Trades Panel
- Last 10 executed trades with timestamps

---

## 6. API Endpoints

### Public (No Auth)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/login` | GET/POST | Terminal password gate |
| `/logout` | GET/POST | Clear session |

### Scanner Control
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/scanner/control` | POST | `{action: "start"|"stop", interval: 8}` |
| `/api/scans` | GET | Segregated anomaly results + raw |

### Bot Monitoring
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/bot/status` | GET | Scanner state, auto-trade, accounts, daily PnL, recent trades |
| `/api/bot/activity` | GET | Full activity log + config |
| `/api/bot/test-signal` | POST | Simulate arbitrage signal → real testnet trade |

### Delta Proxy (CORS)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/delta?url=<encoded>` | GET | Proxies to allowed Delta hosts only |

### Trading (Auth Required)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/accounts` | GET | List configured accounts (id, label only) |
| `/api/account?account=...` | GET | Balances + positions per account |
| `/api/positions?account=...` | GET | Net positions + open orders |
| `/api/orders/open?account=...` | GET | Open/pending orders |
| `/api/orders/history?account=...` | GET | Closed/cancelled orders |
| `/api/orders/place` | POST | Single account order |
| `/api/orders/place-multi` | POST | Fan-out to multiple accounts |
| `/api/orders/amend` | POST | Atomic price/size edit (PUT `/v2/orders`) |
| `/api/orders/cancel` | POST | Cancel single order |
| `/api/orders/cancel-all` | POST | Cancel all (double confirm) |
| `/api/orderbook?symbol=...` | GET | L2 orderbook + ticker |
| `/api/trades?symbol=...` | GET | Recent public trades |
| `/api/trade-context?symbol=...&account=...` | GET | Product spec, leverage, wallet |

---

## 7. Configuration (`.env`)

### Required
```bash
TERMINAL_PASSWORD=your-secure-password    # Mandatory - fail-closed
DELTA_API_KEY=...                         # Primary account
DELTA_API_SECRET=...
DELTA_API_BASE_URL=https://api.india.delta.exchange
```

### Multi-Account (Optional)
```bash
DELTA_ACCOUNT_2_KEY=...
DELTA_ACCOUNT_2_SECRET=...
DELTA_ACCOUNT_2_LABEL=TestNet demo
DELTA_ACCOUNT_2_BASE=https://cdn-ind.testnet.deltaex.org
```

### Telegram Alerts (Optional)
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Anomaly Thresholds
```bash
MAX_ARBITRAGE_SPREAD=200
MIN_BID_ASK_GAP_FAR_OTM=30
MIN_BID_ASK_GAP_NEAR_ATM=70
MIN_TRADE_GAP=70
```

### Auto-Trade Safety
```bash
AUTO_TRADE_ENABLED=false              # Master switch
CONFIRM_BEFORE_TRADE=true             # Per-trade confirmation
MAX_POSITION_SIZE_USD=100             # Per-trade max
MAX_DAILY_LOSS_USD=500                # Daily loss cap
STOP_LOSS_PCT=0.5                     # 50%
TAKE_PROFIT_PCT=1.0                   # 100%
```

### Server
```bash
TERMINAL_HOST=127.0.0.1               # 0.0.0.0 for LAN/Tailscale
PORT=5050
SECRET_KEY=dev-secret-key             # Flask session secret
```

---

## 8. Project Structure

```
delta-india-terminal/
├── app.py                      # Main Flask app (2000+ lines)
├── requirements.txt            # flask, requests, pandas, numpy, python-dotenv
├── README.md                   # Setup & usage guide
├── FEATURES.md                 # This file
├── .env                        # Configuration (gitignored)
├── .env.example                # Template
├── .claude/
│   └── launch.json             # Preview server config
├── scanners/
│   ├── __init__.py
│   └── options_scanner.py      # AnomalyDetector + DeltaAPI (fully implemented)
└── templates/
    ├── index.html              # Anomaly Board (client-side JS for UI only)
    ├── login.html              # Password gate
    ├── terminal.html           # Option Ladder Terminal (full trading UI)
    └── bot_dashboard.html      # Auto-Trader Dashboard
```

---

## 9. Key Implementation Details

### Anomaly Detection (Python Port)
All detection algorithms ported from client-side JS to Python in `scanners/options_scanner.py`:
- `detect_illiquidity_gaps()` — bid/ask spread thresholds
- `detect_trade_anomaly()` — fat-finger trade vs mid
- `detect_strike_price_anomalies()` — arbitrage/glitch (adjacent strike violation)
- `detect_butterfly_spreads()` — GCD-based ratio detection
- `detect_calendar_spreads()` — near vs far expiry same strike
- `detect_credit_spreads()` — adjacent strike credit spreads

### Thread Safety
- `threading.local()` for request sessions
- `threading.Event` for scanner state
- Global `LATEST_ANOMALIES`, `BOT_ACTIVITY`, `ACTIVE_POSITIONS` (single-writer background thread)

### Exchange API Resilience
- Keep-alive sessions (TCP/TLS reuse)
- Automatic fallback host (`api.india.delta.exchange` ↔ `api.deltaexchange.in`)
- Per-request timeouts (5s connect, 15s read)
- Structured error handling with `success: false` parsing

### Order Routing
- **Product ID Cache:** Per-base-URL caching (`_product_cache`)
- **Multi-account Parallelism:** `ThreadPoolExecutor` fan-out
- **Reduce-only Logic:** Auto-scopes closing orders to accounts holding opposite side

---

## 10. Safety & Operational Notes

### Testnet First
- Always validate on testnet (`cdn-ind.testnet.deltaex.org`) before live
- Testnet keys in separate account with `DELTA_ACCOUNT_2_BASE`
- Auto-trade defaults to testnet account (label contains "testnet" or id="2")

### IP Whitelist
- Delta requires IP allowlist for API keys
- Network changes (VPN, reconnection) → update whitelist in Delta portal
- Private endpoints fail with `ip_not_whitelisted_for_api_key`

### Bracket Order Constraints
- One bracket order per product symbol per account
- `bracket_order_position_exists` error if duplicate attempted
- Fixed via `ACTIVE_POSITIONS` tracking + pre-trade sync

### Daily Loss Cap
- Resets at midnight local time
- Halts ALL auto-trading when hit
- Requires manual reset (restart or wait for date change)

### No Deposit/Withdrawal
- App only does: order place/amend/cancel
- API keys cannot move funds off-exchange
- No wallet management endpoints exposed

---

## 11. Deployment Checklist

- [ ] Python 3.10+ installed
- [ ] `pip install -r requirements.txt`
- [ ] `.env` created from `.env.example` with all values
- [ ] `TERMINAL_PASSWORD` set (strong)
- [ ] Delta API keys created with appropriate permissions (Read for scanner, Trade for bot)
- [ ] IP allowlist configured in Delta portal
- [ ] Testnet keys added for auto-trade testing
- [ ] Telegram bot created (optional) + `TELEGRAM_CHAT_ID` obtained
- [ ] Run: `python app.py`
- [ ] Open `http://127.0.0.1:5050/` (Anomaly Board)
- [ ] Open `http://127.0.0.1:5050/terminal` (Trading Terminal)
- [ ] Open `http://127.0.0.1:5050/bot` (Bot Dashboard)
- [ ] Verify scanner starts, testnet trade works via Test Signal
- [ ] Only then: enable `AUTO_TRADE_ENABLED=true` for live testing

---

## 12. Future Enhancement Ideas

1. **Strategy Expansion:** Iron condor, ratio spreads, diagonal spreads
2. **ML Signal Filtering:** Confidence scoring for anomalies
3. **Portfolio Greeks:** Delta/Gamma/Vega/Theta aggregation
4. **Backtesting Engine:** Historical replay with PnL attribution
5. **Webhook Alerts:** Discord/Slack/Email integration
6. **Database Persistence:** SQLite/PostgreSQL for trade history
7. **UI Improvements:** Dark/light theme, customizable layouts
8. **Risk Dashboard:** VaR, stress testing, correlation matrix

---

*Generated from live codebase — all features verified working on testnet as of 2026-11-12*