"""Probabilistic Sharpe Ratio and Deflated Sharpe Ratio.

Bailey & Lopez de Prado (2014). The DSR corrects an observed Sharpe for:
  1. the number of independent configurations tried (multiple testing), and
  2. non-normal returns (skew and kurtosis), and
  3. sample length.

DSR = PSR(SR_0) where SR_0 is the Sharpe you'd expect as the *maximum* of N
trials of zero-skill strategies. DSR > 0.95 is the promotion bar.

Track the trial count honestly - including abandoned ideas. A self-reported
trial count of "1" is how people lie to themselves.
"""

from __future__ import annotations

import numpy as np

from research.validation._normal import norm_cdf, norm_ppf

EULER_MASCHERONI = 0.5772156649015329


def _moments(returns: np.ndarray):
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = len(r)
    mu = r.mean()
    sd = r.std(ddof=1)
    if sd == 0:
        return n, 0.0, 0.0, 3.0
    z = (r - mu) / sd
    skew = np.mean(z**3)
    kurt = np.mean(z**4)  # non-excess
    sr = mu / sd
    return n, sr, skew, kurt


def probabilistic_sharpe_ratio(
    returns: np.ndarray, sr_benchmark: float = 0.0, *, periods_per_year: int | None = None
) -> float:
    """P(true SR > sr_benchmark) given the sample. SR values are per-period
    unless ``periods_per_year`` is given, in which case inputs/benchmark are
    treated as annualised and converted back."""
    n, sr, skew, kurt = _moments(returns)
    if n < 3:
        return 0.5
    if periods_per_year:
        sr = sr  # sr computed per-period above
        sr_benchmark = sr_benchmark / np.sqrt(periods_per_year)
    denom = np.sqrt(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2)
    if denom <= 0:
        return float("nan")
    stat = (sr - sr_benchmark) * np.sqrt(n - 1) / denom
    return float(norm_cdf(stat))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] across ``n_trials`` independent zero-skill strategies whose SR
    estimates have variance ``sr_variance`` (per-period)."""
    if n_trials < 2:
        return 0.0
    e = np.e
    q1 = norm_ppf(1.0 - 1.0 / n_trials)
    q2 = norm_ppf(1.0 - 1.0 / (n_trials * e))
    return np.sqrt(sr_variance) * ((1.0 - EULER_MASCHERONI) * q1 + EULER_MASCHERONI * q2)


def deflated_sharpe_ratio(
    returns: np.ndarray,
    n_trials: int,
    sr_variance: float | None = None,
    *,
    all_trial_sharpes: np.ndarray | None = None,
) -> float:
    """DSR in [0, 1]. Pass either ``sr_variance`` (variance of per-period SR
    estimates across your trials) or ``all_trial_sharpes`` to estimate it.
    """
    if sr_variance is None:
        if all_trial_sharpes is None:
            raise ValueError("provide sr_variance or all_trial_sharpes")
        sr_variance = float(np.var(np.asarray(all_trial_sharpes, dtype=float), ddof=1))
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr0)


def annualise_sharpe(sr_per_period: float, periods_per_year: int) -> float:
    return sr_per_period * np.sqrt(periods_per_year)
