"""
Phase 13 Tests: Dashboard, User Experience, and Telemetry

Tests:
1. Auth enforcement on /api/v2 endpoints
2. System Mode & Telemetry endpoint (/api/v2/system/mode)
3. Emergency Stop endpoint (/api/v2/system/emergency-stop) halts scanning and trips breaker
4. Circuit Breaker manual reset endpoint (/api/v2/circuit-breaker/reset)
5. Risk Metrics endpoint (/api/v2/risk/metrics)
6. Strategy Manager endpoints (/api/v2/strategies and /api/v2/strategies/control)
7. Canonical Signal Feed endpoint (/api/v2/signals)
8. Page route rendering: /strategies, /bot, /terminal, /
9. Static assets verification (health_monitor.js, risk_ui.js)
"""
import os
import json
import pytest
from unittest.mock import patch

from app import app, _pw_token, get_terminal_password
from strategies.registry import is_strategy_enabled, set_strategy_enabled


@pytest.fixture
def client():
    """Flask test client."""
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key-phase13'
    with app.test_client() as c:
        yield c


@pytest.fixture
def authed_client(client):
    """Authenticated client with valid session token."""
    pw = get_terminal_password()
    with client.session_transaction() as sess:
        sess['authed'] = True
        sess['pw_token'] = _pw_token(pw)
    return client


# ──────────────────────────────────────────────
# 1. Security & Authentication Checks
# ──────────────────────────────────────────────
class TestAuthEnforcement:
    def test_unauthenticated_api_v2_access_blocked(self, client):
        endpoints = [
            ('/api/v2/system/mode', 'GET'),
            ('/api/v2/risk/metrics', 'GET'),
            ('/api/v2/strategies', 'GET'),
            ('/api/v2/signals', 'GET'),
            ('/api/v2/system/emergency-stop', 'POST'),
            ('/api/v2/strategies/control', 'POST'),
            ('/api/v2/circuit-breaker/reset', 'POST'),
        ]
        for path, method in endpoints:
            if method == 'GET':
                res = client.get(path)
            else:
                res = client.post(path, json={})
            assert res.status_code == 401, f"Path {path} should be 401 unauthenticated"
            data = res.get_json()
            assert data['status'] == 'error'
            assert 'Not authenticated' in data['message']


# ──────────────────────────────────────────────
# 2. System Mode & Emergency Stop
# ──────────────────────────────────────────────
class TestSystemModeAndEmergencyStop:
    def test_get_system_mode(self, authed_client):
        res = authed_client.get('/api/v2/system/mode')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        assert 'environment' in data
        assert isinstance(data['is_live'], bool)
        assert data['connected'] is True
        assert 'heartbeat_ms' in data
        assert 'circuit_breaker_active' in data

    def test_emergency_stop_lifecycle(self, authed_client):
        # 1. Trigger emergency stop
        res = authed_client.post('/api/v2/system/emergency-stop')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        assert 'EMERGENCY STOP ENGAGED' in data['message']

        # 2. Verify circuit breaker is now active in system mode
        mode_res = authed_client.get('/api/v2/system/mode')
        mode_data = mode_res.get_json()
        assert mode_data['circuit_breaker_active'] is True

        # 3. Reset circuit breaker
        reset_res = authed_client.post('/api/v2/circuit-breaker/reset')
        assert reset_res.status_code == 200
        reset_data = reset_res.get_json()
        assert reset_data['status'] == 'success'

        # 4. Verify circuit breaker is cleared
        mode_res2 = authed_client.get('/api/v2/system/mode')
        mode_data2 = mode_res2.get_json()
        assert mode_data2['circuit_breaker_active'] is False


# ──────────────────────────────────────────────
# 3. Risk Metrics Telemetry
# ──────────────────────────────────────────────
class TestRiskMetricsEndpoint:
    def test_get_risk_metrics(self, authed_client):
        res = authed_client.get('/api/v2/risk/metrics')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        assert 'daily_pnl' in data
        assert 'daily_loss_limit' in data
        assert 'daily_loss_remaining' in data
        assert 'margin_utilization_pct' in data
        assert 'margin_used' in data
        assert 'available_balance' in data
        assert 'total_equity' in data
        assert 'strategy_exposure' in data
        assert isinstance(data['strategy_exposure'], dict)


# ──────────────────────────────────────────────
# 4. Strategy Manager & Control
# ──────────────────────────────────────────────
class TestStrategyManagerEndpoints:
    def test_list_strategies(self, authed_client):
        res = authed_client.get('/api/v2/strategies')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        strats = data['strategies']
        assert len(strats) >= 5
        strat_ids = [s['strategy_id'] for s in strats]
        assert 'trend_following' in strat_ids
        assert 'momentum_breakout' in strat_ids
        assert 'mean_reversion' in strat_ids
        assert 'options_spreads' in strat_ids
        assert 'anomaly_scanner' in strat_ids

    def test_toggle_strategy_activation(self, authed_client):
        # Disable trend following
        res = authed_client.post('/api/v2/strategies/control', json={
            'strategy_id': 'trend_following',
            'enabled': False
        })
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        assert data['enabled'] is False
        assert is_strategy_enabled('trend_following') is False

        # Re-enable trend following
        res2 = authed_client.post('/api/v2/strategies/control', json={
            'strategy_id': 'trend_following',
            'enabled': True
        })
        assert res2.status_code == 200
        assert res2.get_json()['enabled'] is True
        assert is_strategy_enabled('trend_following') is True

    def test_toggle_strategy_missing_id(self, authed_client):
        res = authed_client.post('/api/v2/strategies/control', json={})
        assert res.status_code == 400
        assert res.get_json()['status'] == 'error'


# ──────────────────────────────────────────────
# 5. Signal Feed & UI Pages
# ──────────────────────────────────────────────
class TestSignalsAndPageRoutes:
    def test_get_signals(self, authed_client):
        res = authed_client.get('/api/v2/signals')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'success'
        assert isinstance(data['signals'], list)
        if len(data['signals']) > 0:
            sig = data['signals'][0]
            assert 'symbol' in sig
            assert 'direction' in sig
            assert 'strategy_id' in sig

    def test_page_routes_render(self, authed_client):
        routes = ['/', '/terminal', '/bot', '/strategies']
        for route in routes:
            res = authed_client.get(route)
            assert res.status_code == 200, f"Route {route} failed to render"
            html = res.data.decode('utf-8')
            assert 'DELTA INDIA' in html or 'Delta India' in html

    def test_static_assets_exist(self):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        hm_path = os.path.join(base_dir, 'static', 'js', 'health_monitor.js')
        ru_path = os.path.join(base_dir, 'static', 'js', 'risk_ui.js')
        layout_path = os.path.join(base_dir, 'templates', 'layout.html')
        strat_path = os.path.join(base_dir, 'templates', 'strategy_dashboard.html')

        assert os.path.exists(hm_path), "health_monitor.js must exist"
        assert os.path.exists(ru_path), "risk_ui.js must exist"
        assert os.path.exists(layout_path), "layout.html must exist"
        assert os.path.exists(strat_path), "strategy_dashboard.html must exist"
