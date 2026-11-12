"""
Multi-Account Credential Management

Explicit account configuration with mandatory environment tags.
Eliminates fuzzy fallback logic (aid == '2') that could route to LIVE.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from config.environments import TradingEnvironment, get_environment_from_env


@dataclass(frozen=True)
class AccountConfig:
    """Single trading account configuration."""
    account_id: str                    # Unique identifier (e.g., 'main', 'testnet', 'live_1')
    label: str                         # Human-readable label
    api_key: str                       # Delta API key
    api_secret: str                    # Delta API secret
    environment: TradingEnvironment    # MANDATORY: BACKTEST, PAPER, TESTNET, LIVE_MANUAL, LIVE_AUTO
    base_url: Optional[str] = None     # Optional override (defaults to environment base_url)
    is_active: bool = True             # Whether account is available for trading

    def __post_init__(self):
        if not self.api_key or not self.api_secret:
            raise ValueError(f"Account '{self.account_id}': api_key and api_secret are required")
        if not isinstance(self.environment, TradingEnvironment):
            raise ValueError(f"Account '{self.account_id}': environment must be TradingEnvironment enum")

    @property
    def effective_base_url(self) -> str:
        """Get the effective base URL (override or environment default)."""
        return self.base_url or self.environment.base_url

    @property
    def is_live(self) -> bool:
        """True if this account can trade real money."""
        return self.environment.is_live

    @property
    def is_testnet(self) -> bool:
        """True if this account uses testnet."""
        return self.environment.is_testnet

    def allows_auto_trading(self, global_env: TradingEnvironment) -> bool:
        """Check if auto-trading is allowed for this account in the global environment."""
        # Account environment must be compatible with global environment
        # TESTNET accounts only work in TESTNET/PAPER
        # PAPER accounts only work in PAPER/TESTNET
        # LIVE_AUTO accounts only work in LIVE_AUTO
        # LIVE_MANUAL accounts never allow auto trading
        # BACKTEST accounts only work in BACKTEST
        if self.environment == TradingEnvironment.LIVE_AUTO:
            return global_env == TradingEnvironment.LIVE_AUTO
        if self.environment == TradingEnvironment.LIVE_MANUAL:
            return False  # Manual only
        if self.environment == TradingEnvironment.TESTNET:
            return global_env in (TradingEnvironment.TESTNET, TradingEnvironment.PAPER)
        if self.environment == TradingEnvironment.PAPER:
            return global_env in (TradingEnvironment.PAPER, TradingEnvironment.TESTNET)
        if self.environment == TradingEnvironment.BACKTEST:
            return global_env == TradingEnvironment.BACKTEST
        return False


def _load_account_from_env(prefix: str, account_id: str, label_suffix: str = "") -> Optional[AccountConfig]:
    """Load a single account from environment variables with given prefix."""
    key = os.getenv(f"{prefix}_KEY", "")
    secret = os.getenv(f"{prefix}_SECRET", "")
    if not key or not secret:
        return None

    label = os.getenv(f"{prefix}_LABEL") or os.getenv(f"{prefix}_NAME") or f"{account_id}{label_suffix}".title()
    env_str = os.getenv(f"{prefix}_ENVIRONMENT", "").upper()
    base_url = os.getenv(f"{prefix}_BASE", "").strip() or None

    if not env_str:
        raise ValueError(
            f"Account '{account_id}' ({prefix}): ENVIRONMENT is REQUIRED. "
            f"Must be one of: {[e.value for e in TradingEnvironment]}"
        )

    try:
        environment = TradingEnvironment(env_str)
    except ValueError:
        raise ValueError(
            f"Account '{account_id}' ({prefix}): Invalid ENVIRONMENT='{env_str}'. "
            f"Must be one of: {[e.value for e in TradingEnvironment]}"
        )

    return AccountConfig(
        account_id=account_id,
        label=label,
        api_key=key,
        api_secret=secret,
        environment=environment,
        base_url=base_url,
    )


def load_all_accounts() -> Dict[str, AccountConfig]:
    """
    Load all accounts from environment variables.

    Expected naming pattern:
        DELTA_ACCOUNT_<ID>_KEY
        DELTA_ACCOUNT_<ID>_SECRET
        DELTA_ACCOUNT_<ID>_LABEL (optional)
        DELTA_ACCOUNT_<ID>_ENVIRONMENT (REQUIRED)
        DELTA_ACCOUNT_<ID>_BASE (optional)

    Also supports legacy single-account vars:
        DELTA_API_KEY / DELTA_API_SECRET (treated as 'main' account)
        DELTA_API_BASE_URL (optional base override)

    Returns:
        Dict mapping account_id -> AccountConfig

    Raises:
        ValueError: If any account configuration is invalid
    """
    accounts = {}

    # Legacy main account (DELTA_API_KEY/SECRET)
    main_key = os.getenv("DELTA_API_KEY", "")
    main_secret = os.getenv("DELTA_API_SECRET", "")
    if main_key and main_secret:
        main_label = (
            os.getenv("DELTA_ACCOUNT_MAIN_LABEL")
            or os.getenv("DELTA_ACCOUNT_MAIN_NAME")
            or os.getenv("DELTA_ACCOUNT_LABEL")
            or os.getenv("DELTA_ACCOUNT_NAME")
            or os.getenv("DELTA_MAIN_ACCOUNT_NAME")
            or os.getenv("DELTA_MAIN_LABEL")
            or os.getenv("DELTA_ACCOUNT_1_LABEL")
            or os.getenv("DELTA_ACCOUNT_1_NAME")
            or "Main"
        )
        main_env_str = os.getenv("DELTA_ACCOUNT_MAIN_ENVIRONMENT", "").upper()
        main_base = os.getenv("DELTA_ACCOUNT_MAIN_BASE", os.getenv("DELTA_API_BASE_URL", "")).strip() or None

        if not main_env_str:
            # Default to TESTNET for safety if not specified
            # This prevents accidental LIVE execution
            main_env_str = "TESTNET"

        try:
            main_env = TradingEnvironment(main_env_str)
        except ValueError:
            raise ValueError(
                f"Main account: Invalid ENVIRONMENT='{main_env_str}'. "
                f"Must be one of: {[e.value for e in TradingEnvironment]}"
            )

        accounts["main"] = AccountConfig(
            account_id="main",
            label=main_label,
            api_key=main_key,
            api_secret=main_secret,
            environment=main_env,
            base_url=main_base,
        )

    # Pattern: DELTA_ACCOUNT_<TAG>_KEY
    for var, val in os.environ.items():
        match = re.match(r"DELTA_ACCOUNT_(.+)_KEY$", var)
        if match and val:
            tag = match.group(1)
            # Skip if already loaded as 'main'
            if tag.lower() == "main":
                continue
            prefix = f"DELTA_ACCOUNT_{tag}"
            account_id = tag.lower()
            try:
                acct = _load_account_from_env(prefix, account_id)
                if acct:
                    accounts[account_id] = acct
            except ValueError as e:
                raise ValueError(f"Failed to load account '{account_id}': {e}")

    # Alternate pattern: DELTA_API_KEY_<TAG>
    for var, val in os.environ.items():
        match = re.match(r"DELTA_API_KEY_(.+)$", var)
        if match and val and match.group(1) not in ("", "MAIN"):
            tag = match.group(1)
            account_id = tag.lower()
            # Skip if already loaded
            if account_id in accounts:
                continue
            sec = os.getenv(f"DELTA_API_SECRET_{tag}", "")
            if not sec:
                continue
            label = (
                os.getenv(f"DELTA_API_LABEL_{tag}")
                or os.getenv(f"DELTA_API_NAME_{tag}")
                or tag.title()
            )
            env_str = os.getenv(f"DELTA_API_ENVIRONMENT_{tag}", "").upper()
            base_url = os.getenv(f"DELTA_API_BASE_{tag}", "").strip() or None

            if not env_str:
                raise ValueError(
                    f"Account '{account_id}' (DELTA_API_KEY_{tag}): ENVIRONMENT is REQUIRED"
                )

            try:
                environment = TradingEnvironment(env_str)
            except ValueError:
                raise ValueError(
                    f"Account '{account_id}': Invalid ENVIRONMENT='{env_str}'"
                )

            accounts[account_id] = AccountConfig(
                account_id=account_id,
                label=label,
                api_key=val,
                api_secret=sec,
                environment=environment,
                base_url=base_url,
            )

    if not accounts:
        raise ValueError(
            "No Delta accounts configured. "
            "Set DELTA_API_KEY/SECRET (legacy) or DELTA_ACCOUNT_<ID>_KEY/SECRET with ENVIRONMENT."
        )

    return accounts


def get_accounts_for_environment(
    accounts: Dict[str, AccountConfig],
    environment: TradingEnvironment,
    auto_only: bool = False
) -> List[AccountConfig]:
    """
    Filter accounts compatible with the given environment.

    Args:
        accounts: All loaded accounts
        environment: Target trading environment
        auto_only: If True, only return accounts that allow auto-trading

    Returns:
        List of compatible AccountConfig objects
    """
    result = []
    for acct in accounts.values():
        if not acct.is_active:
            continue
        # Exact environment match
        if acct.environment == environment:
            if not auto_only or acct.allows_auto_trading(environment):
                result.append(acct)
        # PAPER accounts can work in TESTNET for testing
        elif acct.environment == TradingEnvironment.PAPER and environment == TradingEnvironment.TESTNET:
            if not auto_only or acct.allows_auto_trading(environment):
                result.append(acct)
        # TESTNET accounts can work in PAPER for dry-run
        elif acct.environment == TradingEnvironment.TESTNET and environment == TradingEnvironment.PAPER:
            if not auto_only or acct.allows_auto_trading(environment):
                result.append(acct)
    return result


def get_accounts_for_exact_environment(
    accounts: Dict[str, AccountConfig],
    environment: TradingEnvironment,
    auto_only: bool = False
) -> List[AccountConfig]:
    """
    Filter accounts that EXACTLY match the given environment (no cross-compatibility).

    Args:
        accounts: All loaded accounts
        environment: Target trading environment (exact match only)
        auto_only: If True, only return accounts that allow auto-trading

    Returns:
        List of AccountConfig objects with exact environment match
    """
    result = []
    for acct in accounts.values():
        if not acct.is_active:
            continue
        if acct.environment == environment:
            if not auto_only or acct.allows_auto_trading(environment):
                result.append(acct)
    return result


def get_default_account(accounts: Dict[str, AccountConfig]) -> AccountConfig:
    """Get the default account (prefers 'main', then first available)."""
    if "main" in accounts:
        return accounts["main"]
    return next(iter(accounts.values()))


def validate_account_for_live_auto(accounts: Dict[str, AccountConfig]) -> bool:
    """
    Validate that LIVE_AUTO mode has at least one properly configured live account.
    This is a safety check before enabling automated live trading.
    """
    env_config = get_environment_from_env()
    if env_config.environment != TradingEnvironment.LIVE_AUTO:
        return True  # Not in LIVE_AUTO, no check needed

    live_accounts = [
        a for a in accounts.values()
        if a.environment == TradingEnvironment.LIVE_AUTO and a.is_active
    ]
    if not live_accounts:
        raise RuntimeError(
            "LIVE_AUTO mode enabled but no accounts with ENVIRONMENT=LIVE_AUTO found. "
            "Configure at least one account with DELTA_ACCOUNT_<ID>_ENVIRONMENT=LIVE_AUTO"
        )
    return True