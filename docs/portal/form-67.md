# Form 67 upload file

`form-67.csv` is CBDT's official Form 67 bulk-upload file, the portal's
`IncomeDetails.csv` template. `form-67 --format portal-csv`, `run-all`, and
every `*-income` and `*-all` command write it only when every row is ready (see
the README's `[form67]` section). Its columns belong to the uploader, not to
the form's visible fields.

Each command's file covers only the income that command read. When commands
that read different files report the same country and source, the portal needs
one combined row instead, entered by hand; the README's `[form67]` section says
how.

## Upload columns and visible fields

* **Upload columns** (`FORM67_UPLOAD_HEADERS` in
  `src/ibkr_fa/output/schedules/form67.py`): the template's fifteen headers,
  byte for byte, including the trailing spaces in `Please specify ` and
  `Amount `. Country and source of income are numeric codes, such as `2` for
  the United States and `7` for a dividend. The two `Please specify` columns,
  which only country code 9999 and source code 10 (Others) use, stay blank, so
  a row that would need either refuses. The header test compares them with
  the template in `tests/fixtures/form67/IncomeDetails.csv`.
* **Visible fields** (`PORTAL_COLUMN_HEADERS_FORM67` in
  `src/ibkr_fa/output/portal_labels/`): the form's thirteen column labels for
  manual entry, which `form-67-debug.csv`, `--format review-csv` and
  `--format xlsx` show. They never replace the upload headers.

## Upload rows and file dialect

* **Schedule rows** (`form67_upload_rows` in the same module): building the
  writer refuses an unsupported AY. Once the file is staged
  and before the header, a REFUSED row refuses with its reasons and a BLANK row
  refuses the file. Callers refuse REFUSED rows and withhold the file for BLANK
  ones before this; `fidelity-cma-income` also withholds it while its FTC is
  blocked (#1453). Each entry is then built only when it is read: the country
  Others refusal, the country code against the vendored country reference, the
  source Others refusal, then `.filed`'s whole-rupee amounts and two-place
  rates in column order, rendered without rounding again.
* **Upload dialect** (`FORM67_UPLOAD` in `src/ibkr_fa/output/filing_csv.py`):
  UTF-8 without a BOM and LF line endings. Every cell is checked for a NUL byte
  and then for a formula trigger. The shared portal CSV renderer stages the
  file and publishes it only after the last row.

## Importer warnings

`fractional_rate_warnings` and `portal_import_warnings` live with the rows.
The live importer may blank a fractional `Rate(%)`, so every entry with one
gets its own warning, in entry order, even when two warnings read the same.
`portal_import_warnings` then adds the missing-Indian-tax warning once when any
entry leaves its Indian tax payable blank. The Robinhood, Fidelity CMA,
NetBenefits and StockPlan income pipelines call `portal_import_warnings`;
Robinhood's then drops repeated lines from its whole warning list, as before.
`form-67` calls `fractional_rate_warnings` and prints the missing-Indian-tax
warning itself; `run-all` prints that warning once for the whole run.

The rate test is `Form67FiledValues.tax_paid_rate_is_fractional`, and
`Form67ComputeResult.ready_with_fractional_rate` is true when Form 67 is READY
and any row files such a rate (#1447). When the selected format writes the
upload `form-67.csv`, it keeps the exact rate, and the filer enters that rate
by hand after upload, so every command that writes the upload reports manual
work: `form-67` (`portal-csv`), `run-all` under its default format and every
`*-income` command exit 3, and each `*-all` command shows its income leg as manual work. Fidelity
CMA already counted every warning. Formats that write no upload file keep their
exit code.
