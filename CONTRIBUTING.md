# Contributing to ibkr-fa

> ⚠️ This project produces output for tax filings. Correctness is non-negotiable. Read this entire document before opening a PR.

## Disclaimer first

Nothing in this repository — code, documentation, examples, comments — constitutes tax advice. Maintainers and contributors do not accept liability for filings produced using this software. Every PR is reviewed under that lens: behaviour the user might rely on for a tax filing must be either correct, or fail loudly.

## Workflow

### One branch per todo, one PR per branch

- `main` is protected: linear history required, no force-pushes, no deletions.
- Every change lands via a pull request that gets **squash-merged** to `main`. One PR ⇒ one commit on `main`.
- The repo settings allow **only** squash-merge (merge commits and rebase-merge are disabled at the repo level).
- Branch off the latest `main`. If your branch falls behind, **rebase** onto `main` (`git rebase main`) — never `git merge main`.
- Branch naming: `<type>/<short-id>` matching the conventional-commit type (see below).
  - Examples: `feat/parser-open-positions`, `chore/repo-bootstrap`, `ci/github-actions`, `docs/contributing`, `test/e2e-snapshot`.

### Conventional Commits

Every commit (which is also every PR title, since we squash-merge) must follow the [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) format:

```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

- **Subject** ≤ 72 characters; imperative mood; no trailing period.
- **Body** explains *why*, not *what*. Wrap at 72 columns.
- **Footers** include `Closes #NN`, `Refs #NN`, and `Co-authored-by:` trailers.

#### Types

| Type | When to use |
|---|---|
| `feat` | A new feature visible to the user (CLI flag, output column, parser section). |
| `fix` | A bug fix that changes observable behaviour. |
| `refactor` | Code change that neither fixes a bug nor adds a feature. |
| `test` | Adding or correcting tests; no behaviour change. |
| `docs` | Documentation only (README, comments, this file). |
| `chore` | Tooling, repo housekeeping, dependency bumps not part of `build`. |
| `ci` | GitHub Actions and pre-commit configuration. |
| `build` | Build system, packaging (`pyproject.toml`, `uv_build`). |
| `perf` | Performance improvement. |
| `style` | Formatting only (rare — `ruff format` runs in pre-commit). |
| `revert` | Reverts a previous commit. Body must reference the reverted SHA. |

#### Scopes

Pick the most specific scope that fits:

| Scope | Used for |
|---|---|
| `parser` | Flex CSV section parsers |
| `compute` | Pure computation (lot overlap, generic helpers) |
| `fa-a3` | Schedule FA Table A3 specifics |
| `netbenefits` | Fidelity NetBenefits ESPP pipeline (lots CSV / confirmation / statement parsers → FA A2/A3, CG, OS) |
| `fa` | Schedule FA tables A1/A2/A4–A10 (generic FA work) |
| `cg` | Schedule CG (capital gains) work |
| `os` | Schedule OS (other sources — dividends, interest) |
| `fsi` | Schedule FSI (foreign source income) |
| `tr` | Schedule TR (tax relief) |
| `form67` | Form 67 (foreign tax credit claim) |
| `fx` | FX policies and TTBR loading |
| `ttbr` | Vendored TTBR data and refresh script |
| `prices` | Daily price fetchers and cache |
| `output` | Output writers and registry |
| `cli` | argparse CLI and subcommands |
| `models` | Domain dataclasses and exception hierarchy |
| `config` | `user.toml` loader |
| `reference` | Bundled reference data (country codes, IBKR entities, etc.) |
| `pipeline` | Top-level orchestration |
| `reconcile` | AIS/TIS/TCS reconciliation features |
| `compliance` | Black Money Act warnings, ITR-2-vs-3 guidance, AY rollover helpers |
| `arch` | Architecture documentation (used with `docs`) |
| `correctness` | Fail-fast invariants and runtime correctness checks |
| `gates` | Research-gate documents (used with `docs`) |
| `deps` | Dependency changes (used with `chore`) |
| `repo` | Repo-level housekeeping (used with `chore`) |

Omit the scope only when no scope applies (e.g., `docs: add CONTRIBUTING.md`).

#### Examples

