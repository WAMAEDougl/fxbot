"""Causal feature store (design 4.2).

One job: prevent lookahead bias. Every feature is a pure function of data at or
before timestamp ``t``. Each feature carries a version hash derived from its
source; that hash is threaded into every ``Signal`` so any trade traces back to
exactly which code computed its inputs.

Rule: if a feature can't be computed in a streaming loop with only past data, it
does not go in the store. ``assert_causal`` enforces this in tests.
"""

from data.features import library  # noqa: F401  (registers built-ins)
from data.features.registry import FEATURES, FeatureSpec, feature, features_hash

__all__ = ["FEATURES", "FeatureSpec", "feature", "features_hash"]
