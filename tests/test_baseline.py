"""
Baseline regression tests for Delta India Terminal.
Covers login throttle, proxy SSRF protection, HMAC signature calculation, and order validation.
"""
import os
import sys
import pytest
from unittest.mock import Mock, patch, MagicMock
import hmac
import hashlib
import json
import time
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from scanners.options_scanner import DeltaAPI, AnomalyDetector


@pytest.fixture(autouse=True)
def setup_test_env():
    """Set up test environment before each test."""
    os.environ['TERMINAL_PASSWORD'] = 'horizon'
    yield
    if 'TERMINAL_PASSWORD' in os.environ:
        del os.environ['TERMINAL_PASSWORD']


class TestLoginThrottle:
    """Test IP-based brute-force throttle: 5 attempts → 5 min lockout."""

    def setup_method(self):
        app._login_attempts.clear()

    def test_login_locked_after_5_failures(self):
        """After 5 failed attempts, IP should be locked for ~300 seconds."""
        ip = '192.168.1.100'

        # 5 failed attempts
        for i in range(5):
            wait = app._login_locked(ip)
            assert wait == 0, f"Attempt {i+1}: should not be locked yet"
            rec = app._login_attempts.setdefault(ip, {'fails': 0, 'until': 0})
            rec['fails'] += 1
            if rec['fails'] >= 5:
                rec['until'] = time.time() + 300

        # 6th attempt should be locked
        wait = app._login_locked(ip)
        assert wait > 0, "Should be locked after 5 failures"
        assert wait <= 300, "Lockout should be ~300 seconds"

    def test_login_lockout_expires(self):
        """Lockout should expire after time passes."""
        ip = '192.168.1.101'
        # Use a larger time buffer to avoid edge cases
        future_time = time.time() + 10
        rec = app._login_attempts[ip] = {'fails': 5, 'until': future_time}

        # Immediately locked
        wait = app._login_locked(ip)
        assert wait > 0, f"Should be locked, got wait={wait}"

        # After expiry (mock time)
        rec['until'] = time.time() - 10
        wait = app._login_locked(ip)
        assert wait == 0, "Lock should expire"
        assert ip not in app._login_attempts, "Record should be cleaned up"


class TestProxySSRFProtection:
    """Test SSRF proxy at /delta whitelists only allowed Delta domains."""

    @pytest.fixture
    def client(self):
        app.app.config['TESTING'] = True
        app.app.config['SECRET_KEY'] = 'test-secret'
        with app.app.test_client() as client:
            yield client

    def _login(self, client):
        """Helper to log in and get authenticated session."""
        with client.session_transaction() as sess:
            pw = 'horizon'  # Match the .env file
            sess['authed'] = True
            sess['pw_token'] = hashlib.sha256(pw.encode('utf-8')).hexdigest()

    def test_proxy_allows_india_delta_exchange(self, client):
        """api.india.delta.exchange should be allowed."""
        self._login(client)
        with patch('app._session') as mock_session:
            mock_resp = Mock()
            mock_resp.json.return_value = {'success': True, 'result': []}
            mock_resp.status_code = 200
            mock_session.return_value.get.return_value = mock_resp

            response = client.get('/delta?url=https://api.india.delta.exchange/v2/tickers/BTCUSD')
            assert response.status_code == 200

    def test_proxy_allows_testnet_cdn(self, client):
        """cdn-ind.testnet.deltaex.org should be allowed."""
        self._login(client)
        with patch('app._session') as mock_session:
            mock_resp = Mock()
            mock_resp.json.return_value = {'success': True, 'result': []}
            mock_resp.status_code = 200
            mock_session.return_value.get.return_value = mock_resp

            response = client.get('/delta?url=https://cdn-ind.testnet.deltaex.org/v2/tickers/BTCUSD')
            assert response.status_code == 200

    def test_proxy_allows_api_deltaexchange_in(self, client):
        """api.deltaexchange.in should be allowed."""
        self._login(client)
        with patch('app._session') as mock_session:
            mock_resp = Mock()
            mock_resp.json.return_value = {'success': True, 'result': []}
            mock_resp.status_code = 200
            mock_session.return_value.get.return_value = mock_resp

            response = client.get('/delta?url=https://api.deltaexchange.in/v2/tickers/BTCUSD')
            assert response.status_code == 200

    def test_proxy_blocks_non_delta_domains(self, client):
        """Non-Delta domains should be rejected with 403."""
        self._login(client)
        response = client.get('/delta?url=https://evil.com/api')
        assert response.status_code == 403
        data = response.get_json()
        assert 'not allowed' in data['error'].lower()

    def test_proxy_blocks_http(self, client):
        """HTTP (non-HTTPS) should be rejected."""
        self._login(client)
        response = client.get('/delta?url=http://api.india.delta.exchange/v2/tickers/BTCUSD')
        assert response.status_code == 403

    def test_proxy_blocks_malformed_url(self, client):
        """Malformed URLs should be rejected (urlparse fails)."""
        self._login(client)
        response = client.get('/delta?url=not-a-url')
        # urlparse doesn't fail on 'not-a-url', it treats it as a path with no scheme/host
        # So it passes the try/except but fails the hostname check
        assert response.status_code == 403
        data = response.get_json()
        assert 'not allowed' in data['error'].lower()


