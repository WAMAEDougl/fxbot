"""Moving-average crossover - the trivial strategy from the build order (step 3).

Expect it to lose money after costs. That is the point: it is a test harness for
the whole pipeline, not an edge.
"""

from strategies.ma_crossover.config import MaCrossoverConfig
from strategies.ma_crossover.strategy import MaCrossover

__all__ = ["MaCrossover", "MaCrossoverConfig"]
