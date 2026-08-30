"""Validation pipeline (design section 5). Most engineering effort belongs here.

* ``purged_kfold`` - purging + embargo primitives (Lopez de Prado)
* ``cpcv``         - Combinatorial Purged Cross-Validation -> a *distribution* of Sharpes
* ``walkforward``  - rolling / anchored walk-forward, the realism check
* ``dsr``          - Probabilistic + Deflated Sharpe Ratio (multiple-testing correction)
* ``pbo``          - Probability of Backtest Overfitting via CSCV
"""

from research.validation.cpcv import CPCV, cpcv_paths
from research.validation.dsr import deflated_sharpe_ratio, probabilistic_sharpe_ratio
from research.validation.pbo import pbo_cscv
from research.validation.purged_kfold import PurgedKFold
from research.validation.walkforward import WalkForward

__all__ = [
    "PurgedKFold",
    "CPCV",
    "cpcv_paths",
    "WalkForward",
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
    "pbo_cscv",
]
