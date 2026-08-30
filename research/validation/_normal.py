"""Standard-normal CDF and inverse CDF without a hard SciPy dependency.

Uses ``math.erf`` for the CDF (exact) and the Acklam rational approximation for
the inverse (|error| < 1.15e-9). If SciPy is installed we defer to it.
"""

from __future__ import annotations

import math

try:  # pragma: no cover
    from scipy.stats import norm as _sp_norm

    def norm_cdf(x: float) -> float:
        return float(_sp_norm.cdf(x))

    def norm_ppf(p: float) -> float:
        return float(_sp_norm.ppf(p))

except ImportError:  # pragma: no cover

    def norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    _A = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
          1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    _B = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
          6.680131188771972e01, -1.328068155288572e01]
    _C = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
          -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    _D = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
          3.754408661907416e00]

    def norm_ppf(p: float) -> float:
        if not 0.0 < p < 1.0:
            if p == 0.0:
                return -math.inf
            if p == 1.0:
                return math.inf
            raise ValueError("p must be in (0, 1)")
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((_C[0]*q+_C[1])*q+_C[2])*q+_C[3])*q+_C[4])*q+_C[5]) / \
                   ((((_D[0]*q+_D[1])*q+_D[2])*q+_D[3])*q+1)
        if p > phigh:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((_C[0]*q+_C[1])*q+_C[2])*q+_C[3])*q+_C[4])*q+_C[5]) / \
                    ((((_D[0]*q+_D[1])*q+_D[2])*q+_D[3])*q+1)
        q = p - 0.5
        r = q * q
        return (((((_A[0]*r+_A[1])*r+_A[2])*r+_A[3])*r+_A[4])*r+_A[5])*q / \
               (((((_B[0]*r+_B[1])*r+_B[2])*r+_B[3])*r+_B[4])*r+1)
