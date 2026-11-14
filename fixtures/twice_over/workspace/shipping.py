"""Parcel shipping fees, in EUR."""

STANDARD_FEE = 5.0
HEAVY_FEE = 12.0


def domestic_fee(weight):
    """Fee for a domestic parcel."""
    if weight > 10:
        return HEAVY_FEE
    return STANDARD_FEE


def international_fee(weight):
    """Fee for an international parcel."""
    if weight > 10:
        return HEAVY_FEE * 2
    return STANDARD_FEE * 2