class TestHMACSigning:
    """Test Delta v2 HMAC-SHA256 signature calculation."""

    def test_delta_sign_format(self):
        """Signature should match Delta v2 spec: METHOD + timestamp + path + query + body."""
        secret = 'test-secret'
        method = 'POST'
        path = '/v2/orders'
        query_string = '?product_id=123'
        body_str = '{"size": 10, "side": "buy"}'

        timestamp, signature = app._delta_sign(secret, method, path, query_string, body_str)

        # Verify timestamp is valid
        assert timestamp.isdigit()
        assert len(timestamp) == 10  # Unix timestamp

        # Verify signature format (hex)
        assert len(signature) == 64  # SHA256 hex

        # Verify by recomputing
        prehash = f"{method}{timestamp}{path}{query_string}{body_str}"
        expected = hmac.new(
            secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        assert signature == expected

    def test_delta_sign_get_request(self):
        """GET request with empty body should work."""
        secret = 'test-secret'
        method = 'GET'
        path = '/v2/positions/margined'
        query_string = ''
        body_str = ''

        timestamp, signature = app._delta_sign(secret, method, path, query_string, body_str)
        assert len(signature) == 64

        prehash = f"{method}{timestamp}{path}{query_string}{body_str}"
        expected = hmac.new(
            secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        assert signature == expected


class TestOrderValidation:
    """Test order field validation."""

    def test_validate_place_fields_valid_limit_order(self):
        """Valid limit order should pass validation."""
        payload = {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'order_type': 'limit_order',
            'limit_price': '50000',
            'size': 1,
            'time_in_force': 'gtc',
        }
        fields, err = app._validate_place_fields(payload)
        assert err is None
        assert fields['symbol'] == 'BTCUSD'
        assert fields['side'] == 'buy'
        assert fields['size'] == 1

    def test_validate_place_fields_valid_market_order(self):
        """Valid market order (no limit_price required) should pass."""
        payload = {
            'symbol': 'BTCUSD',
            'side': 'sell',
            'order_type': 'market_order',
            'size': 2,
            'time_in_force': 'ioc',
        }
        fields, err = app._validate_place_fields(payload)
        assert err is None
        assert fields['order_type'] == 'market_order'

    def test_validate_place_fields_rejects_missing_symbol(self):
        """Missing symbol should be rejected."""
        payload = {'side': 'buy', 'size': 1, 'order_type': 'limit_order', 'limit_price': '50000'}
        fields, err = app._validate_place_fields(payload)
        assert err is not None
        assert 'symbol' in err.lower()

    def test_validate_place_fields_rejects_invalid_side(self):
        """Invalid side should be rejected."""
        payload = {'symbol': 'BTCUSD', 'side': 'hold', 'size': 1, 'order_type': 'limit_order', 'limit_price': '50000'}
        fields, err = app._validate_place_fields(payload)
        assert err is not None
        assert 'side' in err.lower()

    def test_validate_place_fields_rejects_zero_size(self):
        """Zero or negative size should be rejected."""
        payload = {'symbol': 'BTCUSD', 'side': 'buy', 'size': 0, 'order_type': 'limit_order', 'limit_price': '50000'}
        fields, err = app._validate_place_fields(payload)
        assert err is not None
        assert 'size' in err.lower()

    def test_validate_place_fields_rejects_limit_order_without_price(self):
        """Limit order without limit_price should be rejected."""
        payload = {'symbol': 'BTCUSD', 'side': 'buy', 'size': 1, 'order_type': 'limit_order'}
        fields, err = app._validate_place_fields(payload)
        assert err is not None
        assert 'limit_price' in err.lower()

    def test_validate_place_fields_accepts_brackets(self):
        """Bracket orders (SL/TP) should be accepted."""
        payload = {
            'symbol': 'BTCUSD',
            'side': 'buy',
            'size': 1,
            'order_type': 'limit_order',
            'limit_price': '50000',
            'bracket_stop_loss_price': '49000',
            'bracket_take_profit_price': '51000',
        }
        fields, err = app._validate_place_fields(payload)
        assert err is None
        assert 'bracket_stop_loss_price' in fields
        assert 'bracket_take_profit_price' in fields

    def test_validate_place_fields_rejects_negative_bracket_price(self):
        """Negative bracket prices should be rejected."""
        payload = {
            'symbol': 'BTCUSD', 'side': 'buy', 'size': 1,
            'order_type': 'limit_order', 'limit_price': '50000',
            'bracket_stop_loss_price': '-100',
        }
        fields, err = app._validate_place_fields(payload)
        assert err is not None
        assert 'stop_loss' in err.lower()


class TestDeltaAPI:
    """Test DeltaAPI methods exist and handle errors gracefully."""

    @patch('scanners.options_scanner.requests.Session.get')
    def test_get_ticker_returns_result_on_success(self, mock_get):
        """get_ticker should return result dict on success."""
        mock_resp = Mock()
        mock_resp.json.return_value = {'success': True, 'result': {'symbol': 'BTCUSD', 'price': '50000'}}
        mock_get.return_value = mock_resp

        api = DeltaAPI()
        result = api.get_ticker('BTCUSD')

        assert result is not None
        assert result['symbol'] == 'BTCUSD'
        mock_get.assert_called_once()

    @patch('scanners.options_scanner.requests.Session.get')
    def test_get_ticker_returns_none_on_failure(self, mock_get):
        """get_ticker should return None on API failure."""
        mock_resp = Mock()
        mock_resp.json.return_value = {'success': False, 'error': {'code': 'NotFound'}}
        mock_get.return_value = mock_resp

        api = DeltaAPI()
        result = api.get_ticker('INVALID')

        assert result is None

    @patch('scanners.options_scanner.requests.Session.get')
    def test_get_options_chain_returns_list(self, mock_get):
        """get_options_chain should return list of products."""
        mock_resp = Mock()
        mock_resp.json.return_value = {'success': True, 'result': [{'symbol': 'C-BTC-50000'}, {'symbol': 'P-BTC-50000'}]}
        mock_get.return_value = mock_resp

        api = DeltaAPI()
        result = api.get_options_chain('BTC')

        assert isinstance(result, list)
        assert len(result) == 2

    @patch('scanners.options_scanner.requests.Session.get')
    def test_get_tickers_returns_list(self, mock_get):
        """get_tickers should return list of tickers."""
        mock_resp = Mock()
        mock_resp.json.return_value = {'success': True, 'result': [{'symbol': 'BTCUSD'}]}
        mock_get.return_value = mock_resp

        api = DeltaAPI()
        result = api.get_tickers('BTC')

        assert isinstance(result, list)


class TestAnomalyDetector:
    """Test AnomalyDetector basic functionality."""

    @patch.object(DeltaAPI, 'get_products')
    @patch.object(DeltaAPI, 'get_tickers')
    def test_scan_returns_list(self, mock_tickers, mock_products):
        """scan should return list of anomalies."""
        mock_products.return_value = [
            {'symbol': 'C-BTC-50000-270926', 'settlement_time': '2026-09-27T00:00:00Z',
             'underlying_asset': {'symbol': 'BTC'}, 'contract_type': 'call_options',
             'strike_price': '50000'},
            {'symbol': 'P-BTC-50000-270926', 'settlement_time': '2026-09-27T00:00:00Z',
             'underlying_asset': {'symbol': 'BTC'}, 'contract_type': 'put_options',
             'strike_price': '50000'},
        ]
        mock_tickers.return_value = [
            {'symbol': 'C-BTC-50000-270926', 'contract_type': 'call_options',
             'strike_price': '50000', 'quotes': {'best_bid': '100', 'best_ask': '200'},
             'close': '150', 'spot_price': '50000'},
            {'symbol': 'P-BTC-50000-270926', 'contract_type': 'put_options',
             'strike_price': '50000', 'quotes': {'best_bid': '80', 'best_ask': '180'},
             'close': '130', 'spot_price': '50000'},
        ]

        detector = AnomalyDetector()
        anomalies = detector.scan(underlyings=['BTC'])

        assert isinstance(anomalies, list)

    def test_get_segmented_scans_returns_dict(self):
        """get_segmented_scans should return dict with all strategy keys."""
        detector = AnomalyDetector()
        segregated = detector.get_segmented_scans([])

        expected_keys = {
            'arbitrage', 'fat_finger', 'liquidity_gap',
            'butterfly', 'calendar', 'credit_spread_eth', 'credit_spread_btc'
        }
        assert set(segregated.keys()) == expected_keys
        assert all(isinstance(v, list) for v in segregated.values())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])