# Rounding and Precision Standard

This repository adopts the platform-wide mandatory standard defined in `pbwm-platform-docs/Financial Rounding and Precision Standard.md` and RFC-0063.

## Local Enforcement

- Monetary/financial calculations use `Decimal`.
- Intermediate calculations do not round.
- Output boundaries apply canonical scale + `ROUND_HALF_EVEN` via `precision_policy` helpers.
- Any change to rules requires RFC approval in PPD.
