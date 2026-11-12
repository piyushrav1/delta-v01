# Delta India Terminal

Local web terminal for Delta Exchange India: an options **anomaly board** (`/`)
plus an ODIN-style **option ladder terminal** (`/terminal`) with live trading.

## Setup (every user does this)

1. Install Python 3.10+, then:
   ```
   pip install -r requirements.txt
   ```
2. Copy the env template and fill in **your own** values:
   ```
   copy .env.example .env
   ```
   - `DELTA_API_KEY` / `DELTA_API_SECRET` — from Delta → API Management.
     Needs **Trade** permission for live orders (a read-only key can only
     view data). Enable Delta's **IP allowlist** for your machine.
   - `TERMINAL_PASSWORD` — **required**. Pages refuse to load without it.
   - Extra accounts (master-terminal fan-out):
     ```
     DELTA_ACCOUNT_SECOND_KEY=...
     DELTA_ACCOUNT_SECOND_SECRET=...
     DELTA_ACCOUNT_SECOND_LABEL=Second
     ```
     Repeat with `THIRD`, `FOURTH`, … as needed. Restart the server after
     editing `.env` (it loads once at startup).
   - `TELEGRAM_*` — optional, leave blank to disable alerts.
- `AUTO_TRADE_ENABLED` — set to `true` to enable automated trade execution (default: `false`).
- `MAX_POSITION_SIZE_USD` — maximum USD per trade (default: `100`).
- `MAX_DAILY_LOSS_USD` — daily loss cap in USD (default: `500`).
- `CONFIRM_BEFORE_TRADE` — require manual confirmation via Telegram before each trade (default: `true`).
- `STOP_LOSS_PCT` — stop loss percentage e.g. `0.5` for 50% (default: `0.5`).
- `TAKE_PROFIT_PCT` — take profit percentage e.g. `1.0` for 100% (default: `1.0`).
3. Run:
   ```
   python app.py
   ```
4. Open `http://127.0.0.1:5000/` (board) and
   `http://127.0.0.1:5000/terminal` (ladder). Log in with your
   `TERMINAL_PASSWORD`.

## To-dos for a new user

- [ ] Fill `.env` (keys, terminal password) and restart the server.
- [ ] Confirm the console prints `Loaded Delta accounts: [...]` with your accounts.
- [ ] Open the ladder, check expiries + spot load and status shows Connected.
- [ ] Press F3/F8 with a **read-only** key first to verify signing works.
- [ ] Switch to a **trade-enabled** key, then test amend on a small order.
- [ ] Compare header PnL vs your Delta app at the same minute before sizing up.

## Terminal hotkeys (`/terminal`)

| Key | Function |
| --- | -------- |
| `↑` / `↓` | Move ladder selection |
| `Enter` | Buy ticket for selected row |
| `F1` / `F2` | Buy / sell Delta-style ticket (qty autofocus, double-Enter submits, LIVE) |
| `F3` | My open orders (arrows select, `Del` cancels, `Shift+F2` amends price+qty) |
| `Shift+F2` (in F3) | Amend selected order via native edit (atomic) |
| `Del` (in F3) | Cancel selected order |
| `F5` | Refresh ladder |
| `F6` | Order book: full depth, cumulative totals, O/H/L/C + volume/OI (auto-refreshes, `F1`/`F2` trade from it) |
| `F8` | Past orders (closed/cancelled) |
| `Alt+F6` | Net positions + open orders |
| `A` | Account panel: Balances + Risk and Margin, change terminal password |
| `C` / double-click | Candlestick chart for row (1m/5m/15m/1H/1D, wheel-zoom, drag-pan) |
| `Ctrl+F12` | Recent exchange trades (Price / Size BTC / Time-Taker) |
| `F10` | Full message log (bottom bar keeps last 3) |
| `1` / `2` / `3` | Switch expiry |
| `B` / `E` | BTC / ETH underlying |
| `Esc` | Close panel |

Ladder shows ATM + OTM strikes only (puts ≤ spot ascending into FUT, calls ≥ spot
ascending below). `S`/`L` badges mark strikes where you hold short/long positions.
Header ticks scope F3/F8/positions/PnL per account; the ticket fans one order out
to every checked account with per-account results. Header PnL = spread formula
(inflow − current spread value, SHORT closes at ask / LONG at bid), per account.

## Anomaly board (`/`)

Set thresholds, hit **Start scanning**. Polls public market data through the
built-in `/delta` proxy and groups hits into tabs: arbitrage/glitch, fat-finger,
bid-ask gaps, butterflies, calendars, credit spreads.

## Safety rules (read before live trading)

- **No deposits/withdrawals exist** anywhere in this app — only order
  placement, amendment and cancellation. (API keys can't move funds off-exchange
  either.)
- Every live action needs an explicit confirm step. Cancel-ALL needs two.
- Login is throttled: 5 wrong passwords per IP locks logins for 5 minutes.
  Rotate the terminal password anytime from the Account panel (`A`).
- `TERMINAL_HOST` defaults to `127.0.0.1` (this PC only). Use `0.0.0.0` only
  with the password set, for Tailscale/LAN — never port-forward 5000 raw to
  the internet. Prefer Tailscale or a Cloudflare Tunnel for remote access.
- Keep `.env` out of synced/shared folders, never commit or share it, and
  rotate any key that was ever exposed.
- Test on testnet keys first (`https://cdn-ind.testnet.deltaex.org`).
- If the office PC shows `ModuleNotFoundError: No module named 'requests'`,
  that machine skipped step 1 — run `pip install -r requirements.txt` there,
  then create its own `.env` (never copy yours over chat/email).
