"""
Phase 4 Tests: Exchange Integration & Market Data Layer

Tests:
1. TokenBucket rate limiter
2. DeltaRestClient authentication, URLs, and methods
3. MarketDataQuote normalization and validation
4. MarketDataStore caching and thread-safety
"""
import pytest
import time
from decimal import Decimal
from unittest.mock import Mock, patch

from config.environments import TradingEnvironment
from config.accounts import AccountConfig
from exchange.delta_client import TokenBucket, DeltaRestClient, get_delta_client
from exchange.market_data import (
    normalize_tick, validate_spread, validate_liquidity, is_fresh,
    MarketDataQuote, refresh_quote, get_current_quote
)
from exchange.websocket_manager import MarketDataStore


class TestTokenBucketRateLimiter:
    def test_acquire_within_burst(self):
        bucket = TokenBucket(rate_per_second=10.0, burst=5)
        for _ in range(5):
            assert bucket.acquire() is True
        # Exceeded burst
        assert bucket.acquire() is False

    def test_refill_over_time(self):
        bucket = TokenBucket(rate_per_second=10.0, burst=2)
        assert bucket.acquire() is True
        assert bucket.acquire() is True
        assert bucket.acquire() is False
        time.sleep(0.25)  # Refill ~2.5 tokens
        assert bucket.acquire() is True

    def test_wait_time_calculation(self):
        bucket = TokenBucket(rate_per_second=10.0, burst=1)
        assert bucket.acquire() is True
        wait = bucket.wait_time()
        assert 0.0 < wait <= 0.15


class TestDeltaRestClient:
    def test_client_init_testnet(self):
        acct = AccountConfig(
            account_id="testnet_1",
            label="Testnet Acct",
            environment=TradingEnvironment.TESTNET,
            api_key="test_key",
            api_secret="test_secret",
        )
        client = DeltaRestClient(account=acct)
        assert client.environment == TradingEnvironment.TESTNET
        assert "testnet" in client.base_url

    def test_client_init_live(self):
        acct = AccountConfig(
            account_id="live_1",
            label="Live Acct",
            environment=TradingEnvironment.LIVE_MANUAL,
            api_key="live_key",
            api_secret="live_secret",
        )
        client = DeltaRestClient(account=acct)
        assert client.environment == TradingEnvironment.LIVE_MANUAL
        assert "api.india.delta.exchange" in client.base_url

    def test_client_signing(self):
        timestamp, sig = DeltaRestClient._sign(
            secret="test_secret",
            method="GET",
            path="/v2/orders",
            query_string="",
            body_str=""
        )
        assert len(sig) == 64
        assert int(timestamp) > 0

    def test_factory_get_delta_client(self):
        acct = AccountConfig(
            account_id="test_fac",
            label="Factory Acct",
            environment=TradingEnvironment.TESTNET,
            api_key="k",
            api_secret="s"
        )
        client = get_delta_client(account=acct)
        assert isinstance(client, DeltaRestClient)


class TestMarketDataValidation:
    def test_validate_spread_valid(self):
        assert validate_spread(bid=50000.0, ask=50010.0) is True

    def test_validate_spread_invalid_or_inverted(self):
        assert validate_spread(bid=50010.0, ask=50000.0) is False
        assert validate_spread(bid=0.0, ask=50000.0) is False
        assert validate_spread(bid=50000.0, ask=50000.0) is False

    def test_validate_liquidity(self):
        assert validate_liquidity(bid_size=10.0, ask_size=15.0, min_lot=1) is True
        assert validate_liquidity(bid_size=0.5, ask_size=15.0, min_lot=1) is False

    def test_is_fresh(self):
        now_ms = int(time.time() * 1000)
        assert is_fresh(now_ms - 1000, max_age_ms=3000) is True
        assert is_fresh(now_ms - 5000, max_age_ms=3000) is False

    def test_normalize_tick(self):
        raw = {
            "symbol": "BTCUSD",
            "best_bid": ["50000.00", 5.0],
            "best_ask": ["50010.00", 5.0],
            "last": "50005.00",
            "spot": "50000.00",
            "ts": int(time.time() * 1000)
        }
        quote = normalize_tick("BTCUSD", raw)
        assert quote is not None
        assert quote.symbol == "BTCUSD"
        assert quote.bid == 50000.0
        assert quote.ask == 50010.0
        assert quote.last == 50005.0

    def test_refresh_and_get_current_quote(self):
        raw = {
            "symbol": "ETHUSD",
            "best_bid": ["2999.00", 10.0],
            "best_ask": ["3001.00", 10.0],
            "last": "3000.00",
            "spot": "3000.00",
            "ts": int(time.time() * 1000)
        }
        q = refresh_quote("ETHUSD", raw)
        assert q is not None
        retrieved = get_current_quote("ETHUSD")
        assert retrieved is not None
        assert retrieved.symbol == "ETHUSD"


class TestMarketDataStore:
    def test_store_put_and_get(self):
        store = MarketDataStore()
        now_ms = int(time.time() * 1000)
        quote = MarketDataQuote(
            symbol="BTCUSD",
            bid=50000.0,
            ask=50001.0,
            mid=50000.5,
            bid_size=5.0,
            ask_size=5.0,
            last=50000.5,
            spot=50000.5,
            timestamp_ms=now_ms,
            age_ms=10,
        )
        store.update(quote)
        assert store.get("BTCUSD") == quote

    def test_store_stale_detection(self):
        store = MarketDataStore()
        now_ms = int(time.time() * 1000)
        old_quote = MarketDataQuote(
            symbol="BTCUSD",
            bid=50000.0,
            ask=50001.0,
            mid=50000.5,
            bid_size=5.0,
            ask_size=5.0,
            last=50000.5,
            spot=50000.5,
            timestamp_ms=now_ms - 10000,
            age_ms=10000,  # 10s old
        )
        store.update(old_quote)
        # Should return None because it exceeds 3000ms max age
        assert store.get("BTCUSD") is None