```
feat(parser): parse Open Positions LOT section with hard input validation
fix(fx): handle TTBR carry-forward across year boundary
chore(deps): bump ruff to 0.8.4
ci: add correctness scope to PR-title vocabulary
docs(gates): G2 — confirm A3 entity metadata semantics for US-listed equities
```

### Pull requests

- PR title = the conventional-commit subject. The squash-merge button uses it verbatim, so get it right.
- PR description follows the [PR template](.github/PULL_REQUEST_TEMPLATE.md) and becomes the squash-commit body. Include:
  - Linked issue (`Closes #NN` if applicable).
  - Summary of the change.
  - How you tested it (commands run, fixtures used).
- Resolve all conversations before merging.
- Do not merge your own PRs without ensuring CI is green.

### Co-author trailer

Commits authored or assisted by GitHub Copilot include:

```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

This is automatic when committing through Copilot CLI; add it manually if you commit by hand and Copilot helped.

## Local setup

Install [`uv`](https://docs.astral.sh/uv/) (`pipx install uv`), then:

```bash
git clone git@github.com:jay-tau/ibkr-fa.git
cd ibkr-fa
uv sync                   # creates .venv and installs the package + dev deps
uv run pre-commit install # installs the pre-commit hooks
```

Run the test suite, linter, formatter, and type checker locally before opening a PR:

```bash
uv run pytest --cov
uv run ruff check
uv run ruff format --check
uv run ty check
```

CI runs the same checks on Python 3.13 for same-repository pull requests (pull requests from forks are skipped) and on Python 3.12, 3.13 and 3.14 for pushes to `main`; every uv step of a job runs that job's Python, a uv-managed CPython.

Run local 3.13 and 3.14 suites on uv-managed CPython, for example `uv python install 3.14`, then `uv run --managed-python --python 3.14 pytest`. A distribution's Python may link zlib-ng, whose deflate output differs from stock zlib's, so the workbook byte checks fail on it.

## Code style and typing

- **Python ≥ 3.12.** Use [PEP 695](https://peps.python.org/pep-0695/) generics and modern syntax.
- **Fully type-annotated.** No implicit `Any`. `ty check` runs in strict mode.
- **`Decimal`** for all money and quantity. Never `float`.
- **`datetime.date`** for FA dates (timezone-naive calendar dates). `datetime.datetime` only when intra-day precision matters.
- **`NewType`** for identifiers (`Isin`, `Ticker`, `AccountId`) to prevent string-mixing bugs.
- **`StrEnum`** for closed sets of string values (IBKR side codes, FA statuses).
- **`@dataclass(frozen=True, slots=True)`** for value objects.
- **Custom exceptions** descend from `IbkrFaError`; no bare `except`.

## Tests

- Use `pytest` (with `hypothesis` for property-based tests where useful).
- Coverage threshold ≥ 90 % across `ibkr_fa` (`--cov-fail-under=90`; CI fails otherwise).
- Test rows, fixtures and docs examples are synthetic. Never copy a row, value,
  security, name, date or count from a real statement, an export or `data/`,
  redacted or not. Use made-up securities with check digits from
  `ibkr_fa.types` (`isin_check_digit`, `cusip_check_digit`) and the synthetic
  holder name `Sample Holder`.
- Made-up tickers and names stay neutral (`XQAA`, `SAMPLE ISSUER AA INC`) and never hint at what they replace.
- A test that reads private data (`tests/_private_data.py`) asserts only
  file-relative properties: it parses, it reconciles, signs agree, and sums
  match the file's own totals. It never asserts the data's composition: no
  count, presence, absence, set, mix, date or amount, even one computed at run
  time and compared to a constant. Those scenarios go in synthetic tests.
- `tests/test_fixture_holder_name.py` fails if the maintainer's name appears in
  a tracked file anywhere but the `pyproject.toml` author field and the README
  copyright line.

## Issues

- **Tracking issues** (parent issues) carry one of the `area:*` labels. Each tracker uses GitHub's native [Sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/managing-sub-issues) feature to attach atomic-unit children.
- **Atomic-unit issues** (children) describe one PR-sized change. Title = the conventional-commit subject the eventual PR will use.
- File a new sub-issue under the relevant tracker via `gh issue create --label area:<area>` followed by `gh issue edit <child> --add-parent <parent>`.

## Releases

Not in v1 scope. The first release is cut once Tracking Issue 1 (`area:fa-a3-complete`) closes.
