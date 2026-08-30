"""Research plane: the backtester (same core code as live) and the validation
pipeline (CPCV, walk-forward, DSR, PBO).

No credentials for the live broker exist here. The only artifact that crosses
into the execution plane is an approved, versioned strategy config.
"""
