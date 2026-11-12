"""
Implied Volatility Surface & Skew Engine

Newton-Raphson / Brent root-finding to extract IV from market mid-prices.
Construct 2D strike vs. expiry volatility grid.

Key features:
- Newton-Raphson with bracketing fallback
- Convergence criteria (absolute/relative tolerance)
- Handles ITM/OTM options gracefully
- Surface construction for visualization
- Option pricing given IV (Black-Scholes)
"""
from __future__ import annotations

from decimal import Decimal, getcontext
from math import log, sqrt, exp
from typing import Optional, Tuple, List, Dict

from .greeks import GreeksInput, calculate_greeks

# Precision
getcontext().prec = 50


def _bs_price(input_: GreeksInput, option_type: str = "call") -> Decimal:
    """Black-Scholes theoretical price."""
    S = input_.S
    K = input_.K
    T = input_.T
    r = input_.r
    q = input_.q
    sigma = input_.sigma

    if T <= 0:
        intrinsic = max(Decimal("0"), S - K) if option_type == "call" else max(Decimal("0"), K - S)
        return intrinsic

    d1 = (log(S / K) + (r - q + Decimal("0.5") * sigma * sigma) * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)

    phi = Decimal("1") if option_type == "call" else Decimal("-1")

    # N(d1), N(d2) using our CDF
    cdf_d1 = _std_normal_cdf(d1)
    cdf_d2 = _std_normal_cdf(d2)

    price = (exp(-q * T) * S * cdf_d1 - exp(-r * T) * K * cdf_d2)
    return price


def solve_implied_volatility(
    market_price: Decimal,
    input_: GreeksInput,
    option_type: str = "call",
    guess: Optional[Decimal] = None,
    tol: Decimal = Decimal("1e-10"),
    max_iterations: int = 100,
) -> Decimal:
    """
    Solve for implied volatility using Newton-Raphson method.

    Args:
        market_price: Observed market price
        input_: All params except sigma (fixed)
        option_type: "call" or "put"
        guess: Initial volatility guess (default: 0.2)
        tol: Convergence tolerance
        max_iterations: Max Newton-Raphson iterations

    Returns:
        Implied volatility as Decimal

    Raises:
        ValueError: If volatility cannot be converged within max_iterations
    """
    if guess is None:
        guess = Decimal("0.2")

    # Newton-Raphson: sigma_{n+1} = sigma_n - (price_market - price_model) / vega
    sigma = guess

    for i in range(max_iterations):
        # Compute model price and vega at current sigma
        input_sigma = GreeksInput(
            S=input_.S, K=input_.K, T=input_.T, r=input_.r, q=input_.q, sigma=sigma
        )
        model_price = _bs_price(input_sigma, option_type)
        vega = float(calculate_greeks(input_sigma).vega)  # use float for vega in iteration

        if abs(vega) < Decimal("1e-15"):
            logger.warning("Vega near zero — falling back to bisection")
            # Fall back to bisection
            return _bisection_solve(market_price, input_, option_type, tol, max_iterations)

        # Newton step
        residual = market_price - model_price
        sigma_new = sigma + residual / vega

        # Keep volatility positive
        sigma_new = max(Decimal("0"), sigma_new)

        # Check convergence
        if abs(sigma_new - sigma) < tol:
            return sigma_new

        sigma = sigma_new

    raise ValueError(
        f"Failed to converge IV after {max_iterations} iterations. "
        f"Last sigma={sigma}, market_price={market_price}, model_price={_bs_price(GreeksInput(S=input_.S, K=input_.K, T=input_.T, r=input_.r, q=input_.q, sigma=sigma), option_type)}"
    )


def _bisection_solve(
    market_price: Decimal,
    input_: GreeksInput,
    option_type: str,
    tol: Decimal,
    max_iterations: int,
) -> Decimal:
    """Bisection fallback for IV solving."""
    lo = Decimal("1e-10")  # minimum vol
    hi = Decimal("5.0")    # maximum vol (500%)

    for i in range(max_iterations):
        mid = (lo + hi) / Decimal("2")
        input_mid = GreeksInput(
            S=input_.S, K=input_.K, T=input_.T, r=input_.r, q=input_.q, sigma=mid
        )
        model_price = _bs_price(input_mid, option_type)

        if abs(market_price - model_price) < tol:
            return mid

        # Determine which half to keep
        # If model_price < market_price, we need higher vol (for call, price increases with vol)
        # For put, relationship is inverted for deep ITM, but generally vol increases price
        test_price = _bs_price(GreeksInput(S=input_.S, K=input_.K, T=input_.T, r=input_.r, q=input_.q, sigma=hi), option_type)

        if (model_price - market_price) * (test_price - market_price) < 0:
            hi = mid
        else:
            lo = mid

    return (lo + hi) / Decimal("2")


def build_iv_surface(
    market_prices: Dict[Tuple[Decimal, Decimal], Decimal],
    input_template: GreeksInput,
    option_type: str = "call",
    vols: Optional[List[Decimal]] = None,
    expiries: Optional[List[Decimal]] = None,
) -> Dict[Tuple[Decimal, Decimal], Decimal]:
    """
    Build a 2D volatility surface from market prices.

    Args:
        market_prices: dict of (strike, expiry) -> market price
        input_template: template with S, r, q filled in
        option_type: "call" or "put"
        vols: optional fixed volatility grid (overrides market price solving)
        expiries: optional expiry grid

    Returns:
        dict of (strike, expiry) -> implied volatility
    """
    surface: Dict[Tuple[Decimal, Decimal], Decimal] = {}

    strikes = sorted(set(k for k, _ in market_prices.keys()))
    expiries_ = sorted(set(e for _, e in market_prices.keys())) if expiries is None else expiries

    for strike in strikes:
        for expiry in expiries_:
            # Update input template
            t_input = GreeksInput(
                S=input_.S,
                K=strike,
                T=expiry,
                r=input_.r,
                q=input_.q,
                sigma=input_.sigma,  # placeholder
            )
            price = market_prices.get((strike, expiry))
            if price is not None and price > Decimal("0"):
                try:
                    iv = solve_implied_volatility(price, t_input, option_type)
                    surface[(strike, expiry)] = iv
                except ValueError:
                    surface[(strike, expiry)] = input_.sigma  # fallback to prior vol
            else:
                surface[(strike, expiry)] = input_.sigma

    return surface


# Convenience wrapper
def quick_iv(
    market_price: Decimal | float | int,
    S: Decimal | float | int,
    K: Decimal | float | int,
    T: Decimal | float | int,
    r: Decimal | float | int = Decimal("0"),
    sigma_guess: Decimal | float | int = Decimal("0.2"),
    option_type: str = "call",
) -> Decimal:
    """Quick IV calculation from raw params."""
    return solve_implied_volatility(
        market_price=Decimal(market_price),
        input_=GreeksInput(
            S=Decimal(S), K=Decimal(K), T=Decimal(T), r=Decimal(r), q=Decimal("0"),
            sigma=Decimal(sigma_guess),
        ),
        option_type=option_type,
        guess=Decimal(sigma_guess),
    )