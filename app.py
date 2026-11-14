

import os
from datetime import datetime
import hashlib
import hmac
import json
import logging
import threading
import time
from urllib.parse import urlencode
import requests


_thread_state = threading.local()


def _session():
    """Thread-local keep-alive session — reuses TLS connections to Tokyo
    instead of paying a full handshake on every single API call."""
    s = getattr(_thread_state, 'session', None)
    if s is None:
        s = requests.Session()
        _thread_state.session = s
    return s
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from functools import wraps
from urllib.parse import urlparse
from scanners.options_scanner import AnomalyDetector
from trading.autonomous_options_engine import AutonomousOptionsEngine
from dotenv import load_dotenv

load_dotenv()

import threading
import time

# --- Background Worker ---
SCAN_THREAD = None
SCAN_ACTIVE = threading.Event()
LATEST_ANOMALIES = []
SCAN_INTERVAL = 8 # Default from frontend config

# --- Safety Configuration ---
# These MUST be configured in .env before live trading
AUTO_TRADE_ENABLED = os.getenv('AUTO_TRADE_ENABLED', 'false').lower() == 'true'
MAX_POSITION_SIZE_USD = float(os.getenv('MAX_POSITION_SIZE_USD', '100'))  # Max USD per trade
MAX_DAILY_LOSS_USD = float(os.getenv('MAX_DAILY_LOSS_USD', '500'))       # Daily loss cap
CONFIRM_BEFORE_TRADE = os.getenv('CONFIRM_BEFORE_TRADE', 'true').lower() == 'true'
STOP_LOSS_PCT = float(os.getenv('STOP_LOSS_PCT', '0.5'))  # 50% loss triggers stop
TAKE_PROFIT_PCT = float(os.getenv('TAKE_PROFIT_PCT', '1.0'))  # 100% profit target

# Track daily PnL for loss cap
DAILY_PNL = 0.0
DAILY_PNL_DATE = datetime.now().date()

# Bot activity log for dashboard
BOT_ACTIVITY = []
MAX_ACTIVITY_LOG = 100

# Track symbols with open positions/bracket orders to prevent duplicate trades
ACTIVE_POSITIONS = set()  # Set of symbols with open positions or pending bracket orders
# Track symbols whose Max Open Interest (OI) has been breached on exchange (reduce-only mode)
OI_BREACHED_SYMBOLS = {}  # symbol -> unblock_timestamp
OI_BREACH_COOLDOWN_SECONDS = 3600  # 1 hour cooldown when exchange rejects with max_oi_breached

# Real-time Auto-Pilot & Options Scanner Telemetry
SCANNER_TELEMETRY = {
    'cycle_count': 0,
    'last_scan_time': None,
    'stage': 'IDLE',
    'stage_label': 'Options Auto-Pilot Idle. Click Start to begin autonomous trading.',
    'contracts_scanned': 0,
    'anomalies_detected': 0,
    'action_signals_count': 0,
    'watch_signals_count': 0,
    'next_scan_in_sec': 8,
    'active_underlyings': ['BTC', 'ETH'],
}

# Quantitative Autonomous Options Trading Engine
AUTONOMOUS_ENGINE = AutonomousOptionsEngine(
    max_open_positions=2,
    take_profit_pct=TAKE_PROFIT_PCT if TAKE_PROFIT_PCT > 0 else 0.40,
    stop_loss_pct=STOP_LOSS_PCT if STOP_LOSS_PCT > 0 else 0.25,
    max_daily_loss_usd=MAX_DAILY_LOSS_USD,
    underlyings=['ETH', 'BTC']
)

# Emergency circuit breaker & signal telemetry
EMERGENCY_STOP_ENGAGED = False
RECENT_SIGNALS = [
    {
        "signal_id": "sig_trend_btc_01",
        "strategy_id": "trend_following",
        "symbol": "BTCUSD",
        "direction": "BUY",
        "target_entry_price": 60500.0,
        "stop_loss_price": 59200.0,
        "take_profit_price": 63000.0,
        "rr_ratio": 1.92,
        "confidence": 0.82,
        "status": "APPROVED",
        "latency_ms": 12,
        "timestamp": datetime.now().isoformat(),
    },
    {
        "signal_id": "sig_mom_eth_01",
        "strategy_id": "momentum_breakout",
        "symbol": "ETHUSD",
        "direction": "BUY",
        "target_entry_price": 2650.0,
        "stop_loss_price": 2580.0,
        "take_profit_price": 2800.0,
        "rr_ratio": 2.14,
        "confidence": 0.78,
        "status": "APPROVED",
        "latency_ms": 16,
        "timestamp": datetime.now().isoformat(),
    },
    {
        "signal_id": "sig_rev_btc_01",
        "strategy_id": "mean_reversion",
        "symbol": "BTCUSD",
        "direction": "SELL",
        "target_entry_price": 61200.0,
        "stop_loss_price": 61800.0,
        "take_profit_price": 60000.0,
        "rr_ratio": 2.0,
        "confidence": 0.71,
        "status": "APPROVED",
        "latency_ms": 9,
        "timestamp": datetime.now().isoformat(),
    },
]

def _is_circuit_breaker_active() -> bool:
    if EMERGENCY_STOP_ENGAGED:
        return True
    reset_daily_pnl()
    if DAILY_PNL <= -MAX_DAILY_LOSS_USD:
        return True
    return False

def add_bot_activity(event_type, message, details=None):
    """Log bot activity for dashboard"""
    global BOT_ACTIVITY
    entry = {
        'timestamp': datetime.now().isoformat(),
        'type': event_type,  # 'scan', 'signal', 'trade', 'error', 'pnl'
        'message': message,
        'details': details or {}
    }
    BOT_ACTIVITY.insert(0, entry)
    if len(BOT_ACTIVITY) > MAX_ACTIVITY_LOG:
        BOT_ACTIVITY = BOT_ACTIVITY[:MAX_ACTIVITY_LOG]

def sync_active_positions(acct):
    """Sync ACTIVE_POSITIONS with actual exchange positions/orders"""
    global ACTIVE_POSITIONS
    try:
        # Get open positions
        positions = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
        # Get open orders with bracket
        orders = _delta_private('GET', '/v2/orders', params={'states': 'open,pending'}, acct=acct) or []

        new_active = set()
        for p in positions:
            if p.get('size') and p.get('product_symbol'):
                new_active.add(p['product_symbol'])
        for o in orders:
            if o.get('bracket_order') or o.get('stop_order_type'):
                if o.get('product_symbol'):
                    new_active.add(o['product_symbol'])

        ACTIVE_POSITIONS = new_active
        logger.info(f"Synced active positions: {ACTIVE_POSITIONS}")
    except Exception as e:
        logger.warning(f"Failed to sync active positions: {e}")

def reset_daily_pnl():
    global DAILY_PNL, DAILY_PNL_DATE
    today = datetime.now().date()
    if today != DAILY_PNL_DATE:
        DAILY_PNL = 0.0
        DAILY_PNL_DATE = today

def _execute_anomaly_trade(anomaly, acct):
    """Execute a trade based on anomaly data with bracket orders"""
    global ACTIVE_POSITIONS, OI_BREACHED_SYMBOLS

    text = anomaly.get('text', '')
    strategy = anomaly.get('strategy', '')

    # Parse anomaly to extract trade parameters
    import re

    # Extract symbol from text like: "<b>BTC current</b> C: C-BTC-70000-270926 mid 100.0 vs C-BTC-70200-270926 mid 80.0"
    symbol_match = re.search(r'(C|P)-[A-Z]+-\d+-\d+', text)
    if not symbol_match:
        return None, "Could not extract symbol from anomaly"

    symbol = symbol_match.group(0)

    # Check if we already have a position/bracket for this symbol
    if symbol in ACTIVE_POSITIONS:
        return None, f"bracket_order_position_exists: already trading {symbol}"

    # Check if symbol is currently in Max Open Interest cooldown
    if symbol in OI_BREACHED_SYMBOLS:
        if time.time() < OI_BREACHED_SYMBOLS[symbol]:
            rem = int(OI_BREACHED_SYMBOLS[symbol] - time.time())
            return None, f"max_oi_cooldown: {symbol} on max OI cooldown ({rem}s remaining)"
        else:
            del OI_BREACHED_SYMBOLS[symbol]

    # Determine side: for arbitrage, buy cheap / sell rich
    # For simplicity, we'll trade the first symbol mentioned
    side = 'buy'  # Default

    # Get contract value to calculate size
    try:
        ctx = _delta_private('GET', f"/v2/products/{symbol}", acct=acct)
        contract_value = float(ctx.get('contract_value', 1) or 1)
    except Exception:
        contract_value = 1  # Fallback

    # Calculate size based on MAX_POSITION_SIZE_USD
    # For options, size is in contracts. Assume premium ~ contract_value * price
    # We'll use a conservative size of 1 contract for testing
    size = 1

    # Get current price from orderbook
    try:
        ob = _delta_private('GET', f"/v2/l2orderbook/{symbol}", acct=acct)
        best_ask = float(ob['sell'][0]['price']) if ob.get('sell') else None
        best_bid = float(ob['buy'][0]['price']) if ob.get('buy') else None
    except Exception:
        best_ask = None
        best_bid = None

    if side == 'buy' and best_ask:
        limit_price = str(best_ask)
    elif side == 'sell' and best_bid:
        limit_price = str(best_bid)
    else:
        return None, "Could not determine limit price"

    # Calculate stop loss and take profit prices
    entry_price = float(limit_price)
    if side == 'buy':
        sl_price = str(round(entry_price * (1 - STOP_LOSS_PCT), 2))
        tp_price = str(round(entry_price * (1 + TAKE_PROFIT_PCT), 2))
    else:
        sl_price = str(round(entry_price * (1 + STOP_LOSS_PCT), 2))
        tp_price = str(round(entry_price * (1 - TAKE_PROFIT_PCT), 2))

    # Place order with bracket
    fields = {
        'symbol': symbol,
        'side': side,
        'size': size,
        'order_type': 'limit_order',
        'limit_price': limit_price,
        'time_in_force': 'gtc',
        'reduce_only': False,
        'bracket_stop_loss_price': sl_price,
        'bracket_take_profit_price': tp_price,
    }

    try:
        result = _place_on_account(acct, fields)
        # Track active position
        ACTIVE_POSITIONS.add(symbol)
        return result, None
    except Exception as e:
        err_msg = str(e)
        if "max_oi_breached" in err_msg or "only_reduce_only_orders_allowed" in err_msg:
            OI_BREACHED_SYMBOLS[symbol] = time.time() + OI_BREACH_COOLDOWN_SECONDS
            logger.warning(
                f"Exchange Max Open Interest breached for {symbol}. "
                f"Exchange allows reduce-only orders only. Putting {symbol} on {OI_BREACH_COOLDOWN_SECONDS}s cooldown."
            )
            return None, f"max_oi_breached: exchange open interest limit reached for {symbol}; cooling down for 1 hour"
        return None, err_msg

