"""
Phase 18 Tests: 1-Click Options Auto-Pilot & Live Telemetry Engine

Tests:
1. Autopilot status endpoint (state, active positions count, PnL)
2. Autopilot start trigger (enables auto-trade, activates scanner thread)
3. Autopilot stop (pause-only mode vs flatten-all profit-lock mode)
4. Autopilot live positions retrieval (Delta India options mapping)
5. 1-click single position market close with reduce_only=True
"""
import pytest
import time
from unittest.mock import patch, MagicMock

from app import (
    app,
    AUTO_TRADE_ENABLED,
    SCAN_ACTIVE,
    ACTIVE_POSITIONS,
    OI_BREACHED_SYMBOLS,
    _pw_token,
    get_terminal_password,
)


@pytest.fixture
def auth_client():
    """Flask test client with authenticated session."""
    with app.test_client() as client:
        pw = get_terminal_password()
        with client.session_transaction() as sess:
            sess['authed'] = True
            sess['pw_token'] = _pw_token(pw)
        yield client


class TestAutopilotEngine:
    """Verifies the 1-click options auto-pilot orchestration and profit flattening."""

    def test_autopilot_status_idle(self, auth_client, monkeypatch):
        import app as app_module
        app_module.AUTO_TRADE_ENABLED = False
        app_module.SCAN_ACTIVE.clear()

        # Mock Delta positions to return empty list
        monkeypatch.setattr(app_module, "_delta_private", lambda method, path, acct=None, body=None: [])

        res = auth_client.get('/api/bot/autopilot/status')
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert data["autopilot_active"] is False
        assert data["state"] == "IDLE"
        assert data["active_positions_count"] == 0
        assert isinstance(data["positions"], list)

    def test_autopilot_status_with_mocked_positions(self, auth_client, monkeypatch):
        import app as app_module
        app_module.AUTO_TRADE_ENABLED = True
        app_module.SCAN_ACTIVE.set()

        mock_positions = [
            {
                "product_symbol": "C-ETH-2140-141126",
                "size": 1,
                "entry_price": "12.50",
                "mark_price": "18.00",
                "unrealized_pnl": "5.50",
                "product": {
                    "contract_type": "call_options",
                    "strike_price": "2140",
                    "underlying_asset": {"symbol": "ETH"},
                }
            }
        ]
        monkeypatch.setattr(app_module, "_delta_private", lambda method, path, acct=None, body=None: mock_positions)

        res = auth_client.get('/api/bot/autopilot/status')
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert data["autopilot_active"] is True
        assert data["state"] == "RUNNING"
        assert data["active_positions_count"] == 1
        pos = data["positions"][0]
        assert pos["symbol"] == "C-ETH-2140-141126"
        assert pos["side"] == "LONG"
        assert pos["size"] == 1
        assert pos["strike"] == "2140"
        assert pos["entry_price"] == 12.50
        assert pos["mark_price"] == 18.00
        assert pos["unrealized_pnl"] == 5.50
        assert pos["unrealized_pnl_pct"] == 44.0

    def test_autopilot_start(self, auth_client, monkeypatch):
        import app as app_module
        app_module.AUTO_TRADE_ENABLED = False
        app_module.SCAN_ACTIVE.clear()

        monkeypatch.setattr(app_module, "sync_active_positions", lambda acct: None)
        monkeypatch.setattr(app_module, "send_telegram_message", lambda msg: True)

        res = auth_client.post('/api/bot/autopilot/start')
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert data["autopilot_active"] is True
        assert data["state"] == "RUNNING"
        assert app_module.AUTO_TRADE_ENABLED is True
        assert app_module.SCAN_ACTIVE.is_set()

    def test_autopilot_stop_pause_only(self, auth_client, monkeypatch):
        import app as app_module
        app_module.AUTO_TRADE_ENABLED = True
        app_module.SCAN_ACTIVE.set()

        flatten_called = False
        def mock_flatten(acct):
            nonlocal flatten_called
            flatten_called = True
            return 0, []
        monkeypatch.setattr(app_module, "flatten_all_positions", mock_flatten)

        res = auth_client.post('/api/bot/autopilot/stop', json={"flatten": False})
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert data["autopilot_active"] is False
        assert data["state"] == "IDLE"
        assert app_module.AUTO_TRADE_ENABLED is False
        assert not app_module.SCAN_ACTIVE.is_set()
        assert flatten_called is False

    def test_autopilot_stop_and_flatten_profit(self, auth_client, monkeypatch):
        import app as app_module
        app_module.AUTO_TRADE_ENABLED = True
        app_module.SCAN_ACTIVE.set()

        mock_positions = [
            {"product_symbol": "C-BTC-95000-141126", "size": 1},
            {"product_symbol": "P-ETH-2000-141126", "size": -2},
        ]
        placed_orders = []

        def mock_delta_private(method, path, acct=None, body=None):
            if path == '/v2/positions/margined':
                return mock_positions
            return {"success": True}

        def mock_place(acct, fields):
            placed_orders.append(fields)
            return {"id": "ord_flatten_123", "status": "filled"}

        monkeypatch.setattr(app_module, "_delta_private", mock_delta_private)
        monkeypatch.setattr(app_module, "_place_on_account", mock_place)
        monkeypatch.setattr(app_module, "send_telegram_message", lambda msg: True)

        res = auth_client.post('/api/bot/autopilot/stop', json={"flatten": True})
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert data["autopilot_active"] is False
        assert data["state"] == "IDLE"
        assert data["flattened_count"] == 2
        assert len(placed_orders) == 2

        # Verify market-close order fields
        btc_order = next(o for o in placed_orders if o['symbol'] == "C-BTC-95000-141126")
        assert btc_order['side'] == 'sell'  # Long closed with sell
        assert btc_order['size'] == 1
        assert btc_order['order_type'] == 'market_order'
        assert btc_order['reduce_only'] is True

        eth_order = next(o for o in placed_orders if o['symbol'] == "P-ETH-2000-141126")
        assert eth_order['side'] == 'buy'   # Short closed with buy
        assert eth_order['size'] == 2
        assert eth_order['order_type'] == 'market_order'
        assert eth_order['reduce_only'] is True

    def test_get_autopilot_positions_endpoint(self, auth_client, monkeypatch):
        import app as app_module
        mock_positions = [
            {
                "product_symbol": "P-BTC-88000-141126",
                "size": -1,
                "entry_price": "250.00",
                "mark_price": "180.00",
                "unrealized_pnl": "70.00",
                "product": {
                    "contract_type": "put_options",
                    "strike_price": "88000",
                    "underlying_asset": {"symbol": "BTC"},
                }
            }
        ]
        monkeypatch.setattr(app_module, "_delta_private", lambda method, path, acct=None, body=None: mock_positions)

        res = auth_client.get('/api/bot/autopilot/positions')
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert len(data["positions"]) == 1
        pos = data["positions"][0]
        assert pos["symbol"] == "P-BTC-88000-141126"
        assert pos["side"] == "SHORT"
        assert pos["option_type"] == "PUT"

    def test_close_single_position_success(self, auth_client, monkeypatch):
        import app as app_module
        mock_positions = [
            {"product_symbol": "C-ETH-2140-141126", "size": 1}
        ]
        placed = []

        monkeypatch.setattr(app_module, "_delta_private", lambda method, path, acct=None, body=None: mock_positions)
        monkeypatch.setattr(app_module, "_place_on_account", lambda acct, fields: placed.append(fields) or {"id": "123"})

        res = auth_client.post('/api/bot/autopilot/close-position', json={"symbol": "C-ETH-2140-141126"})
        assert res.status_code == 200
        data = res.json
        assert data["status"] == "success"
        assert len(placed) == 1
        assert placed[0]["symbol"] == "C-ETH-2140-141126"
        assert placed[0]["side"] == "sell"
        assert placed[0]["reduce_only"] is True

    def test_close_single_position_not_found(self, auth_client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "_delta_private", lambda method, path, acct=None, body=None: [])

        res = auth_client.post('/api/bot/autopilot/close-position', json={"symbol": "NONEXISTENT"})
        assert res.status_code == 404
        assert "No open position" in res.json["message"]

    def test_close_single_position_missing_symbol(self, auth_client):
        res = auth_client.post('/api/bot/autopilot/close-position', json={})
        assert res.status_code == 400
        assert "Symbol is required" in res.json["message"]


