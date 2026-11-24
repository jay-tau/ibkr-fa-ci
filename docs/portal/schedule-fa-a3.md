# Schedule FA Table A3 — portal field semantics

Single source of truth for the field labels, data types, and
validation rules of Schedule FA Table A3 (*Details of Foreign Equity
and Debt Interest*) as accepted by the Indian e-filing portal.

These details drive the per-AY snapshot files under
`src/ibkr_fa/output/portal_labels/` (e.g., `_ay_2026_27.py`) and the
review-CSV writer.

## Provenance

Verified against both official AY 2026-27 V1.2 workbooks:

* [ITR-2 V1.2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR2_AY_26-27_V1.2.zip),
  workbook SHA-256
  `e5ce02f3ffc848d7fb708d4157c6c41af12013e68ee84686715b788c1f9fb785`
* [ITR-3 V1.2](https://www.incometax.gov.in/iec/foportal/sites/default/files/2026-07/ITR3_AY_26-27_V1.2.zip),
  workbook SHA-256
  `863281c4f66aa49e374a39ad1d0fc726e321c2ce3d506fc28d7e2d81c8f80c4c`

In both workbooks the source is sheet `TR_FA`, header row 50, data rows
52–57. Labels, named ranges, number formats, and data validations are
identical. The A3 nature field remains free text with a 34-character
maximum; the utility defines no canonical asset-class tokens.

## Portal column layout

There are **11 data columns** (the e-filing portal calls these
*"fields"*) plus one auto-generated `Sl. No.` column.

| Slot | Portal label                                                                                          | Excel data type | Constraint                          | Internal range name           |
|------|-------------------------------------------------------------------------------------------------------|-----------------|-------------------------------------|-------------------------------|
| —    | Sl. No.                                                                                               | auto increment  | —                                   | —                             |
| 1    | Country Name and Code                                                                                 | dropdown        | values from `Country_NoIndia` list  | `FA_A3_Country`               |
| 2    | Name of entity                                                                                        | text            | max 125 chars                       | `FA_A3_BankName`              |
| 3    | Address of entity                                                                                     | text            | max 200 chars                       | `FA_A3_BankAdd`               |
| 4    | ZIP Code                                                                                              | text            | max 8 chars                         | `FA_A3_ZipCode`               |
| 5    | Nature of entity                                                                                      | text            | max 34 chars (free text)            | `FA_A3_NatureOfEntity`        |
| 6    | Date of acquiring the interest                                                                        | text (`@`)      | max 10 chars; `DD/MM/YYYY`          | `FA_A3_AccOpeningDate`        |
| 7    | Initial value of the investment                                                                       | whole           | min 0                               | `FA_A3_initialvalue`          |
| 8    | Peak value of investment during the Period                                                            | whole           | min -99,999,999,999,999             | `FA_A3_PeakBal`               |
| 9    | Closing balance                                                                                       | whole           | min -99,999,999,999,999             | `FA_A3_ClosingBal`            |
| 10   | Total gross amount paid/credited with respect to the holding during the period                        | whole           | min 0                               | `FA_A3_Totalgrossamount`      |
| 11   | Total gross proceeds from sale or redemption of investment during the period                          | whole           | min 0                               | `FA_A3_Totalgrosproceeds`     |

Internal range names like `FA_A3_BankName` / `FA_A3_BankAdd` /
`FA_A3_AccOpeningDate` are reused from Schedule FA Table A2 (foreign
bank accounts) — the utility's authors didn't rename them for A3. The
CBDT JSON schemas use their own element names (`NameOfEntity`,
`AddressOfEntity`, `InterestAcquiringDate`, ... under
`DtlsForeignEquityDebtInterest` in the vendored
`src/ibkr_fa/reference/cbdt_itr2_schema_ay2026_27.json` and
`src/ibkr_fa/reference/cbdt_itr3_schedule_fa_schema_ay2026_27.json`);
`src/ibkr_fa/output/cbdt_json_fragment.py` maps each row onto them.

## Text length limits

Each Table A3 row refuses, when it is built, text longer than both vendored
AY 2026-27 CBDT schemas allow: country name 55, name of entity 125, address
200, ZIP 8 and nature of entity 34 characters. The run exits 4 with a fixed
message that never shows the value, such as
`error: InvariantError: FaA3Row.name_of_entity must be at most 125 characters`.
A blank address, ZIP or nature, or a whitespace-only name without a control
character, is still allowed by the row for manual fill. The filing CSV still
requires ASCII text.

The limits count the raw text. The filing CSV replaces commas with spaces
and collapses whitespace runs afterwards, so it used to write a 127-character
name made of `B`, 125 spaces and `C` as `B C` while the CBDT JSON refused it;
every format now refuses it. Because the row refuses first, a non-ASCII name over its limit
gets the length refusal rather than the filing CSV's ASCII one.

A name, address, ZIP or nature made only of commas and spaces would leave its
upload cell blank, so the row refuses it too, in every format, with a message
that names only the field, such as
`error: InvariantError: FaA3Row.zip_code must contain a character other than a comma or a space`.
Such a value over its limit gets the length refusal first. An empty or
whitespace-only value is left to the row and output-format checks: an empty
name refuses, and a whitespace-only name that passes those checks files as a
blank upload cell that the run names for manual fill (#1369).

A name, address, ZIP or nature with a control character (a character below a
space, or DEL) is refused too, in every format, because the filing CSV would
turn an interior tab into a space and copy U+0001 or DEL into the upload
file. The message names the field and the position of the first one, never
the value or the character, such as
`error: InvariantError: FaA3Row.name_of_entity has a control character at position 7; filed text may not contain control characters`.
The fields are checked in that order, after every check above, so a value
over its limit keeps the length refusal and `,\t,` keeps the comma refusal.
Only the name can come from a statement;
`user.toml` refuses a control character in a configured value when it loads.

## Bulk-upload CSV differs from manual entry

The downloaded CSV template is misleading. The live AY 2026-27 uploader
accepts exactly 12 fields / 11 commas and expects:

1. `Country/Region name`: row serial number (`1`, `2`, …), not a country.
2. `Country Name and Code`: numeric CBDT code only (`2` for the US).
3. Text fields: ASCII and comma-free; quoting does not protect commas because
   the portal splits on raw delimiters.
4. Acquisition date: strict `YYYY-MM-DD`, not the manual form's
   `DD/MM/YYYY`.
5. Monetary fields: whole-rupee integers.

The downloaded header has a stray trailing comma (false 13th field); retaining
it produces `common.errors.csv_row_skip`. NetBenefits normal output therefore
writes the `filing-csv` upload at `schedule-fa-a3.csv`, paired with
`schedule-fa-a2.csv`. `fidelity-cma-fa --format filing-csv` writes the same
upload at its `--output` path, paired with the `-a2` sibling. Full source/FX
provenance remains in the explicit `review-csv` writer and is never mixed into
the upload file.

The upload's rows and its file dialect are kept apart from the manual-entry
labels in the table above:

* **Schedule rows** (`src/ibkr_fa/output/schedules/fa.py`): the 12-column
  header and one row per holding, numbered from 1 and built only when the
  writer reads it. For each row the country is checked against the selected
  form's dropdown first. The amounts are read from the row's filed whole-rupee
  values next. Then the ASCII and length checks run on the text and the cells
  are rendered in column order.
* **Upload dialect** (`FA_UPLOAD` in `src/ibkr_fa/output/filing_csv.py`), shared
  with A2: UTF-8 without a BOM and CRLF line endings. Every cell is checked for
  a NUL byte, and then the first six cells are checked for a formula trigger.

Quicko does not expose A2/A3 uploads. Its
**Additional Details → Personal Assets & Liabilities → Add Personal Assets →
Shares and Securities → Foreign → Upload Holding Statement → Other Broker**
flow instead receives the separate `schedule-fa-quicko.xlsx` workbook, written
only when `--quicko-workbook` is passed with `filing-csv`. Each A3
holding becomes one row. Purchase Value is the A3 initial value: the FMV at
purchase converted at the acquisition-date TTBR
([ADR 0020](../decisions/0020-quicko-purchase-value.md)); peak, closing,
proceeds, and gross distributions map to Quicko's Peak Value, Fair Market
Value, Sale Value, and Dividend/Interest Income. The A2 account number is
reused. The downloaded
`Personal+Assets+Foreign+Shares+And+Securities+Template.xlsx` must be kept in the
NetBenefits input directory: Quicko rejected a newly-created workbook after
upload, while filling the original package imported 1/1 holdings with 0 errors.
If the requested workbook cannot be written, including when the template is
missing, the run refuses and removes the A2 and A3 files as well.

## Country dropdown

Column 1 ("Country Name and Code") is a dropdown sourced from named
range `Country_NoIndia` on sheet `DropDownValues`.
Each entry has the form `"{cbdt_code}-{COUNTRY_NAME_UPPERCASE}"`,
e.g. `"2-UNITED STATES OF AMERICA"`. The dropdown excludes India
(present in the sister `Country` range used elsewhere in the form).
So a Table A3 row with India's code (`91`) refuses when it is built, for
every broker and output format. The run exits 4 with
`InvariantError: FaA3Row.country_code must not be India (Table A3 is foreign assets only)`.
IBKR, NetBenefits and StockPlan refuse India earlier; a Robinhood security
configured with `issuer_country_code = "IN"` reaches this check (#1325).

The ITR-2 range (`BW3:BW252`) exactly matches the vendored CBDT country
table after excluding India and adding the utility-only `(Select)` and
`9999-OTHERS` entries. The ITR-3 range (`BW3:BW250`) uses the same
codes but omits `967-YEMEN` and `263-ZIMBABWE`; #414 must preserve that
form-specific utility limitation rather than assuming the lists match.

## Date format

The acquisition-date cell is **text-formatted** (`@`) with
`textLength = 10`, which is the Indian convention `DD/MM/YYYY`.
There is no Excel-side date data type and no date picker — the cell
is parsed as a string by the portal's downstream JSON serialiser.

## Decimal / whole-rupee policy

All five monetary columns (Initial value, Peak value, Closing
balance, Total gross amount, Total gross proceeds) are `type=whole`.
The Excel utility rejects fractional input. Practitioner consensus
(ClearTax, Quicko, Anshul Dixit) and CBDT FAQ examples align on
rounding half-up to whole rupees.

The models round: `FaA3Row.filed` (and `FaA2Row.filed` for Table A2)
gives each filed amount in whole rupees (`ROUND_HALF_UP`, never `-0`),
and every writer renders those values without rounding. The row fields
and the compute layer keep full `Decimal` precision, so intermediate
sums stay exact. `format_amount_for_portal` in the active per-AY
snapshot under `src/ibkr_fa/output/portal_labels/` (e.g.,
`_ay_2026_27.py`) only checks and renders a whole-rupee amount.

Each symbol's in-CY dividend total is the exact sum of its payments (#1168,
under the owner's 2026-11-10 decision on #1166). Adding payments in Python's
default 28-digit decimal context could drop a digit and change the filed
rupee: six payments totalling exactly $1.50 at ₹1/$ filed ₹1 instead of ₹2.
The addition widens the precision to at least 1,000 digits instead, preserving
a higher active precision, so it never rounds. A total that can't be added
exactly (an amount that isn't finite, an overflow, or a sum still rounding at
that precision) refuses the run (exit 4), naming the payment's 1-based
position before the calendar-year filter and, when the parser recorded one,
its source row: `InvariantError: Schedule FA per-symbol dividends, input row
2, column amount_gross: sum must be exact within Decimal precision (context
keys: source_row_index=7)`. The December 31 conversion of each total is an
exact product too (#1312): `Schedule FA per-symbol dividends, symbol total
<n>, INR: product must be exact within Decimal precision` refuses one that
can't be computed exactly at that precision.

A broker statement's December 31 holding total feeds the closing balance
directly (#1242). When one open Robinhood lot holds a security's whole
statement total, the closing balance is the exact product of that total and
the December 31 TTBR, and the per-share close kept for review is one division
of the total by the quantity. Dividing first and multiplying back could round
the total down: one 3-share lot worth $1.00 at ₹82.50/$ filed ₹82 instead of
₹83. The peak's floor at the closing value compares the same total. A product
that can't be computed exactly at the active precision widened to at least
1,000 digits refuses the run (exit 4): `InvariantError: Schedule FA A3 closing value, INR:
product must be exact within Decimal precision`. A total split across several
Robinhood lots gives each lot its exact rational share by quantity (#1243),
and the closing balance is that share times the TTBR, filed as its exact
HALF_UP.

Robinhood's carried cost rationals and gross credits convert the same way
(#1243): a cost share with a separate rational, and each lot's share of a credit
split across the lots held on its holding date, stay exact rationals until the
row's filed rounding. A closing or credit share whose operand
has more than 1,000 digits, or a digit more than 1,000 places from the units digit, refuses
the run (exit 4), naming only the configured security's 1-based position: `InvariantError:
Schedule FA A3, Robinhood security 1: the closing share needs more digits than
exact arithmetic allows`, or `the credit share` for a credit. An IBKR or
Fidelity CMA lot that holds part of a purchase carries its exact cost share
the same way (#1244, #1273), and its initial value is that rational times the
acquisition-date TTBR, filed as its exact HALF_UP.

Every other December 31 close and A3 conversion is exact or refuses too
(#1250, under the same rule). A per-share close (IBKR's mark price, a
NetBenefits price override, a Yahoo close) times the quantity, the closing
balance, initial value and gross proceeds conversions to INR (including a cost
without a separate rational), and the close's cross-checks (between sources,
against a NetBenefits purchase's FMV, and IBKR's Activity Statement Close Price
against its Open Positions row's Value, to the cent, #1315) keep every digit;
the only rounding is the filed whole-rupee rounding. A 30-digit price override
for 3.482 shares at ₹89.47/$ filed ₹66,504 instead of ₹66,503, because the
28-digit context rounded the USD total first. A product or difference that
can't be computed exactly at the widened precision refuses the run (exit 4)
with a fixed label, for example `InvariantError: Dec-31 close, override total:
product must be exact within Decimal precision`.

The daily peak is exact the same way (#1260). Each day's close (or, under the
high basis, intraday high) times the quantity, its conversion at its date's
TTBR (the winner's under the native default, every day's under the INR
override), the high basis's comparison of the close peak with the best high,
and the floor at the closing value keep every digit, so the peak day, its ties
and the filed rupee follow the exact amounts. A 30-digit close for 6.375
shares at ₹86.15/$ filed ₹20,001 instead of ₹20,000. A daily value that can't
be computed exactly at the widened precision refuses the run (exit 4):
`InvariantError: Schedule FA A3 peak value, candidate USD: product must be
exact within Decimal precision`, or `Schedule FA peak, INR` for its
conversion.

A Robinhood sale split pro rata across same-day lots leaves each lot an exact
rational quantity, such as two thirds of a share (#1263). The lot's closing and
credit shares split by that quantity, and its peak converts the selected day's
price at it, so three same-day 1-share lots, one share sold, and $3.40 for the
other 2 at ₹82.50/$ file ₹94 closing for every remaining lot. The 20-digit quantities
filed ₹93 for one of them. The peak's day is still selected with the Decimal
quantity kept for review; scaling every day by the same quantity can't change
it. A conversion at the exact quantity that can't be computed exactly refuses
the run (exit 4): `InvariantError: Schedule FA A3 peak value, INR: product
must be exact within Decimal precision`.

## Negative values

`Peak value` and `Closing balance` accept negative integers down to
`-99,999,999,999,999`. The other three monetary fields (`Initial
value`, `Total gross amount`, `Total gross proceeds`) require
`>= 0`.

The compute layer never produces a negative value for any of these
fields under v1 inputs (USD equities only). The wider range on Peak
and Closing exists to accommodate options/short-side edge cases not
in v1 scope.

## Disposed-lot closing balance

For a lot disposed during the calendar year, the closing balance is
written as `0` (the `Decimal("0")` already produced by
`src/ibkr_fa/compute/closing_value.py::closing_value_inr_for_lot` for
`ClosedLot`). Blank fails the `type=whole min ≥ -99,999,999,999,999`
validation in the Excel utility; `0` is universally accepted.

## v1 missing-column rendering

Generic IBKR output can leave these fields for manual verification:

* `Name of entity` — a row takes its ISIN's `[[fa.entities]]` `name`, else the
  statement's security description (for Robinhood, the activity report's
  issuer name unless an automatic identity names the issuer). A whitespace-only
  name that passes the row checks stays in the review CSV and workbook; the
  filing CSV requires ASCII and writes a blank cell. On otherwise valid runs,
  the manual-fill warning names each such holding, Robinhood as an
  `a3-identity` task naming `name`, and the run exits 3 (#1369). An empty row
  name refuses (exit 4).
* `Address of entity` / `ZIP Code` — an IBKR row takes both from its ISIN's
  `[[fa.entities]]` entry. Without an entry both cells stay blank, and an entry
  without `zip` leaves the ZIP blank. The run names each such holding under the
  manual-fill warning and exits 3. IBKR looks no address up, so a held ISIN's
  entry without `address` refuses at run time (exit 4, field
  `fa.entities[i].address`). Before #1289 a row without an entry or an
  automatic identity got its country name as the address; a country name
  doesn't prove the issuer's address, so that default is gone for every source.
  Robinhood uses the legal issuer of the held
  financial instrument and tries GLEIF's exact-ISIN `legalAddress` first. GLEIF
  `headquartersAddress` is never used. The GLEIF record must be uniquely mapped,
  active, issued, successor-free, country-consistent, complete, ASCII, and
  within the portal's 200/8-character limits; GLEIF postal values are never
  truncated.
  A US common stock may then use the SEC filer's principal business address
  under the conditions of
  [ADR 0009's amendments](../decisions/0009-robinhood-legal-addresses.md), but
  only when GLEIF has no record for its ISIN (a verified zero-match) or returns
  a `LAPSED` record that passes every other GLEIF check and whose legal name
  agrees with both SEC names. Any other GLEIF result, such as a country
  mismatch, an ambiguous match or a cache-only miss, blocks the SEC address,
  ZIP and SEC-derived nature. An SEC ZIP+4 is reduced to its ZIP5 for the
  portal; the exact ZIP+4 stays in the immutable cache.
  A Robinhood ADR can instead use its supplied SEC Form F-6 to bind the broker
  issuer to a reviewed depositary LEI; exact GLEIF evidence then supplies the
  depositary name, legal address, ZIP, and legal form. Unreviewed depositaries
  and other records without a usable GLEIF address remain manual unless the SEC
  fallback (US common stock only, after the GLEIF results above) or a reviewed
  `[[fa.entities]]` address/ZIP fills them. `[[fa.entities]]` supplies fallback address/ZIP plus optional
  name/nature. A Robinhood entry without `address` stops Schedule FA for the
  filer to verify the address found for it: the run writes it, or a note that
  none was found, to the private `robinhood-identity-guidance.toml`, writes no
  Schedule FA file and exits 4 (#1449).
  `prefer_configured_address = true` retains one explicitly reviewed configured
  address instead of applying an automatic GLEIF or SEC address. A cold lookup
  discloses the held ISIN to GLEIF; `--no-fetch-prices` is cache-only. A corrupt
  or unreadable GLEIF cache artifact, a failed GLEIF fetch, or a GLEIF response
  with a field of an unexpected JSON type stops the run (exit 4), on the
  exact-ISIN lookup and on the ADR depositary and ETF registrant lookups alike.
  So does a corrupt or unreadable SEC Form F-6 or N-CEN selection cache
  artifact, or any unusable entry in the cached SEC identity snapshots searched
  for the post-FY snapshot, whichever securities that entry covers.
  A registrant record that is valid but unusable (lifecycle, name or country
  mismatch, incomplete address) still leaves its route unresolved, and
  configured `[[fa.entities]]` values still apply.
  A non-ASCII legal address is accepted only through a reviewed per-ISIN ASCII
  alias mechanically constrained to NFD combining-mark removal; the exact
  Unicode source remains cached and a warning is emitted.
  For SEC series ETFs, exact class/series N-CEN evidence selects the
  registrant trust. The filed name is
  `<GLEIF registrant legalName> - <N-CEN series name>` and the address is that
  registrant's legal address. The selected N-CEN period must be on or before
  the reporting calendar-year end; post-period filings are never substituted.
  An ETF's name, address, ZIP and nature come together from this route, or all
  four stay manual: no exact-ISIN GLEIF record fills any of them, and the
  ETF's ISIN is not sent to GLEIF. An ETF with an `[[fa.entities]]` entry gets
  no automatic cell, whatever its `prefer_configured_*` flags say; a cell the
  entry leaves out, including `Name of entity`, stays manual
  ([ADR 0011](../decisions/0011-robinhood-etf-registrants.md)).
  `Address of entity` is the exception: an ETF entry without `address` stops
  Schedule FA as above (exit 4, #1449).
* `Nature of entity` — never inferred from broker `ETF` / `COMMON` / exchange
  metadata because those fields describe the traded instrument. Robinhood
  resolves legal nature independently from address using an exact active GLEIF
  ISO 20275 ELF label, a reviewed reserved/overflow mapping, or exact SEC
  registered-investment-company class and filing evidence. An ETF files
  `Investment Company` only when that SEC evidence names the same CIK, series
  and class as its accepted N-CEN registrant, and never a GLEIF legal form. An
  ADR uses legal form only from its Form F-6-proven depositary record. Unknown, stale,
  unresolved ADR, non-English, or portal-incompatible records remain
  blank/manual.
  `prefer_configured_nature = true` retains one explicitly reviewed configured
  value. Every automatic value records its raw source and transformation.
* `Peak value of investment during the Period` — computed (issue #14) by
  selecting `max(close_usd × quantity)` over each lot's holding window
  and then applying the selected date's SBI TTBR. The warned
  `schedule_fa.peak_currency = "inr"` override instead maximizes each
  day's INR-converted value. Prices come from Yahoo's daily chart API
  (**raw** close — never split/dividend-adjusted). `schedule-fa` fetches
  prices by default; with `--no-fetch-prices` it never accesses the
  network and uses only a complete, integrity-checked price cache
  already on disk. The fetched closes are cached next to the input as an
  immutable audit artifact (source URL, fetch time, covered span, split
  flag, the exact closes used), so re-runs are deterministic and a filed
  return is reproducible. Every cache read checks the cache's integrity
  hash, online or not: a cache with no valid hash, or one that doesn't
  parse or fails a check, refuses the run (exit 4, no output) and is never
  refetched over (#940). A fetched response with two bars on one date
  refuses the run the same way, and nothing is cached (#942). Under the default close basis, an open lot
  acquired on December 31 takes its closing value as its peak. Otherwise
  the cell falls back to manual-fill (empty cell) for any holding whose
  price cannot be established with certainty — for example, no price
  series (not fetched and no complete cache), an unknown/non-USD ticker,
  a stock split after the lot's first in-year day and on or before the
  earlier of its disposal date and December 31 (one static lot quantity
  cannot span it), a split in a price series not normalized to
  contemporaneous per-date prices, or a price gap too large to carry
  forward. A close carries only across weekends and listed US market
  closures: each day needs the latest session's own close, including one
  carried into the holding window. A missing required close leaves the cell
  blank (#939). Closures are listed for 2025 (and 2024-12-25) only; in
  another year an unlisted weekday market holiday counts as a session with
  no close, so a window that spans one, or starts on the weekend or closure
  right after one, is left blank too. A CBDT JSON fragment can't hold a
  blank peak, so `--format cbdt-json-fragment` then writes no
  fragment and exits 3. The peak is never guessed. A fetched USD price bar
  dated on a weekend or a listed US market closure, inside the window the
  selected basis reads or carried into it, instead refuses the run (exit 4,
  no output): the price source and the market calendar disagree, so neither
  is trusted (#895). See `src/ibkr_fa/compute/fa_a3_peak_value.py`.
* `Total gross proceeds from sale or redemption of investment during the period` — closed lots use proven pre-fee consideration (`quantity × execution price`) multiplied by the December 31 SBI TTBR; open lots use `Decimal("0")`. Robinhood files each closed lot's exact share of its sale's activity `Amount`, which may be net of sale fees, as gross proceeds at the same TTBR (owner decision 2026-11-14, #1383; ADR 0006 section 7). Any other closed lot whose source does not prove pre-fee consideration (commission-reduced net cash) remains manual-fill rather than understating gross proceeds. Schedule CG retains its separate Rule 115 conversion.
* `Total gross amount paid/credited with respect to the holding during the period` — computed in v1 when the input is an Activity Statement with a `Dividends` section that resolves to in-CY payments. The in-CY USD payments are summed per symbol and converted at the December 31 SBI TTBR. Otherwise (Flex Query input, or Activity Statement without in-CY dividends) this column stays manual-fill, except that an Activity Statement with neither a `Dividends` nor a `Change in Dividend Accruals` section files `0` for every holding, as IBKR leaves both out when there were none. A statement whose `Change in NAV` contradicts its `Dividends`, `Withholding Tax` or `Interest` section (for example, it shows dividends but the statement has no `Dividends` section) stops the run instead of filing that zero or a total (exit 4, `ChangeInNavIncomeError`, no output; #1324), as in the income commands (#1279); see the README's "Missing income sections and `Change in NAV`".

Peak, gross amount paid/credited and gross proceeds are modelled on `FaA3Row`
as `Decimal | None`; a missing address, ZIP or nature is the empty string.
Writers render missing values as
empty cells. The CLI emits a loud warning enumerating fields and
affected holdings before portal entry.

The decision to render uncomputed fields as an empty cell (rather
than as `0`) was deliberate. An empty cell visually screams "fill me
in" louder than a zero, and if the user forgets to fill in a value
the portal's own validation will reject the row — a useful safety
net.

See the [Schedule FA automation and manual-fill matrix](./schedule-fa-manual-fill.md)
for source-specific gaps and exit behavior.

## FX-policy choice (exact-date vs Rule 115 by analogy)

`ibkr-fa` converts Schedule FA Table A3 USD values using the SBI TTBR
on the acquisition date, peak date, and December 31 for the closing
balance and every gross-credit/proceeds field — `FaExactDatePolicy`. Some
practitioners apply **Rule 115(1), Explanation (2)(f)** by analogy with Schedule CG and
use the preceding-month-end TTBR instead. Both readings are
defensible and the disagreement, in principle, covers every Schedule
FA Table A3 INR cell (`initial_value_inr` / `peak_value_inr` /
`closing_value_inr` / `gross_amount_paid_credited_inr` /
`gross_proceeds_from_sale_inr`).
The peak date is selected in native currency by default; selecting the
maximum daily INR-converted value requires the warned
`schedule_fa.peak_currency = "inr"` override.
Schedule CG is identical between the two. For `closing_value_inr`,
a strict Rule-115-by-analogy reading uses the Nov 30 TTBR (carried
back to the most recent published rate) instead of December 31. See
[`fx-policy-choice.md`](./fx-policy-choice.md) for a worked example
and what to do if your CA prefers the Rule-115-by-analogy reading.

## Re-verification checklist for a future AY

1. Download both current ITR-2 and ITR-3 utility archives from the
   official Downloads page and record their hashes.
2. Open each workbook → sheet `TR_FA` → row range that corresponds to
   A3 (AY 2026-27: rows 49–59).
3. For each label in the table above, copy the cell value verbatim
   and compare to the constant in the matching per-AY snapshot under
   `src/ibkr_fa/output/portal_labels/_ay_YYYY_YY.py`
   (`PORTAL_COLUMN_HEADERS_FA_A3`). If no snapshot exists yet for the
   new AY, create a new `_ay_YYYY_YY.py` (copy the latest snapshot)
   and register it in `src/ibkr_fa/output/portal_labels/__init__.py`
   (`_SNAPSHOTS_BY_AY_START` + bump `CURRENT_AY_START`).
4. For each form's data validation, dump
   `wb['TR_FA'].data_validations.dataValidation` and confirm the
   constraints match the table above.
5. Dump each form's `Country_NoIndia` range and diff it against
   `itr_country_codes.csv` and against the other form.