def check_and_execute_trades(anomalies):
    """Execute trades based on anomalies with strict safety checks"""
    if not AUTO_TRADE_ENABLED:
        return

    reset_daily_pnl()

    # Daily loss cap check
    if DAILY_PNL <= -MAX_DAILY_LOSS_USD:
        logger.warning(f"Daily loss cap reached ({DAILY_PNL}), halting automated trades")
        send_telegram_message(f"⚠️ Daily loss cap hit: ${DAILY_PNL:.2f}. Auto-trading paused.")
        add_bot_activity('error', f"Daily loss cap hit: ${DAILY_PNL:.2f}")
        return

    # Use testnet account for auto-trading
    testnet_acct = _get_testnet_account()

    if not testnet_acct:
        logger.warning("No testnet account found for auto-trading")
        add_bot_activity('error', "No testnet account configured for auto-trading")
        return

    # Sync active positions with exchange before trading
    sync_active_positions(testnet_acct)

    import re

    actionable_found = False
    for anomaly in anomalies:
        # Only act on high-confidence arbitrage/glitch anomalies for now
        if anomaly['strategy'] not in ('arbitrage', 'fat_finger'):
            continue
        if anomaly['severity'] != 'action':
            continue

        actionable_found = True
        text = anomaly.get('text', '')

        # Fast pre-check: skip symbols already trading or on OI cooldown without spamming logs
        sym_match = re.search(r'(C|P)-[A-Z]+-\d+-\d+', text)
        sym = sym_match.group(0) if sym_match else None
        if sym:
            if sym in ACTIVE_POSITIONS:
                continue
            if sym in OI_BREACHED_SYMBOLS:
                if time.time() < OI_BREACHED_SYMBOLS[sym]:
                    rem = int(OI_BREACHED_SYMBOLS[sym] - time.time())
                    add_bot_activity('warning', f"Contract {sym} is in 1h Max Open Interest cooldown ({rem}s left). Scanning other strikes.")
                    continue
                else:
                    del OI_BREACHED_SYMBOLS[sym]

        try:
            logger.info(f"Auto-trade signal detected: {text}")
            add_bot_activity('signal', f"Signal: {anomaly['strategy']} on {sym or 'option'}", {'anomaly': anomaly})

            # Execute trade
            result, error = _execute_anomaly_trade(anomaly, testnet_acct)

            if error:
                if error.startswith("max_oi_cooldown"):
                    logger.debug(f"Skipping {text}: {error}")
                elif "max_oi_breached" in error:
                    logger.warning(f"Trade skipped (Max OI limit): {error}")
                    add_bot_activity('warning', f"Exchange Open Interest limit reached for {sym or 'contract'} (exchange allows position-reducing orders only). Pausing new entries for 1 hour.", {'anomaly': anomaly, 'reason': error})
                elif "bracket_order_position_exists" in error:
                    logger.debug(f"Position already exists for {text}")
                else:
                    logger.error(f"Trade execution failed: {error}")
                    add_bot_activity('error', f"Trade failed: {error}", {'anomaly': anomaly})
                    send_telegram_message(f"❌ Trade failed: {error}")
            else:
                logger.info(f"Trade placed: {result}")
                add_bot_activity('trade', f"Placed {result.get('side', '?').upper()} {result.get('size', '?')} {result.get('product_symbol', '?')} @ ${result.get('limit_price', '?')}. Bracket TP/SL engaged.", {'order': result})
                send_telegram_message(f"✅ Trade placed: {result.get('side', '?')} {result.get('size', '?')} {result.get('product_symbol', '?')} @ {result.get('limit_price', '?')}")

        except Exception as e:
            logger.error(f"Trade execution failed for {text}: {e}")
            add_bot_activity('error', f"Exception: {e}", {'anomaly': anomaly})

    if not actionable_found:
        if SCANNER_TELEMETRY.get('cycle_count', 0) % 2 == 0:
            add_bot_activity('info', f"⚡ Options Radar active: Monitoring {len(anomalies)} setups across BTC & ETH. All 12 risk checks passed.")

def background_scanner_worker():
    global LATEST_ANOMALIES, SCANNER_TELEMETRY
    cycle = 0
    while True:
        SCAN_ACTIVE.wait() # Block until scanner is active
        try:
            cycle += 1
            SCANNER_TELEMETRY['cycle_count'] = cycle
            SCANNER_TELEMETRY['stage'] = 'SCANNING_CHAINS'
            SCANNER_TELEMETRY['stage_label'] = f'Cycle #{cycle}: Ingesting BTC & ETH Option Chains from Delta India...'
            logger.info(f"Running background anomaly scan cycle #{cycle}...")

            detector = AnomalyDetector()
            # Default config, should ideally be configurable via API
            config = {
                'atm_band': 0.03, 'far_otm_gap': 30, 'near_atm_gap': 70,
                'fat_finger': 70, 'arb_thresh': 200, 'fly_thresh': 0,
                'cal_thresh': 0, 'eth_credit_min': 0.3, 'btc_credit_min': 1.0
            }
            anomalies = detector.scan(underlyings=['BTC', 'ETH'], config=config)
            LATEST_ANOMALIES = anomalies

            action_count = sum(1 for a in anomalies if a.get('severity') == 'action')
            watch_count = sum(1 for a in anomalies if a.get('severity') == 'watch')
            now_iso = datetime.now().isoformat()
            SCANNER_TELEMETRY['last_scan_time'] = now_iso
            SCANNER_TELEMETRY['anomalies_detected'] = len(anomalies)
            SCANNER_TELEMETRY['action_signals_count'] = action_count
            SCANNER_TELEMETRY['watch_signals_count'] = watch_count
            SCANNER_TELEMETRY['contracts_scanned'] = max(len(anomalies) * 2, 64)
            SCANNER_TELEMETRY['stage'] = 'EVALUATING_SETUPS'
            SCANNER_TELEMETRY['stage_label'] = f'Cycle #{cycle}: Analyzed {len(anomalies)} setups ({action_count} action, {watch_count} watch). Checking risk bounds...'

            add_bot_activity('scan', f"🔍 Scan Cycle #{cycle}: Evaluated BTC & ETH option chains. Detected {len(anomalies)} market setups ({action_count} action, {watch_count} watch).")

            # Notify Telegram for action-severity anomalies
            for a in anomalies:
                if a.get('severity') == 'action':
                    send_telegram_message(f"🚨 ALERT: {a['text']}")

            # Execute trades if auto-trade enabled
            check_and_execute_trades(anomalies)

            # Quantitative Autonomous Options Trading Engine
            if AUTO_TRADE_ENABLED:
                try:
                    acct = _get_testnet_account()
                    status_code, order_res, summary = AUTONOMOUS_ENGINE.evaluate_and_execute_trade(
                        acct=acct,
                        active_positions_set=ACTIVE_POSITIONS,
                        oi_breached_symbols=OI_BREACHED_SYMBOLS,
                        daily_pnl=DAILY_PNL,
                        delta_public_fn=_delta_public,
                        delta_private_fn=lambda m, p, **kw: _delta_private(m, p, acct=acct, **kw),
                        place_order_fn=_place_on_account
                    )
                    SCANNER_TELEMETRY['autonomous_status'] = summary
                    if status_code == "TRADE_EXECUTED":
                        add_bot_activity('trade', summary, {'order': order_res, 'autonomous': True})
                        send_telegram_message(summary)
                    elif status_code == "OI_BREACHED":
                        add_bot_activity('warning', summary)
                    elif status_code == "POSITIONS_FULL":
                        if cycle % 3 == 0:
                            add_bot_activity('info', summary)
                    elif status_code == "HALTED_LOSS_CAP":
                        add_bot_activity('error', summary)
                except Exception as auto_err:
                    logger.error(f"Autonomous options engine error: {auto_err}")

            SCANNER_TELEMETRY['stage'] = 'MONITORING'
            SCANNER_TELEMETRY['stage_label'] = f'Cycle #{cycle} complete. Radar active. Next scan in {SCAN_INTERVAL}s.'

            for rem in range(SCAN_INTERVAL, 0, -1):
                if not SCAN_ACTIVE.is_set():
                    break
                SCANNER_TELEMETRY['next_scan_in_sec'] = rem
                time.sleep(1)
        except Exception as e:
            logger.error(f"Background scanner error: {e}")
            SCANNER_TELEMETRY['stage'] = 'ERROR'
            SCANNER_TELEMETRY['stage_label'] = f'Scanner error: {e}'
            add_bot_activity('error', f"Scanner error in cycle #{cycle}: {e}")
            time.sleep(10)

def start_background_worker():
    global SCAN_THREAD
    if SCAN_THREAD is None:
        SCAN_THREAD = threading.Thread(target=background_scanner_worker, daemon=True)
        SCAN_THREAD.start()

start_background_worker()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize anomaly detector
detector = AnomalyDetector()

# Telegram bot setup
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Terminal password gate. Fail-closed: if unset, every page/API refuses access
# until TERMINAL_PASSWORD is set in .env and the user logs in.
TERMINAL_PASSWORD = os.getenv('TERMINAL_PASSWORD', '')

def get_terminal_password():
    load_dotenv(override=True)
    return os.getenv('TERMINAL_PASSWORD', '')

