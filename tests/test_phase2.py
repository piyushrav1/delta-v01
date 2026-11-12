"""
Phase 2 Tests: Security & Environment Isolation

Tests for:
- config/environments.py: TradingEnvironment enum, EnvironmentConfig, validation
- config/accounts.py: AccountConfig, load_all_accounts, environment filtering
- core/logging_config.py: SecretRedactionFilter, JSON formatting
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
import logging
import json
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTradingEnvironment:
    """Test TradingEnvironment enum and properties."""

    def test_environment_values(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.BACKTEST.value == "BACKTEST"
        assert TradingEnvironment.PAPER.value == "PAPER"
        assert TradingEnvironment.TESTNET.value == "TESTNET"
        assert TradingEnvironment.LIVE_MANUAL.value == "LIVE_MANUAL"
        assert TradingEnvironment.LIVE_AUTO.value == "LIVE_AUTO"

    def test_is_live_property(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.LIVE_MANUAL.is_live is True
        assert TradingEnvironment.LIVE_AUTO.is_live is True
        assert TradingEnvironment.TESTNET.is_live is False
        assert TradingEnvironment.PAPER.is_live is False
        assert TradingEnvironment.BACKTEST.is_live is False

    def test_is_testnet_property(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.TESTNET.is_testnet is True
        assert TradingEnvironment.LIVE_MANUAL.is_testnet is False
        assert TradingEnvironment.PAPER.is_testnet is False

    def test_allows_auto_trading(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.PAPER.allows_auto_trading is True
        assert TradingEnvironment.TESTNET.allows_auto_trading is True
        assert TradingEnvironment.LIVE_AUTO.allows_auto_trading is True
        assert TradingEnvironment.LIVE_MANUAL.allows_auto_trading is False
        assert TradingEnvironment.BACKTEST.allows_auto_trading is False

    def test_allows_manual_trading(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.TESTNET.allows_manual_trading is True
        assert TradingEnvironment.LIVE_MANUAL.allows_manual_trading is True
        assert TradingEnvironment.LIVE_AUTO.allows_manual_trading is True
        assert TradingEnvironment.PAPER.allows_manual_trading is False
        assert TradingEnvironment.BACKTEST.allows_manual_trading is False

    def test_base_url(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.TESTNET.base_url == "https://cdn-ind.testnet.deltaex.org"
        assert TradingEnvironment.LIVE_MANUAL.base_url == "https://api.india.delta.exchange"
        assert TradingEnvironment.LIVE_AUTO.base_url == "https://api.india.delta.exchange"

    def test_color_coding(self):
        from config.environments import TradingEnvironment
        assert TradingEnvironment.BACKTEST.color == "#6366f1"
        assert TradingEnvironment.PAPER.color == "#06b6d4"
        assert TradingEnvironment.TESTNET.color == "#f59e0b"
        assert TradingEnvironment.LIVE_MANUAL.color == "#ef4444"
        assert TradingEnvironment.LIVE_AUTO.color == "#dc2626"


class TestEnvironmentConfig:
    """Test EnvironmentConfig dataclass."""

    def test_valid_paper_config(self):
        from config.environments import EnvironmentConfig, TradingEnvironment
        config = EnvironmentConfig(environment=TradingEnvironment.PAPER)
        assert config.environment == TradingEnvironment.PAPER
        assert config.live_auto_authorized is False
        assert config.is_production is False

    def test_valid_testnet_config(self):
        from config.environments import EnvironmentConfig, TradingEnvironment
        config = EnvironmentConfig(environment=TradingEnvironment.TESTNET)
        assert config.environment == TradingEnvironment.TESTNET
        assert config.is_production is False

    def test_live_manual_config(self):
        from config.environments import EnvironmentConfig, TradingEnvironment
        config = EnvironmentConfig(environment=TradingEnvironment.LIVE_MANUAL)
        assert config.is_production is True

    def test_live_auto_requires_authorization(self):
        from config.environments import EnvironmentConfig, TradingEnvironment
        # Should fail without authorization
        with pytest.raises(ValueError, match="LIVE_AUTO_AUTHORIZED=true"):
            EnvironmentConfig(environment=TradingEnvironment.LIVE_AUTO, live_auto_authorized=False)

        # Should pass with authorization
        config = EnvironmentConfig(environment=TradingEnvironment.LIVE_AUTO, live_auto_authorized=True)
        assert config.environment == TradingEnvironment.LIVE_AUTO
        assert config.live_auto_authorized is True
        assert config.is_production is True

    def test_validate_account_environment(self):
        from config.environments import EnvironmentConfig, TradingEnvironment
        config = EnvironmentConfig(environment=TradingEnvironment.TESTNET)
        assert config.validate_account_environment("TESTNET") is True
        assert config.validate_account_environment("testnet") is True
        assert config.validate_account_environment("LIVE_MANUAL") is False


class TestGetEnvironmentFromEnv:
    """Test loading environment from environment variables."""

    def test_defaults_to_paper(self):
        from config.environments import get_environment_from_env
        with patch.dict(os.environ, {}, clear=True):
            config = get_environment_from_env()
            assert config.environment.value == "PAPER"

    def test_loads_trading_environment(self):
        from config.environments import get_environment_from_env, TradingEnvironment
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "TESTNET"}, clear=True):
            config = get_environment_from_env()
            assert config.environment == TradingEnvironment.TESTNET

    def test_case_insensitive(self):
        from config.environments import get_environment_from_env, TradingEnvironment
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "live_manual"}, clear=True):
            config = get_environment_from_env()
            assert config.environment == TradingEnvironment.LIVE_MANUAL

    def test_invalid_environment_raises(self):
        from config.environments import get_environment_from_env
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "INVALID"}, clear=True):
            with pytest.raises(ValueError, match="Invalid TRADING_ENVIRONMENT"):
                get_environment_from_env()

    def test_live_auto_authorized_flag(self):
        from config.environments import get_environment_from_env, TradingEnvironment
        with patch.dict(os.environ, {
            "TRADING_ENVIRONMENT": "LIVE_AUTO",
            "LIVE_AUTO_AUTHORIZED": "true"
        }, clear=True):
            config = get_environment_from_env()
            assert config.environment == TradingEnvironment.LIVE_AUTO
            assert config.live_auto_authorized is True


class TestAccountConfig:
    """Test AccountConfig dataclass."""

    def test_valid_account(self):
        from config.accounts import AccountConfig
        from config.environments import TradingEnvironment
        acct = AccountConfig(
            account_id="test",
            label="Test Account",
            api_key="key123",
            api_secret="secret456",
            environment=TradingEnvironment.TESTNET,
        )
        assert acct.account_id == "test"
        assert acct.environment == TradingEnvironment.TESTNET
        assert acct.effective_base_url == "https://cdn-ind.testnet.deltaex.org"
        assert acct.is_testnet is True
        assert acct.is_live is False

    def test_account_with_base_override(self):
        from config.accounts import AccountConfig
        from config.environments import TradingEnvironment
        acct = AccountConfig(
            account_id="test",
            label="Test",
            api_key="key",
            api_secret="secret",
            environment=TradingEnvironment.TESTNET,
            base_url="https://custom.example.com",
        )
        assert acct.effective_base_url == "https://custom.example.com"

    def test_missing_credentials_raises(self):
        from config.accounts import AccountConfig
        from config.environments import TradingEnvironment
        with pytest.raises(ValueError, match="api_key and api_secret are required"):
            AccountConfig(
                account_id="test", label="Test", api_key="", api_secret="secret",
                environment=TradingEnvironment.TESTNET,
            )

    def test_invalid_environment_type_raises(self):
        from config.accounts import AccountConfig
        with pytest.raises(ValueError, match="environment must be TradingEnvironment"):
            AccountConfig(
                account_id="test", label="Test", api_key="key", api_secret="secret",
                environment="TESTNET",  # String instead of enum
            )

    def test_allows_auto_trading_by_global_env(self):
        from config.accounts import AccountConfig
        from config.environments import TradingEnvironment
        acct = AccountConfig(
            account_id="test", label="Test", api_key="key", api_secret="secret",
            environment=TradingEnvironment.TESTNET,
        )
        # TESTNET account allows auto in TESTNET and PAPER
        assert acct.allows_auto_trading(TradingEnvironment.TESTNET) is True
        assert acct.allows_auto_trading(TradingEnvironment.PAPER) is True
        assert acct.allows_auto_trading(TradingEnvironment.LIVE_AUTO) is False
        assert acct.allows_auto_trading(TradingEnvironment.LIVE_MANUAL) is False
        assert acct.allows_auto_trading(TradingEnvironment.BACKTEST) is False

        # PAPER account allows auto in PAPER and TESTNET
        paper_acct = AccountConfig(
            account_id="paper", label="Paper", api_key="key", api_secret="secret",
            environment=TradingEnvironment.PAPER,
        )
        assert paper_acct.allows_auto_trading(TradingEnvironment.PAPER) is True
        assert paper_acct.allows_auto_trading(TradingEnvironment.TESTNET) is True
        assert paper_acct.allows_auto_trading(TradingEnvironment.LIVE_AUTO) is False


class TestLoadAllAccounts:
    """Test loading accounts from environment variables."""

    def test_legacy_main_account(self):
        from config.accounts import load_all_accounts
        from config.environments import TradingEnvironment
        with patch.dict(os.environ, {
            "DELTA_API_KEY": "main_key",
            "DELTA_API_SECRET": "main_secret",
            "DELTA_ACCOUNT_MAIN_ENVIRONMENT": "TESTNET",
        }, clear=True):
            accounts = load_all_accounts()
            assert "main" in accounts
            assert accounts["main"].api_key == "main_key"
            assert accounts["main"].environment == TradingEnvironment.TESTNET

    def test_legacy_defaults_to_testnet(self):
        from config.accounts import load_all_accounts
        from config.environments import TradingEnvironment
        # No ENVIRONMENT set - should default to TESTNET for safety
        with patch.dict(os.environ, {
            "DELTA_API_KEY": "main_key",
            "DELTA_API_SECRET": "main_secret",
        }, clear=True):
            accounts = load_all_accounts()
            assert accounts["main"].environment == TradingEnvironment.TESTNET

    def test_account_env_required(self):
        from config.accounts import load_all_accounts
        # DELTA_ACCOUNT_2_KEY/SECRET set but no ENVIRONMENT
        with patch.dict(os.environ, {
            "DELTA_API_KEY": "main_key",
            "DELTA_API_SECRET": "main_secret",
            "DELTA_ACCOUNT_MAIN_ENVIRONMENT": "TESTNET",
            "DELTA_ACCOUNT_2_KEY": "key2",
            "DELTA_ACCOUNT_2_SECRET": "secret2",
            # Missing DELTA_ACCOUNT_2_ENVIRONMENT
        }, clear=True):
            with pytest.raises(ValueError, match="ENVIRONMENT is REQUIRED"):
                load_all_accounts()

    def test_invalid_account_env_raises(self):
        from config.accounts import load_all_accounts
        with patch.dict(os.environ, {
            "DELTA_API_KEY": "main_key",
            "DELTA_API_SECRET": "main_secret",
            "DELTA_ACCOUNT_MAIN_ENVIRONMENT": "TESTNET",
            "DELTA_ACCOUNT_2_KEY": "key2",
            "DELTA_ACCOUNT_2_SECRET": "secret2",
            "DELTA_ACCOUNT_2_ENVIRONMENT": "INVALID",
        }, clear=True):
            with pytest.raises(ValueError, match="Invalid ENVIRONMENT"):
                load_all_accounts()

    def test_multiple_accounts(self):
        from config.accounts import load_all_accounts
        from config.environments import TradingEnvironment
        with patch.dict(os.environ, {
            "DELTA_API_KEY": "main_key",
            "DELTA_API_SECRET": "main_secret",
            "DELTA_ACCOUNT_MAIN_ENVIRONMENT": "TESTNET",
            "DELTA_ACCOUNT_2_KEY": "key2",
            "DELTA_ACCOUNT_2_SECRET": "secret2",
            "DELTA_ACCOUNT_2_ENVIRONMENT": "LIVE_MANUAL",
            "DELTA_ACCOUNT_2_LABEL": "Live Account",
        }, clear=True):
            accounts = load_all_accounts()
            assert len(accounts) == 2
            assert accounts["main"].environment == TradingEnvironment.TESTNET
            assert accounts["2"].environment == TradingEnvironment.LIVE_MANUAL
            assert accounts["2"].label == "Live Account"

    def test_no_accounts_raises(self):
        from config.accounts import load_all_accounts
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="No Delta accounts configured"):
                load_all_accounts()


class TestGetAccountsForEnvironment:
    """Test filtering accounts by environment."""

    def test_filter_by_exact_match(self):
        from config.accounts import AccountConfig, get_accounts_for_exact_environment
        from config.environments import TradingEnvironment
        accounts = {
            "testnet": AccountConfig("testnet", "Testnet", "k", "s", TradingEnvironment.TESTNET),
            "live": AccountConfig("live", "Live", "k", "s", TradingEnvironment.LIVE_MANUAL),
            "paper": AccountConfig("paper", "Paper", "k", "s", TradingEnvironment.PAPER),
        }
        result = get_accounts_for_exact_environment(accounts, TradingEnvironment.TESTNET)
        assert len(result) == 1
        assert result[0].account_id == "testnet"

    def test_paper_works_in_testnet(self):
        from config.accounts import AccountConfig, get_accounts_for_environment
        from config.environments import TradingEnvironment
        accounts = {
            "paper": AccountConfig("paper", "Paper", "k", "s", TradingEnvironment.PAPER),
        }
        result = get_accounts_for_environment(accounts, TradingEnvironment.TESTNET)
        assert len(result) == 1

    def test_testnet_works_in_paper(self):
        from config.accounts import AccountConfig, get_accounts_for_environment
        from config.environments import TradingEnvironment
        accounts = {
            "testnet": AccountConfig("testnet", "Testnet", "k", "s", TradingEnvironment.TESTNET),
        }
        result = get_accounts_for_environment(accounts, TradingEnvironment.PAPER)
        assert len(result) == 1

    def test_inactive_accounts_filtered(self):
        from config.accounts import AccountConfig, get_accounts_for_environment
        from config.environments import TradingEnvironment
        accounts = {
            "active": AccountConfig("active", "Active", "k", "s", TradingEnvironment.TESTNET, is_active=True),
            "inactive": AccountConfig("inactive", "Inactive", "k", "s", TradingEnvironment.TESTNET, is_active=False),
        }
        result = get_accounts_for_environment(accounts, TradingEnvironment.TESTNET)
        assert len(result) == 1
        assert result[0].account_id == "active"

    def test_auto_only_filter(self):
        from config.accounts import AccountConfig, get_accounts_for_environment
        from config.environments import TradingEnvironment
        accounts = {
            "testnet": AccountConfig("testnet", "Testnet", "k", "s", TradingEnvironment.TESTNET),
            "live_manual": AccountConfig("live_manual", "Live Manual", "k", "s", TradingEnvironment.LIVE_MANUAL),
        }
        # With auto_only=True, LIVE_MANUAL should be excluded
        result = get_accounts_for_environment(accounts, TradingEnvironment.TESTNET, auto_only=True)
        assert len(result) == 1
        assert result[0].account_id == "testnet"


class TestValidateLiveAuto:
    """Test LIVE_AUTO validation."""

    def test_passes_when_not_live_auto(self):
        from config.accounts import validate_account_for_live_auto, AccountConfig
        from config.environments import TradingEnvironment
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "TESTNET"}, clear=True):
            accounts = {"test": AccountConfig("test", "Test", "k", "s", TradingEnvironment.TESTNET)}
            assert validate_account_for_live_auto(accounts) is True

    def test_fails_when_live_auto_no_accounts(self):
        from config.accounts import validate_account_for_live_auto, AccountConfig
        from config.environments import TradingEnvironment
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "LIVE_AUTO", "LIVE_AUTO_AUTHORIZED": "true"}, clear=True):
            accounts = {"test": AccountConfig("test", "Test", "k", "s", TradingEnvironment.TESTNET)}
            with pytest.raises(RuntimeError, match="no accounts with ENVIRONMENT=LIVE_AUTO"):
                validate_account_for_live_auto(accounts)

    def test_passes_when_live_auto_has_account(self):
        from config.accounts import validate_account_for_live_auto, AccountConfig
        from config.environments import TradingEnvironment
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "LIVE_AUTO", "LIVE_AUTO_AUTHORIZED": "true"}, clear=True):
            accounts = {
                "live": AccountConfig("live", "Live", "k", "s", TradingEnvironment.LIVE_AUTO),
            }
            assert validate_account_for_live_auto(accounts) is True


class TestSecretRedactionFilter:
    """Test secret redaction in logging."""

    def test_redacts_api_key_32chars(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "API key: abcdefghijklmnopqrstuvwxyz123456"
        record.args = ()
        filter_obj.filter(record)
        assert "abcdefghijklmnopqrstuvwxyz123456" not in record.msg
        assert "[REDACTED_API_KEY]" in record.msg

    def test_redacts_secret_64hex(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        import logging
        # Test standalone 64+ hex secret (not in key=value format, not preceded by "secret:")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="Raw hex value abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789 here",
            args=(), exc_info=None
        )
        filter_obj.filter(record)
        assert "[REDACTED_SECRET]" in record.msg

    def test_redacts_jwt_token(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        import logging
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="Raw JWT value eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0 here",
            args=(), exc_info=None
        )
        filter_obj.filter(record)
        assert "[REDACTED_JWT]" in record.msg

    def test_redacts_authorization_header(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "Authorization: Bearer secrettoken123"
        record.args = ()
        filter_obj.filter(record)
        assert "Authorization: [REDACTED]" in record.msg

    def test_redacts_api_key_header(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "api-key: hNnIHz8129i3XuwhywVjhISaZqpbXG"
        record.args = ()
        filter_obj.filter(record)
        assert "Api-Key: [REDACTED]" in record.msg

    def test_redacts_signature_header(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "signature: abcdef123456"
        record.args = ()
        filter_obj.filter(record)
        assert "Signature: [REDACTED]" in record.msg

    def test_redacts_password_field(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "password: mysecretpassword"
        record.args = ()
        filter_obj.filter(record)
        assert "Password: [REDACTED]" in record.msg

    def test_redacts_private_key_pem(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "Key: -----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQD...\n-----END PRIVATE KEY-----"
        record.args = ()
        filter_obj.filter(record)
        assert "[REDACTED_PRIVATE_KEY]" in record.msg

    def test_redacts_dict_args(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "Request data: %s"
        record.args = ({"api_key": "secretkey123", "normal_field": "value"},)
        filter_obj.filter(record)
        assert record.args[0]["api_key"] == "[REDACTED]"
        assert record.args[0]["normal_field"] == "value"

    def test_redacts_nested_dict(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        record = MagicMock()
        record.msg = "Data: %s"
        record.args = ({"outer": {"inner": {"api_secret": "supersecret"}}},)
        filter_obj.filter(record)
        assert record.args[0]["outer"]["inner"]["api_secret"] == "[REDACTED]"

    def test_redacts_list_items(self):
        from core.logging_config import SecretRedactionFilter
        filter_obj = SecretRedactionFilter()
        import logging
        # Test list item redaction - string items get processed by _redact_iterable -> _redact_string
        # API key pattern requires 32+ alphanumeric chars
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="Items: %s",
            args=(["normal", "apikey12345678901234567890123456"],), exc_info=None
        )
        filter_obj.filter(record)
        assert isinstance(record.args, tuple)
        assert len(record.args) == 1
        assert isinstance(record.args[0], list)
        # The 32+ char alphanumeric string should be redacted by the generic API key pattern
        redacted_item = record.args[0][1]
        assert "[REDACTED_API_KEY]" in redacted_item or "[REDACTED_SECRET]" in redacted_item


class TestJsonFormatter:
    """Test JSON log formatter."""

    def test_json_format_includes_standard_fields(self):
        from core.logging_config import JsonFormatter
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="test.py",
            lineno=42, msg="Test message", args=(), exc_info=None
        )
        record.module = "test"
        record.funcName = "test_func"
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["module"] == "test"
        assert data["function"] == "test_func"
        assert data["line"] == 42
        assert "timestamp" in data
        assert "process_id" in data
        assert "thread_id" in data

    def test_json_format_includes_correlation_id(self):
        from core.logging_config import JsonFormatter, set_correlation_id, clear_correlation_id
        formatter = JsonFormatter()
        set_correlation_id("test-correlation-123")
        try:
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="test.py",
                lineno=1, msg="Test", args=(), exc_info=None
            )
            record.module = "test"
            record.funcName = "test"
            output = formatter.format(record)
            data = json.loads(output)
            assert data["correlation_id"] == "test-correlation-123"
        finally:
            clear_correlation_id()


class TestSetupLogging:
    """Test logging setup."""

    def test_setup_logging_returns_logger(self):
        from core.logging_config import setup_logging, get_logger
        logger = setup_logging(level="DEBUG", json_output=False)
        assert logger is not None
        assert logger.level == logging.DEBUG

    def test_get_logger_returns_instance(self):
        from core.logging_config import get_logger
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"


class TestCorrelationContext:
    """Test correlation ID context manager."""

    def test_context_manager_sets_and_clears(self):
        from core.logging_config import CorrelationContext, get_correlation_id, clear_correlation_id
        clear_correlation_id()
        assert get_correlation_id() is None

        with CorrelationContext("test-id-123") as cid:
            assert cid == "test-id-123"
            assert get_correlation_id() == "test-id-123"

        assert get_correlation_id() is None

    def test_context_manager_restores_previous(self):
        from core.logging_config import CorrelationContext, get_correlation_id, set_correlation_id, clear_correlation_id
        clear_correlation_id()
        set_correlation_id("previous-id")

        with CorrelationContext("new-id"):
            assert get_correlation_id() == "new-id"

        assert get_correlation_id() == "previous-id"
        clear_correlation_id()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])