class TestAutonomousOptionsEngine:
    """Verifies quantitative calculations, ATM strike ranking, and autonomous execution."""

    def test_calculate_rsi(self):
        from trading.autonomous_options_engine import AutonomousOptionsEngine
        engine = AutonomousOptionsEngine()
        # Flat closes
        assert engine.calculate_rsi([100.0] * 20) == 100.0 or engine.calculate_rsi([100.0] * 20) == 50.0

        # Monotonically increasing closes (strong overbought)
        inc_closes = [100.0 + i for i in range(25)]
        rsi_high = engine.calculate_rsi(inc_closes)
        assert rsi_high > 80.0

        # Monotonically decreasing closes (strong oversold)
        dec_closes = [200.0 - i for i in range(25)]
        rsi_low = engine.calculate_rsi(dec_closes)
        assert rsi_low < 20.0

    def test_analyze_market_regime(self):
        from trading.autonomous_options_engine import AutonomousOptionsEngine
        engine = AutonomousOptionsEngine()

        # Mock candles where prices are above MA20
        mock_candles = {
            'c': [1900.0] * 10 + [1950.0 + i for i in range(15)]
        }
        mock_delta_pub = lambda path: mock_candles
        regime = engine.analyze_market_regime('ETH', mock_delta_pub)
        assert regime['bias'] == 'BULLISH'
        assert regime['underlying'] == 'ETH'
        assert regime['price'] > regime['ma20']

    def test_select_best_option_contract_bullish(self):
        from trading.autonomous_options_engine import AutonomousOptionsEngine
        engine = AutonomousOptionsEngine()

        mock_tickers = [
            # Call option, near ATM, tight spread
            {
                'symbol': 'C-ETH-1920-141126',
                'contract_type': 'call_options',
                'strike_price': '1920',
                'quotes': {'best_bid': '1.0', 'best_ask': '1.2'}
            },
            # Call option, far OTM (> 12%)
            {
                'symbol': 'C-ETH-2500-141126',
                'contract_type': 'call_options',
                'strike_price': '2500',
                'quotes': {'best_bid': '0.1', 'best_ask': '0.2'}
            },
            # Put option (should be ignored in Bullish regime)
            {
                'symbol': 'P-ETH-1900-141126',
                'contract_type': 'put_options',
                'strike_price': '1900',
                'quotes': {'best_bid': '2.0', 'best_ask': '2.2'}
            },
        ]

        best = engine.select_best_option_contract(
            underlying='ETH',
            bias='BULLISH',
            spot_price=1910.0,
            tickers=mock_tickers,
            active_symbols=set(),
            oi_breached_symbols={}
        )

        assert best is not None
        assert best['symbol'] == 'C-ETH-1920-141126'
        assert best['contract_type'] == 'call_options'
        assert best['limit_price'] == '1.2'
        # TP +40%: 1.2 * 1.4 = 1.68
        assert float(best['bracket_take_profit_price']) == 1.68
        # SL -25%: 1.2 * 0.75 = 0.90
        assert float(best['bracket_stop_loss_price']) == 0.90

    def test_evaluate_and_execute_trade_position_cap(self):
        from trading.autonomous_options_engine import AutonomousOptionsEngine
        engine = AutonomousOptionsEngine(max_open_positions=1)

        active = {'C-ETH-1900-141126'}
        code, order, msg = engine.evaluate_and_execute_trade(
            acct={},
            active_positions_set=active,
            oi_breached_symbols={},
            daily_pnl=0.0,
            delta_public_fn=lambda p: {},
            delta_private_fn=lambda m, p, **kw: {},
            place_order_fn=lambda a, f: {}
        )
        assert code == 'POSITIONS_FULL'
        assert order is None
        assert 'Position limit active' in msg

    def test_evaluate_and_execute_trade_loss_cap(self):
        from trading.autonomous_options_engine import AutonomousOptionsEngine
        engine = AutonomousOptionsEngine(max_daily_loss_usd=50.0)

        code, order, msg = engine.evaluate_and_execute_trade(
            acct={},
            active_positions_set=set(),
            oi_breached_symbols={},
            daily_pnl=-55.0,  # Exceeds $50 loss limit
            delta_public_fn=lambda p: {},
            delta_private_fn=lambda m, p, **kw: {},
            place_order_fn=lambda a, f: {}
        )
        assert code == 'HALTED_LOSS_CAP'
        assert order is None
        assert 'Daily loss cap reached' in msg

