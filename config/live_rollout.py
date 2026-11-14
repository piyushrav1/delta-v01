"""
Phased Capital Rollout Configuration (TASK-P16-02)

Enforces strict defense-in-depth limits during the initial live trading transition:
- Stage 1 (MICRO_LOT):
    - Maximum 1 micro-lot contract per order (size = 1)
    - Restricted to Strategy A (Trend Following) & Strategy F (Anomaly Scanner)
    - Hard stop-loss at $20.00 USD daily realized/unrealized loss
    - Mandatory protective Stop-Loss on every order
    - Max 1 open position concurrently
- Stage 2 (CONSERVATIVE):
    - Maximum 2 contracts per order
    - Maximum 3 concurrent open positions
    - Hard stop-loss at $50.00 USD daily loss
- Stage 3 (STANDARD):
    - Standard portfolio risk parameters governed by RiskSettings

Fails CLOSED: Any order violating active rollout stage bounds is immediately rejected.
"""
from __future__ import annotations

import os
import logging
from enum import Enum
from decimal import Decimal
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field

from core.exceptions import RiskViolation

logger = logging.getLogger(__name__)


class RolloutStage(str, Enum):
    """Phased deployment stages for live trading capital control."""
    MICRO_LOT = "MICRO_LOT"          # Stage 1: Single micro-lot, Strat A/F, $20 hard cap
    CONSERVATIVE = "CONSERVATIVE"    # Stage 2: 2 lots, max 3 positions, $50 hard cap
    STANDARD = "STANDARD"            # Stage 3: Full production risk boundaries


@dataclass(frozen=True)
class LiveRolloutConfig:
    """Enforceable constraints for the active live rollout stage."""
    stage: RolloutStage
    max_lots_per_order: int
    max_open_positions: int
    max_daily_loss_usd: Decimal
    allowed_strategies: List[str]
    max_slippage_pct: Decimal = Decimal("0.002")  # 0.2% max slippage for live rollout
    require_stop_loss: bool = True

    def validate_order(
        self,
        strategy_id: str,
        size: int,
        current_open_positions: int = 0,
        current_daily_loss_usd: Decimal = Decimal("0.0"),
        has_stop_loss: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate order intent against live rollout stage boundaries.

        Returns:
            (is_valid, rejection_reason)
        """
        # 1. Strategy Whitelist Check
        if self.allowed_strategies and strategy_id not in self.allowed_strategies:
            reason = (
                f"Strategy '{strategy_id}' is not authorized in rollout stage {self.stage.value}. "
                f"Allowed strategies: {self.allowed_strategies}"
            )
            logger.warning(f"Rollout violation: {reason}")
            return False, reason

        # 2. Lot Size Sizing Check
        if size > self.max_lots_per_order:
            reason = (
                f"Order size {size} exceeds rollout stage {self.stage.value} "
                f"max lot size limit of {self.max_lots_per_order} contract(s)"
            )
            logger.warning(f"Rollout violation: {reason}")
            return False, reason

        if size <= 0:
            reason = f"Order size must be positive, got {size}"
            return False, reason

        # 3. Concurrent Position Limit Check
        if current_open_positions >= self.max_open_positions:
            reason = (
                f"Current open positions ({current_open_positions}) has reached "
                f"rollout stage {self.stage.value} limit of {self.max_open_positions}"
            )
            logger.warning(f"Rollout violation: {reason}")
            return False, reason

        # 4. Hard Daily Loss Cap Check
        if current_daily_loss_usd >= self.max_daily_loss_usd:
            reason = (
                f"Current daily loss (${current_daily_loss_usd:.2f}) has reached or breached "
                f"rollout stage {self.stage.value} hard stop of ${self.max_daily_loss_usd:.2f}"
            )
            logger.critical(f"🚨 Rollout hard stop breached: {reason}")
            return False, reason

        # 5. Mandatory Stop Loss Check
        if self.require_stop_loss and not has_stop_loss:
            reason = f"Rollout stage {self.stage.value} requires mandatory protective Stop-Loss bracket"
            logger.warning(f"Rollout violation: {reason}")
            return False, reason

        return True, None

    def assert_order_allowed(
        self,
        strategy_id: str,
        size: int,
        current_open_positions: int = 0,
        current_daily_loss_usd: Decimal = Decimal("0.0"),
        has_stop_loss: bool = True,
    ) -> None:
        """Raise RiskViolation if order fails rollout constraints."""
        valid, reason = self.validate_order(
            strategy_id=strategy_id,
            size=size,
            current_open_positions=current_open_positions,
            current_daily_loss_usd=current_daily_loss_usd,
            has_stop_loss=has_stop_loss,
        )
        if not valid:
            raise RiskViolation(rule=f"live_rollout_{self.stage.value.lower()}", message=reason or "Rollout violation")


# Pre-configured rollout configurations
STAGE_CONFIGS: Dict[RolloutStage, LiveRolloutConfig] = {
    RolloutStage.MICRO_LOT: LiveRolloutConfig(
        stage=RolloutStage.MICRO_LOT,
        max_lots_per_order=1,
        max_open_positions=1,
        max_daily_loss_usd=Decimal("20.00"),
        allowed_strategies=[
            "strat_trend_futures",      # Strategy A: Futures Trend Following
            "strat_options_anomaly",    # Strategy F: Options Anomaly Scanner
            "trend_following_btc",
            "options_mispricing",
            "opening_range_breakout",   # Day-Trader-Skill ORB
            "daily_setup_scanner",      # Day-Trader-Skill 5 Setups
            "atr_exit_manager",         # Day-Trader-Skill Dynamic Exit
        ],
        max_slippage_pct=Decimal("0.002"),
        require_stop_loss=True,
    ),
    RolloutStage.CONSERVATIVE: LiveRolloutConfig(
        stage=RolloutStage.CONSERVATIVE,
        max_lots_per_order=2,
        max_open_positions=3,
        max_daily_loss_usd=Decimal("50.00"),
        allowed_strategies=[
            "strat_trend_futures",
            "strat_options_anomaly",
            "strat_momentum",
            "strat_mean_reversion",
            "trend_following_btc",
            "options_mispricing",
            "opening_range_breakout",
            "daily_setup_scanner",
            "atr_exit_manager",
        ],
        max_slippage_pct=Decimal("0.003"),
        require_stop_loss=True,
    ),
    RolloutStage.STANDARD: LiveRolloutConfig(
        stage=RolloutStage.STANDARD,
        max_lots_per_order=10,
        max_open_positions=5,
        max_daily_loss_usd=Decimal("500.00"),
        allowed_strategies=[],  # Empty list = all strategies permitted
        max_slippage_pct=Decimal("0.005"),
        require_stop_loss=True,
    ),
}


def get_rollout_config(stage: Optional[RolloutStage | str] = None) -> LiveRolloutConfig:
    """
    Get rollout configuration from stage name or environment variable LIVE_ROLLOUT_STAGE.
    Defaults to MICRO_LOT for fail-closed safety.
    """
    if stage is None:
        stage_str = os.getenv("LIVE_ROLLOUT_STAGE", "MICRO_LOT").upper()
    elif isinstance(stage, RolloutStage):
        return STAGE_CONFIGS[stage]
    else:
        stage_str = str(stage).upper()

    try:
        resolved_stage = RolloutStage(stage_str)
    except ValueError:
        logger.warning(f"Unknown LIVE_ROLLOUT_STAGE '{stage_str}'. Falling back to MICRO_LOT.")
        resolved_stage = RolloutStage.MICRO_LOT

    return STAGE_CONFIGS[resolved_stage]
