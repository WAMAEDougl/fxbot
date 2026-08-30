"""Risk manager - the mandatory gate (design 4.4).

Every ``Signal`` passes through ``RiskManager.evaluate``. It can clip, block, or
annotate - never pass through blindly. Position size is *derived* from risk
budget and stop distance, never configured directly.
"""

from risk.manager import RiskConfig, RiskDecision, RiskManager
from risk.sizing import SizingParams, size_from_risk

__all__ = [
    "SizingParams",
    "size_from_risk",
    "RiskConfig",
    "RiskManager",
    "RiskDecision",
]
