"""Feature registration + versioning.

``@feature`` registers a pure function ``f(df, **params) -> pd.Series`` where
``df`` is a validated bar frame (columns: ts, open, high, low, close, volume).
The version hash is the SHA-1 of the dedented source plus the param signature -
change the code and every downstream signal's ``features_hash`` changes.
"""

from __future__ import annotations

import hashlib
import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

FeatureFn = Callable[..., pd.Series]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    fn: FeatureFn
    version: str
    doc: str

    def compute(self, df: pd.DataFrame, **params) -> pd.Series:
        s = self.fn(df, **params)
        s.name = self.name
        return s


FEATURES: dict[str, FeatureSpec] = {}


def _version_of(fn: FeatureFn) -> str:
    src = textwrap.dedent(inspect.getsource(fn))
    sig = str(inspect.signature(fn))
    return hashlib.sha1(f"{src}||{sig}".encode()).hexdigest()[:12]


def feature(name: str | None = None) -> Callable[[FeatureFn], FeatureFn]:
    def deco(fn: FeatureFn) -> FeatureFn:
        fname = name or fn.__name__
        if fname in FEATURES:
            raise ValueError(f"duplicate feature registration: {fname}")
        FEATURES[fname] = FeatureSpec(
            name=fname,
            fn=fn,
            version=_version_of(fn),
            doc=(fn.__doc__ or "").strip(),
        )
        return fn

    return deco


def compute(name: str, df: pd.DataFrame, **params) -> pd.Series:
    return FEATURES[name].compute(df, **params)


def features_hash(names: list[str], params: dict | None = None) -> str:
    """Stable hash over a set of feature versions + params, for a Signal."""
    params = params or {}
    parts = []
    for n in sorted(names):
        spec = FEATURES.get(n)
        v = spec.version if spec else "MISSING"
        parts.append(f"{n}:{v}:{params.get(n, '')}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def assert_causal(name: str, df: pd.DataFrame, **params) -> None:
    """A feature is causal if truncating the input at row k does not change the
    feature value at any row <= k. Raises AssertionError on a leak.
    """
    full = FEATURES[name].compute(df, **params).to_numpy()
    n = len(df)
    for k in (n // 3, n // 2, (2 * n) // 3, n - 1):
        if k < 1:
            continue
        trunc = FEATURES[name].compute(df.iloc[: k + 1], **params).to_numpy()
        a, b = full[: k + 1], trunc
        mask = ~(pd.isna(a) & pd.isna(b))
        if not pd.Series(a[mask]).equals(pd.Series(b[mask])):
            raise AssertionError(
                f"feature '{name}' leaks: values at/<= row {k} change when future "
                f"rows are removed"
            )
