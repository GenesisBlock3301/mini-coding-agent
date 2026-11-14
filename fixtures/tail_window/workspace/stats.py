"""Small helpers behind the ops dashboard."""


def mean(values):
    """The average of values, or 0.0 for an empty list."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def tail(values, n):
    """The last n values, oldest first."""
    if n <= 0:
        return []
    return values[-n + 1:]
