"""One module per venue. The rest of the system never sees a venue-specific format.

* ``synthetic`` - GBM bar generator, no network. The default for tests and for
  exercising the backtester / validation pipeline before wiring a real feed.
* ``csv_feed``  - load bars from a CSV/Parquet file you already have.
* ``ibkr``      - Interactive Brokers (equities + spot FX) via ``ib_async``.
"""