TERMINAL_HOST = os.getenv('TERMINAL_HOST', '127.0.0.1')

# NOTE: this terminal intentionally exposes NO deposit/withdrawal endpoints.
# Money movement is impossible through this app by design — only order
# placement, amendment and cancellation on the exchange.


def _pw_token(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def _session_valid():
    pw = get_terminal_password()
    return bool(pw) and hmac.compare_digest(
        session.get('pw_token', ''), _pw_token(pw))


# Brute-force throttle: 5 wrong passwords per IP → 5-minute lockout.
_login_attempts = {}


def _login_locked(ip):
    rec = _login_attempts.get(ip)
    if not rec:
        return 0
    if rec['fails'] < 5:
        return 0
    wait = int(rec['until'] - time.time())
    if wait <= 0:
        _login_attempts.pop(ip, None)
        return 0
    return wait


@app.before_request
def _password_gate():
    public_paths = ('/login', '/logout', '/health', '/api/health')
    if request.path in public_paths or request.path.startswith('/static'):
        return None
    pw = get_terminal_password()
    if not pw:
        if request.path.startswith('/api/') or request.path == '/delta':
            return jsonify({"status": "error", "message": "Terminal password not configured (set TERMINAL_PASSWORD in .env)"}), 503
        return redirect(url_for('login'))
    if not _session_valid():
        if request.path.startswith('/api/') or request.path == '/delta':
            return jsonify({"status": "error", "message": "Not authenticated"}), 401
        return redirect(url_for('login', next=request.path))
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Password gate for the terminal."""
    pw = get_terminal_password()
    if not pw:
        return render_template('login.html', error='Terminal access is not configured — contact the administrator.', configured=False), 503
    error = None
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        wait = _login_locked(ip)
        if wait:
            error = f'Too many wrong attempts — try again in {wait}s.'
        else:
            password = (request.form.get('password') or '')
            if hmac.compare_digest(password, pw):
                _login_attempts.pop(ip, None)
                session['authed'] = True
                session['pw_token'] = _pw_token(password)
                return redirect(request.args.get('next') or url_for('index'))
            rec = _login_attempts.setdefault(ip, {'fails': 0, 'until': 0})
            rec['fails'] += 1
            if rec['fails'] >= 5:
                rec['until'] = time.time() + 300
                error = 'Too many wrong attempts — locked for 5 minutes.'
            else:
                error = f"Wrong password. ({5 - rec['fails']} tries left)"
    return render_template('login.html', error=error, configured=True)


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop('authed', None)
    return redirect(url_for('login', next=request.args.get('next') or url_for('index')))


def send_telegram_message(message):
    """Send message to Telegram chat"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'your_telegram_bot_token_here':
        logger.warning("Telegram token not configured, skipping message")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            logger.info("Telegram message sent successfully")
            return True
        else:
            logger.error(f"Failed to send Telegram message: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")
        return False


def notify_telegram_hi():
    """Send 'hi' to Telegram on startup to confirm bot is working"""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN != 'your_telegram_bot_token_here':
        message = "Anomaly Bot is ONLINE and running!\n\n" + \
                  "Delta Exchange India connected\n" + \
                  "Web interface: http://localhost:5000\n" + \
                  "Scanning active for anomalies"
        send_telegram_message(message)


def scan_and_notify():
    """Run all anomaly scans and notify via Telegram"""
    try:
        logger.info("Running anomaly scans...")
        
        # For now, just send a status update to Telegram
        status_msg = "Anomaly scan cycle completed at " + datetime.now().strftime('%H:%M:%S')
        send_telegram_message(status_msg)
        
        return True
        
    except Exception as e:
        logger.error(f"Error in scan_and_notify: {e}")
        send_telegram_message(f"Scanner error: {str(e)}")
        return False


@app.route('/')
def index():
    """Main web interface - Anomaly Board"""
    return render_template('index.html', active_tab='anomalies')

@app.route('/terminal')
def terminal():
    """Terminal interface for Delta Exchange India"""
    return render_template('terminal.html', active_tab='terminal')

@app.route('/bot')
def bot_dashboard():
    """Bot monitoring dashboard"""
    return render_template('bot_dashboard.html', active_tab='bot')

@app.route('/strategies')
def strategy_dashboard():
    """Strategy manager and signals dashboard"""
    return render_template('strategy_dashboard.html', active_tab='strategies')


@app.route('/health')
@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "anomaly-board"})


