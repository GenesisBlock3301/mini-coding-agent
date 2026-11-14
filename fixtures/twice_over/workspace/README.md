# shipping fees

`shipping.py` prices one parcel. The rate card, which is not negotiable:

- Domestic: parcels of **10 kg and over** pay the heavy fee; lighter parcels
  pay the standard fee.
- International: parcels **over 10 kg** pay twice the heavy fee; parcels of
  10 kg or less pay twice the standard fee.

The two rules differ at exactly 10 kg. That is intentional and the
international rule is correct as written.
