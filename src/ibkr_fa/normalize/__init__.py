"""Normalize layer: reconcile parsed source documents into source evidence.

Each source package takes :mod:`ibkr_fa.parser` and :mod:`ibkr_fa.config`
types and returns reconciled source evidence for the compute layer.

This layer never imports :mod:`ibkr_fa.compute`, :mod:`ibkr_fa.output`,
``ibkr_fa.pipeline*`` or :mod:`ibkr_fa.cli`.

* :mod:`ibkr_fa.normalize.robinhood` — Robinhood cross-document
  reconciliation of activity reports and monthly statements.
* :mod:`ibkr_fa.normalize.fidelity_cma` — Fidelity CMA monthly statements
  checked against the year-end report and Form 1042-S.
* :mod:`ibkr_fa.normalize.netbenefits` — Fidelity NetBenefits statement
  dividends and withholding, deduplicated and matched.
* :mod:`ibkr_fa.normalize.form_1042s` — Form 1042-S form-level checks and
  whole-dollar rounding shared by every issuer.
"""
