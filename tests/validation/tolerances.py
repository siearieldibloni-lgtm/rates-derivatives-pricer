"""Validation tolerances against QuantLib: the single place where they are defined.

Starting values from docs/CONVENTIONS.md section 5. Any change must be justified by observed
differences and recorded in docs/DECISIONS.md (ADR-014).
"""

# Day counts: year fractions on a grid of date pairs.
YEAR_FRACTION_ABS = 1e-14

# Bootstrapped curve: discount factors at and between pillars.
DISCOUNT_FACTOR_ABS = 1e-10

# Cashflows: projected overnight coupon amounts (relative).
CASHFLOW_REL = 1e-9

# Swap PV, par rate and annuity (relative). PVs are compared relative to the notional, since the
# PV of a swap close to par is close to zero and a pure relative comparison would be meaningless.
SWAP_REL = 1e-8

# Property test: a swap struck at its par rate has a zero PV (absolute, per unit of notional).
PAR_SWAP_PV_PER_NOTIONAL = 1e-10

# Bucket DV01 (bump-and-reprice of calibration quotes) against QuantLib finite differences on the
# same quotes. Compared per unit of the trade's absolute parallel DV01: a bucket far from the
# trade has a DV01 close to zero, for which a pure relative comparison would be meaningless.
DV01_REL = 1e-6

# Internal consistency: the key-rate DV01s add up to the parallel DV01. The two are the same
# first derivative evaluated with different bumps, so they differ by the third-order terms of the
# central scheme; relative to the parallel DV01 (documented in ADR-029).
KEY_RATE_SUM_REL = 1e-6
