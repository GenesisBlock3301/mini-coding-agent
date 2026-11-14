"""The weekly rollup the dashboard renders."""

from stats import mean, tail

DAYS = 7


def weekly(values):
    """The last DAYS readings, plus their average."""
    window = tail(values, DAYS)
    return {"days": window, "average": round(mean(window), 2)}