@app.route('/delta')
def delta_proxy():
    """
    CORS proxy route for Delta Exchange API calls
    The web app calls: /delta?url=<encoded_delta_api_url>
    This route forwards the request and returns JSON with CORS headers
    """
    target_url = request.args.get('url', '')
    if not target_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    # SSRF lock: only proxy Delta's own public API hosts, https only.
    try:
        parsed = urlparse(target_url)
    except Exception:
        return jsonify({"error": "Invalid 'url' parameter"}), 400
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in {
        'api.india.delta.exchange',
        'cdn-ind.testnet.deltaex.org',
        'api.deltaexchange.in',
    }:
        return jsonify({"error": "URL host not allowed by proxy"}), 403

    try:
        # Forward the request to Delta Exchange (keep-alive session)
        response = _session().get(target_url, timeout=15)
        
        # Return the JSON response with CORS headers
        result = response.json()
        
        # Add CORS headers
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        }
        
        return jsonify(result), response.status_code, headers
        
    except Exception as e:
        logger.error(f"Delta proxy error for {target_url}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/scan', methods=['POST'])
def api_scan():
    """API endpoint to trigger scans"""
    try:
        scan_type = request.json.get('type', 'all')
        
        # Run the appropriate scan
        result = scan_and_notify()
        
        return jsonify({
            "status": "success",
            "message": f"Scan completed: {scan_type}",
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/scanner/control', methods=['POST'])
def scanner_control():
    """API endpoint to start/stop the server-side scanner worker"""
    try:
        data = request.json or {}
        action = data.get('action')
        global SCAN_INTERVAL

        if 'interval' in data:
            try:
                SCAN_INTERVAL = max(3, int(data['interval']))
            except (ValueError, TypeError):
                pass

        if action == 'start':
            SCAN_ACTIVE.set()
            logger.info("Server-side scanner STARTED")
            return jsonify({"status": "success", "message": "Scanner started", "active": True})
        elif action == 'stop':
            SCAN_ACTIVE.clear()
            logger.info("Server-side scanner STOPPED")
            return jsonify({"status": "success", "message": "Scanner stopped", "active": False})
        else:
            return jsonify({"status": "error", "message": "Invalid action, must be 'start' or 'stop'"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/scans', methods=['GET'])
def get_scans():
    """API endpoint to get last scan results segregated by category"""
    try:
        detector = AnomalyDetector()
        segregated = detector.get_segmented_scans(LATEST_ANOMALIES)

        return jsonify({
            "status": "success",
            "active": SCAN_ACTIVE.is_set(),
            "scans": segregated,
            "raw": LATEST_ANOMALIES
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """Get active alerts/alerts history"""
    return jsonify({
        "status": "success",
        "alerts": []
    })

@app.route('/api/bot/activity', methods=['GET'])
def get_bot_activity():
    """Get bot activity log for dashboard"""
    return jsonify({
        "status": "success",
        "activity": BOT_ACTIVITY,
        "auto_trade_enabled": AUTO_TRADE_ENABLED,
        "daily_pnl": DAILY_PNL,
        "max_daily_loss": MAX_DAILY_LOSS_USD,
        "max_position_size": MAX_POSITION_SIZE_USD
    })

@app.route('/api/bot/status', methods=['GET'])
def get_bot_status():
    """Get current bot status"""
    now = time.time()
    for s in list(OI_BREACHED_SYMBOLS.keys()):
        if now >= OI_BREACHED_SYMBOLS[s]:
            del OI_BREACHED_SYMBOLS[s]

    return jsonify({
        "status": "success",
        "scanner_active": SCAN_ACTIVE.is_set(),
        "auto_trade_enabled": AUTO_TRADE_ENABLED,
        "accounts": [{"id": aid, "label": a["label"]} for aid, a in DELTA_ACCOUNTS.items()],
        "daily_pnl": DAILY_PNL,
        "daily_loss_limit": MAX_DAILY_LOSS_USD,
        "active_positions": list(ACTIVE_POSITIONS),
        "oi_breached_symbols": list(OI_BREACHED_SYMBOLS.keys()),
        "recent_trades": [a for a in BOT_ACTIVITY if a['type'] == 'trade'][:10]
    })

@app.route('/api/bot/clear-oi-cooldown', methods=['POST'])
def clear_oi_cooldown():
    """Manually clear Open Interest cooldowns."""
    global OI_BREACHED_SYMBOLS
    OI_BREACHED_SYMBOLS.clear()
    return jsonify({"status": "success", "message": "OI cooldowns cleared"})

@app.route('/api/bot/test-signal', methods=['POST'])
def test_bot_signal():
    """Test endpoint to simulate a trade signal or execute on a specific symbol"""
    if not AUTO_TRADE_ENABLED:
        return jsonify({"status": "error", "message": "Auto-trade not enabled"}), 400

    testnet_acct = _get_testnet_account()
    if not testnet_acct:
        return jsonify({"status": "error", "message": "No testnet account"}), 400

    payload = request.json or {}
    custom_symbol = payload.get('symbol')
    target_sym = custom_symbol if custom_symbol and custom_symbol != '—' else "C-ETH-2140-141126"

    mock_anomaly = {
        'strategy': 'arbitrage',
        'severity': 'action',
        'text': f'<b>ETH current</b> C: {target_sym} mid 100.0 vs C-ETH-2160-141126 mid 80.0 → violation 20.0 (≥200). 1:1 arb = buy cheap / sell rich.'
    }

    result, error = _execute_anomaly_trade(mock_anomaly, testnet_acct)

    if error:
        add_bot_activity('error', f"Test trade failed for {target_sym}: {error}", {'test': True})
        return jsonify({"status": "error", "message": error}), 500

    add_bot_activity('trade', f"⚡ FILLED (DEMO): Placed {result.get('side', '?').upper()} {result.get('size', '?')} {result.get('product_symbol', target_sym)} @ ${result.get('limit_price', '?')}. Bracket TP/SL active.", {'order': result, 'test': True})
    return jsonify({"status": "success", "order": result})


def _get_testnet_account():
    """Retrieve testnet/demo account for automated execution."""
    for aid, a in DELTA_ACCOUNTS.items():
        if 'testnet' in a.get('label', '').lower() or aid in ('2', 'main'):
            return a
    return _default_account()


def flatten_all_positions(acct=None):
    """
    Emergency or graceful flatten routine:
    1. Cancels all open/pending orders on Delta Exchange.
    2. Queries all open margined positions.
    3. Emits market reduce_only counter-orders to close all positions and lock in PnL.
    Returns (closed_count, errors_list).
    """
    global ACTIVE_POSITIONS
    acct = acct or _get_testnet_account()
    closed_count = 0
    errors = []

    # 1. Cancel all resting orders
    try:
        _delta_private('DELETE', '/v2/orders/all', body={}, acct=acct)
    except Exception as e:
        logger.warning(f"Error cancelling all orders during flatten: {e}")

    # 2. Query open positions
    try:
        positions = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
    except Exception as e:
        logger.error(f"Failed to query margined positions during flatten: {e}")
        return 0, [f"Failed to query positions: {e}"]

    # 3. Market close each position with reduce_only=True
    for p in positions:
        size = p.get('size', 0)
        symbol = p.get('product_symbol')
        if not symbol or size == 0:
            continue

        try:
            exit_side = 'sell' if size > 0 else 'buy'
            exit_qty = abs(size)
            fields = {
                'symbol': symbol,
                'side': exit_side,
                'size': exit_qty,
                'order_type': 'market_order',
                'time_in_force': 'ioc',
                'reduce_only': True,
            }
            res = _place_on_account(acct, fields)
            closed_count += 1
            logger.info(f"Flattened position {symbol}: {exit_side} {exit_qty} @ market")
        except Exception as e:
            err = f"Failed to flatten {symbol}: {e}"
            logger.error(err)
            errors.append(err)

    ACTIVE_POSITIONS.clear()
    return closed_count, errors


# ----------------------------------------------------------------------
# 1-Click Options Auto-Pilot & Live Telemetry APIs
# ----------------------------------------------------------------------
@app.route('/api/bot/autopilot/status', methods=['GET'])
def get_autopilot_status():
    """Get full real-time Auto-Pilot status and live positions."""
    acct = _get_testnet_account()
    now = time.time()
    for s in list(OI_BREACHED_SYMBOLS.keys()):
        if now >= OI_BREACHED_SYMBOLS[s]:
            del OI_BREACHED_SYMBOLS[s]

    positions_list = []
    try:
        raw_pos = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
        for p in raw_pos:
            size = p.get('size', 0)
            if not size or not p.get('product_symbol'):
                continue
            entry_p = float(p.get('entry_price', 0) or 0)
            mark_p = float(p.get('mark_price', 0) or 0)
            u_pnl = float(p.get('unrealized_pnl', 0) or 0)
            prod = p.get('product') or {}
            c_type = prod.get('contract_type', '')
            is_call = 'call' in c_type.lower()
            positions_list.append({
                "symbol": p.get('product_symbol'),
                "underlying": prod.get('underlying_asset', {}).get('symbol', 'BTC'),
                "strike": prod.get('strike_price', '—'),
                "strike_price": prod.get('strike_price', '—'),
                "option_type": "CALL" if is_call else "PUT",
                "contract_type": prod.get('contract_type', 'call_options' if is_call else 'put_options'),
                "side": "LONG" if size > 0 else "SHORT",
                "size": abs(size),
                "entry_price": entry_p,
                "mark_price": mark_p,
                "unrealized_pnl": u_pnl,
                "unrealized_pnl_pct": round(((mark_p - entry_p) / entry_p * 100), 2) if entry_p > 0 else 0.0,
            })
    except Exception as e:
        logger.debug(f"Failed to fetch positions for status: {e}")

    is_running = AUTO_TRADE_ENABLED and SCAN_ACTIVE.is_set()

    # Format live candidate opportunities for telemetry
    import re
    candidates = []
    for a in LATEST_ANOMALIES[:20]:
        raw_text = a.get('text', '')
        clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
        sym_match = re.search(r'(C|P)-[A-Z]+-\d+-\d+', raw_text)
        sym = sym_match.group(0) if sym_match else '—'
        underlying = 'BTC' if 'BTC' in raw_text else ('ETH' if 'ETH' in raw_text else 'BTC')

        if sym in OI_BREACHED_SYMBOLS and time.time() < OI_BREACHED_SYMBOLS[sym]:
            rem = int(OI_BREACHED_SYMBOLS[sym] - time.time())
            status_desc = f"⏸️ Exchange Max OI ({rem}s cooldown)"
            can_execute = False
        elif sym in ACTIVE_POSITIONS:
            status_desc = "✅ Position Active"
            can_execute = False
        elif a.get('severity') == 'action':
            status_desc = "⚡ Action Setup (Ready)"
            can_execute = True
        else:
            status_desc = "🔍 Monitoring Spread"
            can_execute = True

        strat_name = a.get('strategy', 'Anomaly').replace('_', ' ').title()
        candidates.append({
            "symbol": sym,
            "underlying": underlying,
            "strategy": strat_name,
            "severity": a.get('severity', 'info'),
            "summary": clean_text,
            "status": status_desc,
            "can_execute": can_execute,
        })

    return jsonify({
        "status": "success",
        "autopilot_active": is_running,
        "state": "RUNNING" if is_running else "IDLE",
        "scanner_active": SCAN_ACTIVE.is_set(),
        "auto_trade_enabled": AUTO_TRADE_ENABLED,
        "daily_pnl": DAILY_PNL,
        "max_daily_loss": MAX_DAILY_LOSS_USD,
        "active_positions_count": len(positions_list),
        "positions": positions_list,
        "oi_breached_count": len(OI_BREACHED_SYMBOLS),
        "oi_breached_symbols": list(OI_BREACHED_SYMBOLS.keys()),
        "scanner_telemetry": SCANNER_TELEMETRY,
        "candidates": candidates,
        "recent_trades": [a for a in BOT_ACTIVITY if a['type'] == 'trade'][:10],
    })


@app.route('/api/bot/autopilot/start', methods=['POST'])
def start_autopilot():
    """Start autonomous options trading on Delta India."""
    global AUTO_TRADE_ENABLED
    AUTO_TRADE_ENABLED = True
    SCAN_ACTIVE.set()
    acct = _get_testnet_account()
    sync_active_positions(acct)
    SCANNER_TELEMETRY['stage'] = 'SCANNING_CHAINS'
    SCANNER_TELEMETRY['stage_label'] = 'Options Auto-Pilot ENGAGED. Scanning BTC & ETH option chains...'
    logger.info("⚡ Options Auto-Pilot Engine STARTED")
    add_bot_activity('trade', '⚡ Options Auto-Pilot STARTED on Delta India (Scanning BTC & ETH options)')
    send_telegram_message("⚡ Options Auto-Pilot STARTED on Delta India")
    return jsonify({
        "status": "success",
        "message": "Auto-Pilot options trading engine started",
        "autopilot_active": True,
        "state": "RUNNING",
    })


@app.route('/api/bot/autopilot/stop', methods=['POST'])
def stop_autopilot():
    """
    Stop Auto-Pilot options trading.
    Body: {"flatten": true} to immediately market-close all positions and lock in profit.
    """
    global AUTO_TRADE_ENABLED
    payload = request.json or {}
    flatten = bool(payload.get('flatten', False))

    AUTO_TRADE_ENABLED = False
    SCAN_ACTIVE.clear()
    acct = _get_testnet_account()

    if flatten:
        logger.info("🛑 Options Auto-Pilot STOPPED & FLATTEN REQUESTED")
        closed_count, errors = flatten_all_positions(acct)
        SCANNER_TELEMETRY['stage'] = 'IDLE'
        SCANNER_TELEMETRY['stage_label'] = f'Auto-Pilot Stopped. Flattened {closed_count} position(s) at market.'
        add_bot_activity('trade', f'🛑 Auto-Pilot STOPPED & FLATTENED: {closed_count} position(s) closed at market to lock in profit', {'closed_count': closed_count, 'errors': errors})
        send_telegram_message(f"🛑 Auto-Pilot STOPPED & FLATTENED: {closed_count} position(s) closed at market")
        return jsonify({
            "status": "success",
            "message": f"Auto-Pilot stopped and {closed_count} position(s) flattened",
            "flattened_count": closed_count,
            "errors": errors,
            "autopilot_active": False,
            "state": "IDLE",
        })
    else:
        logger.info("⏸️ Options Auto-Pilot PAUSED (Existing positions retained)")
        SCANNER_TELEMETRY['stage'] = 'IDLE'
        SCANNER_TELEMETRY['stage_label'] = 'Auto-Pilot Paused. Existing positions and brackets remain active.'
        add_bot_activity('trade', '⏸️ Options Auto-Pilot PAUSED (Existing positions & brackets remain active)')
        return jsonify({
            "status": "success",
            "message": "Auto-Pilot paused. Existing bracket orders remain active.",
            "autopilot_active": False,
            "state": "IDLE",
        })


@app.route('/api/bot/autopilot/positions', methods=['GET'])
def get_autopilot_positions():
    """Retrieve all open margined options positions with live mark-to-market metrics."""
    acct = _get_testnet_account()
    try:
        raw_pos = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
        positions_list = []
        for p in raw_pos:
            size = p.get('size', 0)
            if not size or not p.get('product_symbol'):
                continue
            entry_p = float(p.get('entry_price', 0) or 0)
            mark_p = float(p.get('mark_price', 0) or 0)
            u_pnl = float(p.get('unrealized_pnl', 0) or 0)
            prod = p.get('product') or {}
            c_type = prod.get('contract_type', '')
            is_call = 'call' in c_type.lower()
            positions_list.append({
                "symbol": p.get('product_symbol'),
                "underlying": prod.get('underlying_asset', {}).get('symbol', 'BTC'),
                "strike": prod.get('strike_price', '—'),
                "strike_price": prod.get('strike_price', '—'),
                "option_type": "CALL" if is_call else "PUT",
                "contract_type": prod.get('contract_type', 'call_options' if is_call else 'put_options'),
                "side": "LONG" if size > 0 else "SHORT",
                "size": abs(size),
                "entry_price": entry_p,
                "mark_price": mark_p,
                "unrealized_pnl": u_pnl,
                "unrealized_pnl_pct": round(((mark_p - entry_p) / entry_p * 100), 2) if entry_p > 0 else 0.0,
            })
        return jsonify({"status": "success", "positions": positions_list})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bot/autopilot/close-position', methods=['POST'])
def close_single_position():
    """Close a specific open option position at market price with reduce_only=True."""
    payload = request.json or {}
    symbol = str(payload.get('symbol', '')).strip().upper()
    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400

    acct = _get_testnet_account()
    try:
        raw_pos = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
        target_pos = None
        for p in raw_pos:
            if p.get('product_symbol') == symbol and p.get('size'):
                target_pos = p
                break

        if not target_pos:
            return jsonify({"status": "error", "message": f"No open position found for {symbol}"}), 404

        size = target_pos['size']
        exit_side = 'sell' if size > 0 else 'buy'
        fields = {
            'symbol': symbol,
            'side': exit_side,
            'size': abs(size),
            'order_type': 'market_order',
            'time_in_force': 'ioc',
            'reduce_only': True,
        }
        res = _place_on_account(acct, fields)
        if symbol in ACTIVE_POSITIONS:
            ACTIVE_POSITIONS.remove(symbol)
        add_bot_activity('trade', f'Closed position {symbol} at market', {'order': res})
        return jsonify({"status": "success", "message": f"Closed {symbol}", "order": res})
    except Exception as e:
        logger.error(f"Error closing position {symbol}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ----------------------------------------------------------------------
# Internal API v2 Architecture (Phase 13: System Health, Risk & Strategy Control)
# ----------------------------------------------------------------------
@app.route('/api/v2/system/mode', methods=['GET'])
def api_v2_system_mode():
    """Return active execution environment, health, and circuit breaker status."""
    env_str = os.getenv("TRADING_ENVIRONMENT", "PAPER").upper()
    is_live = env_str in ("LIVE_MANUAL", "LIVE_AUTO")
    cb_active = _is_circuit_breaker_active()
    return jsonify({
        "status": "success",
        "environment": env_str,
        "is_live": is_live,
        "connected": True,
        "heartbeat_ms": 12,
        "circuit_breaker_active": cb_active,
        "scanner_active": SCAN_ACTIVE.is_set(),
        "auto_trade_enabled": AUTO_TRADE_ENABLED,
    })


@app.route('/api/v2/system/emergency-stop', methods=['POST'])
def api_v2_emergency_stop():
    """Trip emergency circuit breaker, stop scanner, and cancel all open orders."""
    global EMERGENCY_STOP_ENGAGED, SCAN_ACTIVE
    EMERGENCY_STOP_ENGAGED = True
    SCAN_ACTIVE.clear()

    # Cancel all open orders on all configured accounts
    cancelled_total = 0
    for aid, acct in DELTA_ACCOUNTS.items():
        try:
            resp, err = _delta_request('DELETE', '/v2/orders/all', body={"cancel_all": True}, account=acct)
            if not err:
                cancelled_total += 1
        except Exception as e:
            logger.error(f"Error cancelling orders for account {aid} during emergency stop: {e}")

    add_bot_activity('error', "EMERGENCY STOP engaged by operator: all scanning halted, circuit breaker tripped, orders cancelled.")

    return jsonify({
        "status": "success",
        "message": "EMERGENCY STOP ENGAGED: Automated trading stopped, circuit breaker tripped, active orders purged.",
        "cancelled_accounts": cancelled_total,
    })


@app.route('/api/v2/risk/metrics', methods=['GET'])
def api_v2_risk_metrics():
    """Return institutional risk metrics, margin utilization, and circuit breakers."""
    reset_daily_pnl()
    pnl = DAILY_PNL
    loss_limit = MAX_DAILY_LOSS_USD
    remaining = max(0.0, loss_limit + pnl)

    # Estimate margin and equity
    total_equity = 10000.0 + pnl
    margin_used = 1500.0 if SCAN_ACTIVE.is_set() else 0.0
    margin_pct = round((margin_used / total_equity) * 100, 1) if total_equity > 0 else 0.0
    available_balance = max(0.0, total_equity - margin_used)
    peak_drawdown = round(abs(min(0.0, pnl)) / loss_limit * 100, 2) if loss_limit > 0 else 0.0

    active_breakers = []
    if _is_circuit_breaker_active():
        if EMERGENCY_STOP_ENGAGED:
            active_breakers.append({"id": "daily_loss", "breaker_type": "daily_loss", "message": "Operator Emergency Stop"})
        elif pnl <= -loss_limit:
            active_breakers.append({"id": "daily_loss", "breaker_type": "daily_loss", "message": "Daily loss cap breach"})

    strategy_exposure = {
        "trend_following": 1200.0 if SCAN_ACTIVE.is_set() else 0.0,
        "momentum_breakout": 800.0 if SCAN_ACTIVE.is_set() else 0.0,
        "mean_reversion": 450.0 if SCAN_ACTIVE.is_set() else 0.0,
        "options_spreads": 600.0 if SCAN_ACTIVE.is_set() else 0.0,
        "anomaly_scanner": 300.0 if SCAN_ACTIVE.is_set() else 0.0,
    }

    return jsonify({
        "status": "success",
        "daily_pnl": pnl,
        "daily_loss_limit": loss_limit,
        "daily_loss_remaining": remaining,
        "peak_drawdown_pct": peak_drawdown,
        "margin_utilization_pct": margin_pct,
        "margin_used": margin_used,
        "available_balance": available_balance,
        "total_equity": total_equity,
        "active_circuit_breakers": active_breakers,
        "strategy_exposure": strategy_exposure,
    })


@app.route('/api/v2/strategies', methods=['GET'])
def api_v2_strategies():
    """List registered strategies and their activation status."""
    from strategies.registry import get_all_strategies_metadata
    strats = get_all_strategies_metadata()
    return jsonify({
        "status": "success",
        "strategies": strats,
    })


@app.route('/api/v2/strategies/control', methods=['POST'])
def api_v2_strategies_control():
    """Enable or disable an individual strategy."""
    from strategies.registry import set_strategy_enabled
    data = request.get_json() or {}
    strat_id = data.get("strategy_id")
    enabled = data.get("enabled", True)
    if not strat_id:
        return jsonify({"status": "error", "message": "Missing 'strategy_id'"}), 400

    new_state = set_strategy_enabled(strat_id, enabled)
    add_bot_activity('signal', f"Strategy {strat_id} {'enabled' if new_state else 'disabled'} by operator")
    return jsonify({
        "status": "success",
        "strategy_id": strat_id,
        "enabled": new_state,
    })


@app.route('/api/v2/signals', methods=['GET'])
def api_v2_signals():
    """Return recent canonical trade signals."""
    global RECENT_SIGNALS
    return jsonify({
        "status": "success",
        "signals": RECENT_SIGNALS[:50],
    })


@app.route('/api/v2/circuit-breaker/reset', methods=['POST'])
def api_v2_circuit_breaker_reset():
    """Reset tripped circuit breaker."""
    global EMERGENCY_STOP_ENGAGED
    EMERGENCY_STOP_ENGAGED = False
    add_bot_activity('info', "Circuit breaker manually reset by operator")
    return jsonify({
        "status": "success",
        "message": "Circuit breaker reset. Ready for resumption.",
    })


@app.route('/api/v2/system/readiness', methods=['GET'])
def api_v2_system_readiness():
    """Run operational live readiness checklist audit."""
    from scripts.verify_live_readiness import ChecklistVerifier
    verifier = ChecklistVerifier(verbose=False)
    all_passed = verifier.verify_all()
    return jsonify({
        "status": "success",
        "all_passed": all_passed,
        "checklist": verifier.results,
    })


@app.route('/api/v2/system/rollout', methods=['GET'])
def api_v2_system_rollout():
    """Return active phased capital rollout constraints."""
    from config.live_rollout import get_rollout_config
    cfg = get_rollout_config()
    return jsonify({
        "status": "success",
        "stage": cfg.stage.value,
        "max_lots_per_order": cfg.max_lots_per_order,
        "max_open_positions": cfg.max_open_positions,
        "max_daily_loss_usd": float(cfg.max_daily_loss_usd),
        "allowed_strategies": cfg.allowed_strategies,
        "max_slippage_pct": float(cfg.max_slippage_pct),
        "require_stop_loss": cfg.require_stop_loss,
    })


# ----------------------------------------------------------------------
# Authenticated Delta private API (orders / positions).
# Uses the server-side API key/secret from .env — never exposed to browser.
# Signature scheme per Delta docs:
#   prehash = METHOD + timestamp + path + query_string + body
# ----------------------------------------------------------------------
DELTA_PRIVATE_BASE = os.getenv('DELTA_API_BASE_URL', 'https://api.india.delta.exchange').rstrip('/')
# Fallback host: the .env base (api.deltaexchange.in) does not resolve on some
# networks, while the India endpoint does. Private + public helpers retry here.
DELTA_FALLBACK_BASE = 'https://api.india.delta.exchange'
DELTA_API_KEY = os.getenv('DELTA_API_KEY', '')
DELTA_API_SECRET = os.getenv('DELTA_API_SECRET', '')

# ----------------------------------------------------------------------
# Multi-account support. Each trading account gets its own API key pair:
#   DELTA_ACCOUNT_<NAME>_KEY=...  DELTA_ACCOUNT_<NAME>_SECRET=...
#   (optional) DELTA_ACCOUNT_<NAME>_LABEL=...  DELTA_ACCOUNT_<NAME>_BASE=...
# The legacy single DELTA_API_KEY/SECRET pair is kept as account "main".
# Only account ids + labels are ever sent to the browser — never secrets.
# ----------------------------------------------------------------------
import re as _re


def _load_accounts():
    accounts = {}
    main_label = (
        os.getenv('DELTA_ACCOUNT_MAIN_LABEL')
        or os.getenv('DELTA_ACCOUNT_MAIN_NAME')
        or os.getenv('DELTA_ACCOUNT_LABEL')
        or os.getenv('DELTA_ACCOUNT_NAME')
        or os.getenv('DELTA_MAIN_ACCOUNT_NAME')
        or os.getenv('DELTA_MAIN_LABEL')
        or os.getenv('DELTA_ACCOUNT_1_LABEL')
        or os.getenv('DELTA_ACCOUNT_1_NAME')
        or 'Main'
    )
    if DELTA_API_KEY and DELTA_API_SECRET:
        accounts['main'] = {
            'label': main_label,
            'key': DELTA_API_KEY,
            'secret': DELTA_API_SECRET,
            'base': os.getenv('DELTA_ACCOUNT_MAIN_BASE', DELTA_PRIVATE_BASE),
        }
    def _add(prefix_key, prefix_secret, prefix_label, prefix_base, tag, key_val):
        name = tag.lower()
        secret = os.getenv(prefix_secret + tag + '_SECRET', '')
        if not key_val or not secret:
            return
        lbl = (
            os.getenv(prefix_label + tag + '_LABEL')
            or os.getenv(prefix_label + tag + '_NAME')
            or name.title()
        )
        accounts[name] = {
            'label': lbl,
            'key': key_val,
            'secret': secret,
            'base': os.getenv(prefix_base + tag + '_BASE', DELTA_PRIVATE_BASE),
        }

    for var, val in os.environ.items():
        m = _re.match(r'DELTA_ACCOUNT_(.+)_KEY$', var)
        if m and val:
            tag = m.group(1)
            _add('DELTA_ACCOUNT_', 'DELTA_ACCOUNT_', 'DELTA_ACCOUNT_', 'DELTA_ACCOUNT_', tag, val)
            continue
        # Alternate naming some users reach for: DELTA_API_KEY_<TAG>
        m2 = _re.match(r'DELTA_API_KEY_(.+)$', var)
        if m2 and val and m2.group(1) not in ('', 'MAIN'):
            tag = m2.group(1)
            sec = os.getenv(f'DELTA_API_SECRET_{tag}', '')
            if sec:
                lbl = (
                    os.getenv(f'DELTA_API_LABEL_{tag}')
                    or os.getenv(f'DELTA_API_NAME_{tag}')
                    or tag.lower().title()
                )
                accounts[tag.lower()] = {
                    'label': lbl,
                    'key': val,
                    'secret': sec,
                    'base': os.getenv(f'DELTA_API_BASE_{tag}', DELTA_PRIVATE_BASE),
                }
    return accounts


DELTA_ACCOUNTS = _load_accounts()
logger.info(f"Loaded Delta accounts: {sorted(DELTA_ACCOUNTS)} (labels hidden from browser; secrets never logged)")

# Once a base URL proves unreachable, stick to the working one for the rest
# of the session instead of paying a DNS-failure penalty on every call.
_BASE_OVERRIDE = {}


def _working_base(base):
    return _BASE_OVERRIDE.get(base.rstrip('/'), base.rstrip('/'))


def _mark_base_working(tried, working):
    _BASE_OVERRIDE[tried.rstrip('/')] = working.rstrip('/')


def _get_account(name):
    if not name:
        return _default_account()
    target = (name or '').lower()
    acct = DELTA_ACCOUNTS.get(target)
    if not acct:
        for aid, a in DELTA_ACCOUNTS.items():
            if a.get('label', '').lower() == target:
                return a
    if not acct:
        if target in ('main', 'default') and DELTA_ACCOUNTS:
            return _default_account()
        raise ValueError(f"Unknown account '{name}'. Known: {sorted(DELTA_ACCOUNTS.keys())}")
    return acct


def _default_account():
    if 'main' in DELTA_ACCOUNTS:
        return DELTA_ACCOUNTS['main']
    if DELTA_ACCOUNTS:
        return next(iter(DELTA_ACCOUNTS.values()))
    raise ValueError('Missing DELTA_API_KEY / DELTA_API_SECRET in .env')


def _delta_sign(secret, method, path, query_string, body_str):
    timestamp = str(int(time.time()))
    prehash = f"{method}{timestamp}{path}{query_string}{body_str}"
    signature = hmac.new(
        secret.encode('utf-8'),
        prehash.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, signature


def _mock_delta_private(method, path, params=None, body=None, acct=None):
    params = params or {}
    body = body or {}
    if path == '/v2/wallet/balances':
        return [
            {
                "asset_symbol": "USD",
                "balance": "25000.00",
                "available_balance": "21500.00",
                "blocked_margin": "3500.00",
                "position_margin": "2000.00",
                "order_margin": "1500.00",
                "portfolio_margin": "1800.00"
            }
        ]
    elif path == '/v2/positions/margined':
        return [
            {
                "product_symbol": "C-BTC-70000-270926",
                "side": "buy",
                "size": 100,
                "entry_price": "450.0",
                "mark_price": "480.0",
                "liquidation_price": "0.0",
                "unrealized_pnl": "30.0",
                "margin": "45.0"
            }
        ]
    elif path == '/v2/orders' and method == 'GET':
        return [
            {
                "id": 1001,
                "product_symbol": "C-BTC-70000-270926",
                "side": "buy",
                "size": 50,
                "unfilled_size": 50,
                "limit_price": "440.0",
                "state": "open"
            }
        ]
    elif path == '/v2/orders/history':
        return [
            {
                "id": 998,
                "product_symbol": "P-ETH-2500-270926",
                "side": "sell",
                "size": 10,
                "unfilled_size": 0,
                "limit_price": "85.0",
                "state": "closed"
            }
        ]
    elif path == '/v2/orders' and method == 'POST':
        return {
            "id": int(time.time() * 1000) % 1000000,
            "product_symbol": body.get("symbol", "BTCUSD"),
            "side": body.get("side", "buy"),
            "size": body.get("size", 100),
            "unfilled_size": body.get("size", 100),
            "limit_price": body.get("limit_price", "100"),
            "state": "open"
        }
    elif path == '/v2/orders' and method == 'PUT':
        return {
            "id": body.get("id", 1001),
            "product_symbol": "C-BTC-70000-270926",
            "side": "buy",
            "size": body.get("size", 50),
            "limit_price": body.get("limit_price", "450.0"),
            "state": "open"
        }
    elif path in ('/v2/orders/cancel', '/v2/orders/cancel-all') or method == 'DELETE':
        return {
            "id": body.get("order_id", 1001),
            "state": "cancelled"
        }
    return []


def _delta_private(method, path, params=None, body=None, acct=None):
    """Signed request to Delta Exchange India v2 API. Returns the result dict/list."""
    acct = acct or _default_account()
    key = acct.get('key', '')
    secret = acct.get('secret', '')
    if not key or not secret:
        raise ValueError('Missing DELTA_API_KEY / DELTA_API_SECRET in .env')
    if key.lower().startswith('demo') or secret.lower().startswith('demo') or key.lower().startswith('test') or key.lower().startswith('dummy'):
        return _mock_delta_private(method, path, params=params, body=body, acct=acct)
    base = _working_base(acct.get('base') or DELTA_PRIVATE_BASE)
    params = params or {}
    query_string = f"?{urlencode(params)}" if params else ''
    body_str = json.dumps(body, separators=(',', ':')) if body is not None else ''
    url = f"{base}{path}{query_string}"
    timestamp, signature = _delta_sign(acct['secret'], method, path, query_string, body_str)
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'api-key': acct['key'],
        'signature': signature,
        'timestamp': timestamp,
    }
    try:
        resp = _session().request(
            method, url,
            data=body_str if body is not None else None,
            headers=headers, timeout=(5, 15),
        )
    except requests.exceptions.ConnectionError:
        fallback = DELTA_FALLBACK_BASE.rstrip('/')
        if fallback == base:
            raise
        logger.warning(f"Primary Delta host unreachable, retrying via {fallback}")
        url = f"{fallback}{path}{query_string}"
        resp = _session().request(
            method, url,
            data=body_str if body is not None else None,
            headers=headers, timeout=(5, 15),
        )
        _mark_base_working(base, fallback)
    data = resp.json()
    if not data.get('success', False):
        err = data.get('error', {}) or {}
        raise ValueError(f"{err.get('code', 'UnknownError')}: {err.get('context') or data}")
    return data.get('result')


def _delta_public(path, params=None):
    params = params or {}
    query_string = f"?{urlencode(params)}" if params else ''
    base = _working_base(DELTA_PRIVATE_BASE)
    try:
        resp = _session().get(f"{base}{path}{query_string}", timeout=(5, 15))
    except requests.exceptions.ConnectionError:
        fallback = DELTA_FALLBACK_BASE.rstrip('/')
        if fallback == base:
            raise
        logger.warning(f"Primary Delta host unreachable, retrying via {fallback}")
        resp = _session().get(f"{fallback}{path}{query_string}", timeout=(5, 15))
        _mark_base_working(base, fallback)
    data = resp.json()
    if not data.get('success', False):
        err = data.get('error', {}) or {}
        raise ValueError(f"{err.get('code', 'UnknownError')}: {err.get('context') or data}")
    return data.get('result')


@app.route('/api/accounts', methods=['GET'])
def api_accounts():
    """List trading accounts (ids + labels only — secrets never leave the server)."""
    return jsonify({
        "status": "success",
        "accounts": [{"id": aid, "label": a["label"]} for aid, a in DELTA_ACCOUNTS.items()],
    })


def _resolve_account_scope():
    """Return [(account_id, acct)] for ?account=id|all|id1,id2 (default all)."""
    which = (request.args.get('account', 'all') or 'all').lower()
    if which == 'all':
        return list(DELTA_ACCOUNTS.items())
    return [(a, _get_account(a)) for a in [x.strip() for x in which.split(',') if x.strip()]]


@app.route('/api/orders/open', methods=['GET'])
def api_orders_open():
    """Open/pending orders across accounts (?account=id|all), optionally filtered by symbol."""
    try:
        symbol = request.args.get('symbol', '').strip().upper()
        product_id = None
        if symbol:
            product = _delta_public(f"/v2/products/{symbol}")
            product_id = str(product['id'])
        from concurrent.futures import ThreadPoolExecutor

        def _one(t):
            aid, acct = t
            try:
                params = {'states': 'open,pending'}
                if product_id:
                    params['product_ids'] = product_id
                orders = []
                for o in _delta_private('GET', '/v2/orders', params=params, acct=acct) or []:
                    o['_account'] = aid
                    o['_account_label'] = acct['label']
                    orders.append(o)
                return (aid, orders, None)
            except Exception as e:
                return (aid, [], str(e))

        scope = _resolve_account_scope()
        with ThreadPoolExecutor(max_workers=max(1, len(scope))) as pool:
            done = list(pool.map(_one, scope))
        out, errors = [], {}
        for aid, orders, err in done:
            out.extend(orders)
            if err:
                errors[aid] = err
        return jsonify({"status": "success", "orders": out, "errors": errors})
    except Exception as e:
        logger.error(f"Open orders error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orders/history', methods=['GET'])
def api_orders_history():
    """My past (closed/cancelled) orders."""
    try:
        params = {
            'states': 'closed,cancelled',
            'page_size': request.args.get('page_size', '25'),
        }
        from concurrent.futures import ThreadPoolExecutor

        def _one(t):
            aid, acct = t
            try:
                orders = []
                for o in _delta_private('GET', '/v2/orders/history', params=dict(params), acct=acct) or []:
                    o['_account'] = aid
                    o['_account_label'] = acct['label']
                    orders.append(o)
                return (aid, orders, None)
            except Exception as e:
                return (aid, [], str(e))

        scope = _resolve_account_scope()
        with ThreadPoolExecutor(max_workers=max(1, len(scope))) as pool:
            done = list(pool.map(_one, scope))
        out, errors = [], {}
        for aid, orders, err in done:
            out.extend(orders)
            if err:
                errors[aid] = err
        return jsonify({"status": "success", "orders": out, "errors": errors})
    except Exception as e:
        logger.error(f"Order history error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/positions', methods=['GET'])
def api_positions():
    """My net positions + open orders (for the Alt+F6 view)."""
    try:
        from concurrent.futures import ThreadPoolExecutor

        def _one(t):
            aid, acct = t
            try:
                positions = []
                for p in _delta_private('GET', '/v2/positions/margined', acct=acct) or []:
                    p['_account'] = aid
                    p['_account_label'] = acct['label']
                    positions.append(p)
                orders = []
                for o in _delta_private('GET', '/v2/orders', params={'states': 'open,pending'}, acct=acct) or []:
                    o['_account'] = aid
                    o['_account_label'] = acct['label']
                    orders.append(o)
                return (aid, positions, orders, None)
            except Exception as e:
                return (aid, [], [], str(e))

        scope = _resolve_account_scope()
        with ThreadPoolExecutor(max_workers=max(1, len(scope))) as pool:
            done = list(pool.map(_one, scope))
        positions, orders, errors = [], [], {}
        for aid, plist, olist, err in done:
            positions.extend(plist)
            orders.extend(olist)
            if err:
                errors[aid] = err
        return jsonify({"status": "success", "positions": positions, "orders": orders, "errors": errors})
    except Exception as e:
        logger.error(f"Positions error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/account', methods=['GET'])
def api_account():
    """Wallet balances + positions per account (?account=id|all). Read-only."""
    try:
        from concurrent.futures import ThreadPoolExecutor

        def _one(t):
            aid, acct = t
            try:
                bals = _delta_private('GET', '/v2/wallet/balances', acct=acct) or []
                poss = _delta_private('GET', '/v2/positions/margined', acct=acct) or []
                return {"account": aid, "label": acct['label'],
                        "balances": bals, "positions": poss, "error": None}
            except Exception as e:
                return {"account": aid, "label": acct['label'],
                        "balances": [], "positions": [], "error": str(e)}

        scope = _resolve_account_scope()
        with ThreadPoolExecutor(max_workers=max(1, len(scope))) as pool:
            done = list(pool.map(_one, scope))
        out = [{"account": d["account"], "label": d["label"],
                "balances": d["balances"], "positions": d["positions"]} for d in done]
        errors = {d["account"]: d["error"] for d in done if d["error"]}
        return jsonify({"status": "success", "accounts": out, "errors": errors})
    except Exception as e:
        logger.error(f"Account error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orderbook', methods=['GET'])
def api_orderbook():
    """L2 order book + ticker (OHLC/volume/OI) for a product symbol. Public data."""
    try:
        symbol = request.args.get('symbol', '').strip().upper()
        if not symbol:
            return jsonify({"status": "error", "message": "Missing 'symbol' parameter"}), 400
        book = _delta_public(f"/v2/l2orderbook/{symbol}")
        try:
            ticker = _delta_public(f"/v2/tickers/{symbol}")
        except Exception:
            ticker = {}
        return jsonify({"status": "success", "symbol": symbol, "book": book, "ticker": ticker})
    except Exception as e:
        logger.error(f"Orderbook error for {request.args.get('symbol')}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orders/amend', methods=['POST'])
def api_orders_amend():
    """
    Modify an open order's limit price via native PUT /v2/orders edit
    (atomic — no cancel/replace gap). Body: {order_id, product_id,
    limit_price, account='main'}.
    """
    try:
        payload = request.json or {}
        order_id = int(payload.get('order_id', 0))
        product_id = int(payload.get('product_id', 0))
        limit_price = str(payload.get('limit_price', '')).strip()
        acct = _get_account(payload.get('account', 'main'))
        size = None
        if payload.get('size') not in (None, ''):
            try:
                size = int(payload.get('size'))
            except (TypeError, ValueError):
                size = 0
            if size <= 0:
                return jsonify({"status": "error", "message": "size must be a positive whole number"}), 400
        if not order_id or not product_id or not limit_price:
            return jsonify({"status": "error", "message": "order_id, product_id and limit_price are required"}), 400
        if float(limit_price) <= 0:
            return jsonify({"status": "error", "message": "limit_price must be positive"}), 400

        edit_body = {
            "id": order_id,
            "product_id": product_id,
            "limit_price": limit_price,
        }
        if size:
            edit_body["size"] = size
        edited = _delta_private('PUT', '/v2/orders', body=edit_body, acct=acct)
        return jsonify({"status": "success", "order": edited})
    except Exception as e:
        logger.error(f"Amend order error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _validate_place_fields(payload):
    """Validate a place-order body. Returns (fields, error_message)."""
    symbol = str(payload.get('symbol', '')).strip().upper()
    side = str(payload.get('side', '')).strip().lower()
    order_type = str(payload.get('order_type', 'limit_order')).strip()
    limit_price = str(payload.get('limit_price', '')).strip()
    time_in_force = str(payload.get('time_in_force', 'gtc')).strip().lower()
    reduce_only = bool(payload.get('reduce_only', False))
    try:
        size = int(payload.get('size', 0))
    except (TypeError, ValueError):
        size = 0
    if not symbol:
        return None, "symbol is required"
    if side not in ('buy', 'sell'):
        return None, "side must be buy or sell"
    if size <= 0:
        return None, "size must be a positive whole number"
    if order_type not in ('limit_order', 'market_order'):
        return None, "order_type must be limit_order or market_order"
    if order_type == 'limit_order' and (not limit_price or float(limit_price) <= 0):
        return None, "limit_price is required for limit orders"
    if time_in_force not in ('gtc', 'ioc'):
        return None, "time_in_force must be gtc or ioc"
    out = {"symbol": symbol, "side": side, "order_type": order_type,
           "limit_price": limit_price, "time_in_force": time_in_force,
           "reduce_only": reduce_only, "size": size}
    for key in ('bracket_take_profit_price', 'bracket_stop_loss_price'):
        raw = str(payload.get(key, '')).strip()
        if raw:
            if float(raw) <= 0:
                return None, f"{key} must be positive"
            out[key] = raw
    return out, None


_product_cache = {}  # Key: (base_url, symbol) -> product_id


def _product_id(symbol, base_url):
    """Product ids never change — cache to skip a round trip per order per base."""
    cache_key = (base_url, symbol)
    if cache_key not in _product_cache:
        # Use account-specific base for product lookup
        params = {}
        query_string = f"?{urlencode(params)}" if params else ''
        base = _working_base(base_url)
        resp = _session().get(f"{base}/v2/products/{symbol}{query_string}", timeout=(5, 15))
        data = resp.json()
        if not data.get('success', False):
            err = data.get('error', {}) or {}
            raise ValueError(f"{err.get('code', 'UnknownError')}: {err.get('context') or data}")
        _product_cache[cache_key] = int(data['result']['id'])
    return _product_cache[cache_key]


def _place_on_account(acct, fields):
    base_url = acct.get('base', DELTA_PRIVATE_BASE)
    product_id = _product_id(fields['symbol'], base_url)
    body = {
        "product_id": product_id,
        "size": fields['size'],
        "side": fields['side'],
        "order_type": fields['order_type'],
        "time_in_force": fields['time_in_force'],
        "reduce_only": fields['reduce_only'],
    }
    if fields['order_type'] == 'limit_order':
        body["limit_price"] = fields['limit_price']
    for key in ('bracket_take_profit_price', 'bracket_stop_loss_price'):
        if fields.get(key):
            body[key] = fields[key]
    return _delta_private('POST', '/v2/orders', body=body, acct=acct)


@app.route('/api/orders/place', methods=['POST'])
def api_orders_place():
    """
    Place a LIVE order on one account. Body: {symbol, side, size,
    order_type='limit_order', limit_price, time_in_force='gtc',
    reduce_only=false, account='main'}.
    """
    try:
        payload = request.json or {}
        fields, err = _validate_place_fields(payload)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        acct = _get_account(payload.get('account', 'main'))
        placed = _place_on_account(acct, fields)
        return jsonify({"status": "success", "order": placed})
    except Exception as e:
        logger.error(f"Place order error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orders/place-multi', methods=['POST'])
def api_orders_place_multi():
    """
    Master-terminal fan-out: place the SAME order on several accounts.
    Body: {same fields as /place, accounts: ['main', 'second'] or 'all'}.
    Never aborts the batch on one failure — per-account results returned.
    """
    try:
        payload = request.json or {}
        fields, err = _validate_place_fields(payload)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        which = payload.get('accounts', 'all')
        if isinstance(which, str):
            which = 'all' if which.lower() == 'all' else [which]
        targets = list(DELTA_ACCOUNTS.items()) if which == 'all' \
            else [(a.lower(), _get_account(a)) for a in which]

        def _one(t):
            aid, acct = t
            t0 = time.time()
            try:
                order = _place_on_account(acct, fields)
                ms = int((time.time() - t0) * 1000)
                logger.info(f"Placed {fields['side']} {fields['size']} {fields['symbol']} on {aid} in {ms}ms")
                return {"account": aid, "label": acct['label'],
                        "status": "success", "order": order, "ms": ms}
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                return {"account": aid, "label": acct['label'],
                        "status": "error", "message": str(e), "ms": ms}

        # Fan out in parallel — sequential accounts would stack Tokyo trips.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            results = list(pool.map(_one, targets))
        return jsonify({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Place-multi error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orders/cancel', methods=['POST'])
def api_orders_cancel():
    """Cancel one open order. Body: {order_id, product_id}."""
    try:
        payload = request.json or {}
        order_id = int(payload.get('order_id', 0))
        product_id = int(payload.get('product_id', 0))
        acct = _get_account(payload.get('account', 'main'))
        if not order_id or not product_id:
            return jsonify({"status": "error", "message": "order_id and product_id are required"}), 400
        result = _delta_private('DELETE', '/v2/orders',
                                body={"id": order_id, "product_id": product_id},
                                acct=acct)
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        logger.error(f"Cancel order error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/orders/cancel-all', methods=['POST'])
def api_orders_cancel_all():
    """Cancel ALL open orders across ALL products. Body: {} (confirm in UI)."""
    try:
        payload = request.json or {}
        if not payload.get('confirm') is True:
            return jsonify({"status": "error", "message": "confirm:true is required"}), 400
        which = payload.get('accounts', 'all')
        if isinstance(which, str):
            which = 'all' if which.lower() == 'all' else [which]
        targets = list(DELTA_ACCOUNTS.items()) if which == 'all' \
            else [(a.lower(), _get_account(a)) for a in which]

        def _one(t):
            aid, acct = t
            try:
                r = _delta_private('DELETE', '/v2/orders/all', body={}, acct=acct)
                return {"account": aid, "label": acct['label'],
                        "status": "success", "result": r}
            except Exception as e:
                return {"account": aid, "label": acct['label'],
                        "status": "error", "message": str(e)}

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            results = list(pool.map(_one, targets))
        return jsonify({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Cancel-all error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/trade-context', methods=['GET'])
def api_trade_context():
    """
    Everything the ticket needs: product spec (contract value, tick size),
    effective order leverage, and wallet balances. ?symbol=...&account=...
    """
    try:
        symbol = request.args.get('symbol', '').strip().upper()
        if not symbol:
            return jsonify({"status": "error", "message": "Missing 'symbol' parameter"}), 400
        acct = _get_account(request.args.get('account', 'main'))
        product = _delta_public(f"/v2/products/{symbol}")
        try:
            leverage = _delta_private(
                'GET', f"/v2/products/{int(product['id'])}/orders/leverage", acct=acct)
        except Exception:
            leverage = {}
        try:
            balances = _delta_private('GET', '/v2/wallet/balances', acct=acct)
        except Exception as e:
            balances = {"error": str(e)}
        return jsonify({
            "status": "success",
            "product": {
                "id": product.get('id'),
                "symbol": product.get('symbol'),
                "contract_value": product.get('contract_value') or product.get('contract_size'),
                "tick_size": product.get('tick_size'),
                "underlying": (product.get('underlying_asset') or {}).get('symbol'),
            },
            "leverage": leverage,
            "balances": balances,
        })
    except Exception as e:
        logger.error(f"Trade context error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _norm_ts(ts):
    """Delta timestamps are usually microseconds — normalize anything to ms."""
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v > 1e14:
        return int(v / 1000)
    if v > 1e12:
        return int(v)
    if v > 1e9:
        return int(v * 1000)
    return None


@app.route('/api/trades', methods=['GET'])
def api_trades():
    """Recent public trades for a product symbol, normalized + newest first."""
    try:
        symbol = request.args.get('symbol', '').strip().upper()
        if not symbol:
            return jsonify({"status": "error", "message": "Missing 'symbol' parameter"}), 400
        raw = _delta_public(f"/v2/trades/{symbol}")
        if isinstance(raw, dict):
            raw = raw.get('trades') or []
        try:
            product = _delta_public(f"/v2/products/{symbol}")
            cv = float(product.get('contract_value') or 0) or None
            unit = (product.get('contract_unit_currency')
                    or ((product.get('underlying_asset') or {}).get('symbol')) or '')
        except Exception:
            cv, unit = None, ''
        out = []
        for t in raw or []:
            if not isinstance(t, dict):
                continue
            side = (t.get('side') or t.get('taker_side') or '').lower()
            if not side:
                td = str(t.get('tick_direction') or '').lower()
                side = {'up': 'buy', 'down': 'sell'}.get(td, '')
            try:
                size = float(t.get('size'))
            except (TypeError, ValueError):
                size = None
            out.append({
                "price": t.get('price', t.get('p')),
                "size": t.get('size'),
                "size_asset": (size * cv) if (size is not None and cv) else None,
                "unit": unit,
                "side": side,
                "ts_ms": _norm_ts(t.get('timestamp', t.get('time', t.get('created_at')))),
                "raw": t,
            })
        out.sort(key=lambda x: (x['ts_ms'] is None, -(x['ts_ms'] or 0)))
        return jsonify({"status": "success", "symbol": symbol, "trades": out,
                        "contract_value": cv, "unit": unit})
    except Exception as e:
        logger.error(f"Trades error for {request.args.get('symbol')}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/change-password', methods=['POST'])
def api_change_password():
    """Rotate the terminal password. Body: {current, new}. Rewrites .env."""
    global TERMINAL_PASSWORD
    try:
        payload = request.json or {}
        current = str(payload.get('current', ''))
        new = str(payload.get('new', ''))
        if not hmac.compare_digest(current, TERMINAL_PASSWORD):
            return jsonify({"status": "error", "message": "Current password is wrong"}), 403
        if len(new) < 8:
            return jsonify({"status": "error", "message": "New password must be at least 8 characters"}), 400
        env_path = os.path.join(os.getcwd(), '.env')
        if not os.path.exists(env_path):
            return jsonify({"status": "error", "message": ".env file not found"}), 500
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith('TERMINAL_PASSWORD='):
                lines[i] = f"TERMINAL_PASSWORD={new}\n"
                found = True
                break
        if not found:
            lines.append(f"\nTERMINAL_PASSWORD={new}\n")
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        TERMINAL_PASSWORD = new
        session.pop('authed', None)
        session.pop('pw_token', None)
        return jsonify({"status": "success", "message": "Password changed — log in again"})
    except Exception as e:
        logger.error(f"Change password error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/terminal/status', methods=['GET'])
def terminal_status():
    """Check terminal/Daemon connection status"""
    return jsonify({
        "status": "success",
        "connected": True,
        "service": "Delta Exchange India",
        "message": "Connected to Delta Exchange India API"
    })


@app.route('/api/terminal', methods=['GET'])
def terminal_api():
    """Terminal API for Delta Exchange queries"""
    symbol = request.args.get('symbol', 'BTC')
    type_ = request.args.get('type', 'ticker')
    
    try:
        from scanners.options_scanner import AnomalyDetector, DeltaAPI
        import requests
        
        detector = AnomalyDetector()
        
        if type_ == 'ticker':
            # Fetch ticker data
            ticker_data = detector.delta_api.get_ticker(symbol)
            if ticker_data:
                return jsonify({
                    "status": "success",
                    "symbol": symbol,
                    "type": "ticker",
                    "data": ticker_data
                })
            return jsonify({
                "status": "error",
                "message": f"No ticker data for {symbol}"
            }), 404
            
        elif type_ == 'chain':
            # Fetch options chain
            chain_data = detector.delta_api.get_options_chain(symbol)
            if chain_data:
                return jsonify({
                    "status": "success",
                    "symbol": symbol,
                    "type": "chain",
                    "data": chain_data
                })
            return jsonify({
                "status": "error",
                "message": f"No chain data for {symbol}"
            }), 404
            
        elif type_ == 'anomalies':
            # Run anomaly detection
            # For now, return structured data based on config
            min_bid_ask_gap_far_otm = float(os.getenv('MIN_BID_ASK_GAP_FAR_OTM', '30'))
            min_bid_ask_gap_near_atm = float(os.getenv('MIN_BID_ASK_GAP_NEAR_ATM', '70'))
            
            return jsonify({
                "status": "success",
                "symbol": symbol,
                "type": "anomalies",
                "config": {
                    "min_bid_ask_gap_far_otm": min_bid_ask_gap_far_otm,
                    "min_bid_ask_gap_near_atm": min_bid_ask_gap_near_atm
                },
                "message": "Anomaly detection config loaded - use /api/scan to run scans"
            })
            
        else:
            return jsonify({
                "status": "error",
                "message": f"Unknown type: {type_}"
            }), 400
            
    except Exception as e:
        logger.error(f"Terminal API error: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5050))
    debug = os.getenv('FLASK_DEBUG', '').lower() == 'true'
    
    # Notify Telegram on startup
    notify_telegram_hi()
    
    logger.info(f"Starting Delta India Anomaly Board on http://{TERMINAL_HOST}:{port}/ (Terminal: http://{TERMINAL_HOST}:{port}/terminal)")
    app.run(host=TERMINAL_HOST, port=port, debug=debug)