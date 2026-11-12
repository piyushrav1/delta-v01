"""
Black-Scholes Greeks Engine

Analytical pricing and Greek calculation (Δ, Γ, Θ, 𝒱, ρ) for crypto options
with continuous dividend yield.

Uses standard normal CDF via math.erf for compatibility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Optional

# Decimal precision
getcontext().prec = 30


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    """Greek sensitivities for an option."""
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    rho: Decimal

    def __repr__(self) -> str:
        return (f"Greeks(delta={self.delta:.4f}, gamma={self.gamma:.4f}, "
                f"theta={self.theta:.4f}, vega={self.vega:.4f}, rho={self.rho:.4f})")


@dataclass
class GreeksInput:
    """Input parameters for Greeks calculation."""
    S: Decimal     # Underlying price
    K: Decimal     # Strike price
    T: Decimal     # Time to expiry (years)
    r: Decimal     # Risk-free rate (annualized)
    q: Decimal     # Dividend yield (annualized, 0 for crypto)
    sigma: Decimal # Volatility (annualized)


def calculate_greeks(input_: GreeksInput) -> Greeks:
    """Calculate Black-Scholes Greeks from input parameters."""
    S = float(input_.S)
    K = float(input_.K)
    T = float(input_.T)
    r = float(input_.r)
    q = float(input_.q)
    sigma = float(input_.sigma)

    if T <= 0:
        if S > K:
            delta = Decimal(1)
        elif S < K:
            delta = Decimal(0)
        else:
            delta = Decimal("0.5")
        return Greeks(delta=delta, gamma=Decimal("0"), theta=Decimal("0"),
                      vega=Decimal("0"), rho=Decimal("0"))

    # d1, d2
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    # Common terms
    exp_neg_qT = math.exp(-q * T)
    exp_neg_rT = math.exp(-r * T)
    n_d1 = _norm_cdf(d1)
    n_d2 = _norm_cdf(d2)
    n_d1_pdf = _norm_pdf(d1)

    # Delta (call delta)
    delta = Decimal(exp_neg_qT * n_d1)

    # Gamma
    gamma = Decimal(exp_neg_qT * n_d1_pdf / (S * sigma * math.sqrt(T)))

    # Theta
    theta = Decimal(-exp_neg_qT * (S * n_d1_pdf * sigma / (2.0 * math.sqrt(T)) + q * K * _norm_cdf(d2)))

    # Vega
    vega = Decimal(exp_neg_qT * S * n_d1_pdf * math.sqrt(T))

    # Rho
    rho = Decimal(exp_neg_qT * K * T * exp_neg_rT * _norm_cdf(d2))

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def quick_greeks(
    S: float | Decimal | int,
    K: float | Decimal | int,
    T: float | Decimal | int,
    r: float | Decimal | int,
    sigma: float | Decimal | int,
    q: float | Decimal | int = 0.0,
) -> Greeks:
    """Quick Greeks from raw numeric types."""
    return calculate_greeks(GreeksInput(
        S=Decimal(str(S)),
        K=Decimal(str(K)),
        T=Decimal(str(T)),
        r=Decimal(str(r)),
        q=Decimal(str(q)),
        sigma=Decimal(str(sigma)),
    ))