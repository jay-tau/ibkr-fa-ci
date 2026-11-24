# Third-Party Notices

This file records provenance, license information and attribution for
third-party data used by `ibkr-fa`. Tables that `ibkr-fa` compiles itself,
such as `src/ibkr_fa/reference/dtaa_dividend_rates.csv`, list their sources
in their `.source` files. Python package dependencies are tracked in
`pyproject.toml` and `uv.lock`.

---

## SBI TT-Buying Reference Rates (USD → INR)

**Bundled file:** `src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv`
(refreshed by `scripts/refresh_ttbr.py`)

**Upstream:** [`sahilgupta/sbi-fx-ratekeeper`](https://github.com/sahilgupta/sbi-fx-ratekeeper)

**Credits:** The State Bank of India publishes the rates. The upstream README
credits data before December 2022 to
[Shivam Khandelwal's SBI rate PDF archive](https://github.com/skbly7/sbi-tt-rates-historical),
the forex rate card archives of Maneesh K. Singh & Co., and the Internet
Archive Wayback Machine.

**Why bundled:** A pinned USD rate snapshot makes the tool's FX lookups
deterministic and offline-capable.

**License:** MIT, as recorded in the sibling `.source` file.

**Retained license notice:**

```
MIT License

Copyright (c) 2022 Sahil Gupta

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**Verification:** Each refresh records the upstream commit SHA in
`src/ibkr_fa/reference/sbi_ttbr_usd_inr.csv.source` so the vendored data
can be reproduced using the transformations documented there.

**Original SBI PDFs:** The CSV records source PDF links in its `PDF FILE`
column. The PDFs are not bundled; users should retain source copies for
audit when filing.

---

## ITR-2 Country Codes

**Bundled file:** `src/ibkr_fa/reference/itr_country_codes.csv`
(provenance in the sibling `.source` file)

**Source:** The official CBDT ITR-2 Excel utility for AY 2025-26
(`ITR2_AY_25-26_V1.0`), sheet `DropDownValues` — the union of its three
country named ranges, published on
[`incometax.gov.in`](https://www.incometax.gov.in/iec/foportal/).
`cbdt_code` and `country_name_portal` are the CBDT values verbatim;
`ibkr-fa` added the ISO 3166-1 `iso_alpha2` key and the title-case
`country_name`.

**License:** No license statement is recorded in the bundled CSV or its
provenance. The e-filing portal's
[Copyright Policy](https://www.incometax.gov.in/iec/foportal/using-the-portal/webSitePolicies)
sets conditions for reproducing material featured on the portal. Reproduced
with attribution to the Central Board of Direct Taxes (CBDT).

---

## CBDT ITR-2 JSON Schema (AY 2026-27)

**Bundled file:** `src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json`
(provenance + sha256 in the sibling `.source` file)

**Source:** The official JSON Schema for Income-tax Return form ITR-2,
Assessment Year 2026-27, Main V1.2 (released 2026-08-13), published by the
Central Board of Direct Taxes (CBDT) under *Downloads* on
[`incometax.gov.in`](https://www.incometax.gov.in/iec/foportal/downloads/income-tax-returns).
Vendored with line-ending and trailing-whitespace normalization and pinned
by sha256.

**Why bundled:** `ibkr-fa` validates Schedule FA A2/A3 items against this
schema before writing a JSON fragment for merging into a full return.
Bundling makes that validation deterministic and offline-capable.

**License:** No license statement is recorded in the bundled schema or its
provenance. The e-filing portal's
[Copyright Policy](https://www.incometax.gov.in/iec/foportal/using-the-portal/webSitePolicies)
sets conditions for reproducing material featured on the portal. Reproduced
with attribution to the Central Board of Direct Taxes (CBDT).

---

## CBDT ITR-3 JSON Schema — Schedule FA extract (AY 2026-27)

**Bundled file:** `src/ibkr_fa/reference/cbdt_itr3_schedule_fa_schema_ay2026_27.json`
(provenance, sha256s, and the extraction command in the sibling `.source` file)

**Source:** Validation-equivalent extract (Tables A2/A3 definitions, their
`$ref` closure, and `Form_ITR3`; descriptions removed) of the official CBDT
ITR-3 AY 2026-27 Main V1.1 JSON Schema, published under *Downloads* on
[`incometax.gov.in`](https://www.incometax.gov.in/iec/foportal/downloads/income-tax-returns).

**License:** No license statement is recorded in the bundled schema or its
provenance. The e-filing portal's
[Copyright Policy](https://www.incometax.gov.in/iec/foportal/using-the-portal/webSitePolicies)
sets conditions for reproducing material featured on the portal. Reproduced
with attribution to the Central Board of Direct Taxes (CBDT).

---

## GLEIF Legal Entity Reference Data

**Runtime source:** [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api)

`ibkr-fa` retrieves exact-ISIN legal-entity records at runtime for Robinhood
Schedule FA review and stores private immutable cache artifacts. The ISO 20275
ELF code/English-label table is also vendored at
`src/ibkr_fa/reference/gleif_elf_codes.csv`, with retrieval details and hashes
in its `.source` companion file. That provenance records the
[GLEIF LEI Data Terms of Use](https://www.gleif.org/en/meta/lei-data-terms-of-use/).

---

## License of `ibkr-fa` itself

`ibkr-fa` is distributed under the GNU Affero General Public License,
version 3 or (at your option) any later version (`AGPL-3.0-or-later`) — see
[`LICENSE`](./LICENSE).

---

When redistributing `ibkr-fa` or its bundled data, keep the source
acknowledgements above with it. The e-filing portal's Copyright Policy
requires the source of reproduced portal material to be prominently and
correctly acknowledged wherever it is published or issued to others. The
recorded MIT license requires the copyright and permission notice above to
be included in all copies or substantial portions of the SBI TTBR data.
