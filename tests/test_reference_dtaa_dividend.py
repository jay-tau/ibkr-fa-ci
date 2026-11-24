"""Tests for :mod:`ibkr_fa.reference.dtaa_dividend`."""

from __future__ import annotations

from decimal import Decimal
from importlib import resources

import pytest

import ibkr_fa.reference.dtaa_dividend as dtd
from ibkr_fa.exceptions import IbkrFaError, InvariantError
from ibkr_fa.reference.dtaa_dividend import (
    DtaaApplicability,
    DtaaDividendEntry,
    load_dtaa_dividend_rates,
    lookup_dtaa_dividend,
)

# ============================================================================
# Happy path
# ============================================================================


def test_load_dtaa_dividend_rates_returns_dict_keyed_by_iso() -> None:
    table = load_dtaa_dividend_rates()
    assert isinstance(table, dict)
    assert "US" in table
    entry = table["US"]
    assert isinstance(entry, DtaaDividendEntry)


def test_us_row_matches_irs_treaty_pdf_verbatim_authoritative_source() -> None:
    """US is DTAA, Article 10(2)(b) / 25(2)(a), 25%. Verified
    verbatim against the IRS-hosted India treaty PDF at
    https://www.irs.gov/pub/irs-trty/india.pdf (Article 10 lines
    1177-1292 and Article 25 lines 2351-2464 of pdftotext -layout
    extract).

    The 25% rate is the cap for "all other cases" under Article
    10(2)(b) — including portfolio Indian individuals. The 15%
    rate under Article 10(2)(a) applies ONLY to corporate
    beneficial owners holding >= 10% voting stock; vendoring 15%
    would be wrong for the typical IBKR-investor case (an
    individual whose holding never approaches 10% of any
    listed-company's voting stock)."""
    entry = lookup_dtaa_dividend("US")
    assert entry is not None
    assert entry.iso_alpha2 == "US"
    assert entry.country_name == "United States"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)(b)"
    assert entry.relief_article == "Article 25(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("25")
    assert "portfolio" in entry.applicability_notes.lower()
    assert "irs.gov" in entry.source_citation


def test_spain_row_matches_india_es_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Spain is DTAA, Article 11(2) / 25(2)(a), 15%. Verified
    verbatim against the taxsutra-hosted India-Spain DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/SPAIN%20DTAA.pdf
    (Article 11 lines 230-253 and Article 25 lines 464-517 of
    pdftotext -layout extract; Article 12 begins at line 254 and
    Article 26 at line 518; MFN clause (royalties/FTS only — NOT
    dividends) in Protocol Section 7 at lines 643-651).

    UNUSUAL ARTICLE NUMBERING (like India-France): in this
    treaty Dividends is Article 11 (NOT Article 10 as in most
    Indian DTAAs); Article 10 is Associated Enterprises. Among
    12 vendored countries this pattern applies to FR (10%) and
    ES (15%).

    RATE 15%: Spain has a HIGHER dividend rate than the modern
    10% caps because the treaty was signed 1993-02-08 / notified
    1995-04-21 (older era before the international trend toward
    lower dividend WHT rates). Other 15%-rate vendored countries
    are India-Japan and India-Singapore (the (2)(b) sub-paragraph
    rate).

    Article 25 paragraph 1 = general savings clause; paragraph 2
    = Indian-resident relief sub-paragraph (a) which is the
    income-tax credit clause with (i) income tax and (ii) capital
    tax sub-sub-paragraphs (capital tax out of scope) and (b)
    exempt-income rate-progression; paragraph 3 = Spanish-
    resident relief; paragraphs 4-5 = tax-sparing for Spanish
    residents (NOT relevant). The Schedule FSI relief citation is
    "Article 25(2)(a)" — structurally similar to India-US /
    India-Sweden / India-Japan / India-Israel.

    Spain IS the FOURTH MFN-list country vendored (after NL
    #241 / FR #244 / SE #245). Per CBDT Circular 3/2022 India
    has MFN clauses with 6 treaty partners: NL / FR / CH / SE /
    ES / HU.

    CRITICAL NUANCE — the India-Spain MFN clause (Protocol
    Section 7) covers ONLY royalties and fees for technical
    services; it does NOT extend to dividends. The MFN clause
    enumerates only "royalties or fees for technical services" —
    dividends are NOT included. Therefore the 2023 Indian
    Supreme Court Nestlé-case ruling (which addressed dividend
    MFN auto-application via NL/FR treaties) does NOT affect
    the India-Spain dividend rate vendoring. The literal 15%
    dividend rate per Article 11(2) stands without MFN
    considerations.

    This is the FIRST vendored MFN-list country where the MFN
    clause does NOT cover dividends. Spain is on the CBDT
    Circular 3/2022 list because it HAS an MFN clause (for
    royalties); but the MFN-Nestlé framework that protected
    the literal NL/FR/SE 10% dividend rates is INAPPLICABLE
    here because Spain's MFN clause never reached dividends in
    the first place."""
    entry = lookup_dtaa_dividend("ES")
    assert entry is not None
    assert entry.iso_alpha2 == "ES"
    assert entry.country_name == "Spain"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 11(2)"
    assert entry.relief_article == "Article 25(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("15")
    assert "spain" in entry.applicability_notes.lower()
    assert "mfn" in entry.applicability_notes.lower()
    assert "royalties" in entry.applicability_notes.lower()
    assert "article 11" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_sweden_row_matches_india_se_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Sweden is DTAA, Article 10(2) / 24(2)(a), 10%. Verified
    verbatim against the taxsutra-hosted India-Sweden DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/SWEDEN%20DTAA.pdf
    (Article 10 lines 246-269 and Article 24 lines 501-549 of
    pdftotext -layout extract; Article 11 begins at line 271 and
    Article 25 at line 550; MFN clause in Protocol "With
    reference to Articles 10, 11 and 12" at lines 702-708).

    The 10% rate is a SINGLE-RATE cap (Article 10(2) — no
    substantial-holder split) — structurally similar to
    India-Germany / India-Ireland / India-Israel / India-UAE
    flat 10% caps.

    Article 24 = Elimination of Double Taxation with paragraph 1
    = general savings clause; paragraph 2 = Indian-resident
    relief sub-paragraphs (a)(b)(c) where (a) is the income-tax
    credit clause (b) is exempt-income rate-progression and (c)
    is capital tax (out of Form 67 scope); paragraph 3 =
    Swedish-resident relief sub-paragraphs (a)-(f) including
    tax-sparing provisions in (d)-(e). The Schedule FSI relief citation is
    Article 24(2)(a) — structurally similar to India-Japan
    Article 23(2)(a) and India-Israel Article 24(2)(a).

    Sweden IS the THIRD MFN-list country vendored (after NL
    #241 and FR #244). Per CBDT Circular 3/2022 India has MFN
    clauses with 6 treaty partners: Netherlands / France /
    Switzerland / Sweden / Spain / Hungary. The Protocol "With
    reference to Articles 10, 11 and 12" carries the MFN clause
    that triggers the 2023 Indian Supreme Court Nestlé-case
    framework.

    UNIQUE FEATURE: unlike India-France (Protocol Section 7 with
    explicit "1-9-1989" cutoff) and India-Netherlands (Protocol
    Section IV.2 with "after the signature of this convention"
    cutoff), the India-Sweden MFN clause has NO explicit date
    cutoff — it applies to "any Convention, Agreement or
    Protocol between India and a third State which is a member
    of the OECD" without temporal bound. The same Nestlé
    framework still applies: MFN auto-application requires the
    third State to have been an OECD member at the time of its
    India treaty signature AND requires explicit CBDT
    notification (India has not issued such a notification for
    Sweden). Vendoring the literal 10% rate per the Supreme
    Court position; the applicability_notes document the MFN
    context for auditor reference."""
    entry = lookup_dtaa_dividend("SE")
    assert entry is not None
    assert entry.iso_alpha2 == "SE"
    assert entry.country_name == "Sweden"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 24(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "sweden" in entry.applicability_notes.lower()
    assert "mfn" in entry.applicability_notes.lower()
    assert "nestl" in entry.applicability_notes.lower()
    assert "supreme court" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_singapore_row_matches_india_sg_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Singapore is DTAA, Article 10(2)(b) / 25(2), 15%. Verified
    verbatim against the taxsutra-hosted India-Singapore DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/SINGAPORE.pdf
    (Article 10 lines 249-290 and Article 25 lines 567-617 of
    pdftotext -layout extract).

    The 15% rate is the cap for "all other cases" under Article
    10(2)(b) — including portfolio Indian individuals. The 10%
    rate under Article 10(2)(a) applies ONLY to corporate
    beneficial owners owning >= 25% of the shares of the Singapore
    distributor; vendoring 10% would be wrong for the typical
    IBKR-investor case.

    Note India-Singapore's Article 25(2) is a single paragraph
    (not split into sub-paragraphs like India-US Article 25), so
    the Schedule FSI relief citation is "Article 25(2)" not "Article 25(2)(a)"."""
    entry = lookup_dtaa_dividend("SG")
    assert entry is not None
    assert entry.iso_alpha2 == "SG"
    assert entry.country_name == "Singapore"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)(b)"
    assert entry.relief_article == "Article 25(2)"
    assert entry.portfolio_dividend_rate_percent == Decimal("15")
    assert "portfolio" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_hungary_row_matches_india_hu_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Hungary is DTAA, Article 10(2) / 23(2), 10%. Verified
    verbatim against the taxsutra-hosted India-Hungary DTAA PDF
    at https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/HUNGARY%20DTAA.pdf
    (Article 10 lines 220-243 and Article 23 lines 446-464 of
    pdftotext -layout extract; Article 11 begins at line 244 and
    Article 24 at line 465; MFN clause in Protocol "With
    reference to Articles 10, 11 and 12" at lines 583-590).

    The 10% rate is a SINGLE-RATE cap (Article 10(2) — no
    substantial-holder split) — structurally similar to
    India-Germany / India-Ireland / India-Israel /
    India-Netherlands / India-Sweden / India-UAE / India-France
    flat 10% caps.

    Article 23 = Elimination of Double Taxation with paragraph 1
    = Hungary-resident relief with sub-paragraphs (a)(b);
    paragraph 2 = Indian-resident relief (single block — no
    sub-paragraph split unlike India-Israel 24(2)(a) or India-US
    25(2)(a)); paragraph 3 = exempt-income rate-progression
    applies symmetrically. The Schedule FSI relief citation is "Article 23(2)"
    — structurally similar to India-Germany Article 23(2) and
    India-Ireland Article 23(2).

    Hungary IS the FIFTH MFN-list country vendored (after NL
    #241 / FR #244 / SE #245 / ES #246). Per CBDT Circular
    3/2022 India has MFN clauses with 6 treaty partners:
    NL / FR / CH / SE / ES / HU. Only Switzerland remains
    pending of the MFN-list set.

    The Protocol "With reference to Articles 10, 11 and 12"
    carries the MFN clause that triggers the 2023 Indian Supreme
    Court Nestlé-case framework. Unlike Spain (whose MFN clause
    excludes dividends and covers only royalties/FTS), Hungary's
    MFN clause EXPLICITLY enumerates dividends.

    UNIQUE FEATURE: like India-Sweden the India-Hungary MFN
    clause has NO explicit date cutoff (unlike India-France
    "1-9-1989" and India-Netherlands "after the signature of
    this convention"). The same Nestlé framework still applies:
    MFN auto-application requires the third State to have been
    an OECD member at the time of its India treaty signature
    AND requires explicit CBDT notification (India has not
    issued such a notification for Hungary). Vendoring the
    literal 10% rate per the Supreme Court position.

    ALSO NOTABLE: the Protocol "With reference to Article 10"
    contains an India-specific distributed-profits-tax (DDT)
    pass-through provision applicable to Hungarian-resident
    shareholders receiving Indian-source dividends (NOT
    relevant to Indian-resident receiving Hungarian-source
    dividends — the Form 67 scenario)."""
    entry = lookup_dtaa_dividend("HU")
    assert entry is not None
    assert entry.iso_alpha2 == "HU"
    assert entry.country_name == "Hungary"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(2)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "hungary" in entry.applicability_notes.lower()
    assert "mfn" in entry.applicability_notes.lower()
    assert "nestl" in entry.applicability_notes.lower()
    assert "supreme court" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_ireland_row_matches_india_ie_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Ireland is DTAA, Article 10(2) / 23(2), 10%. Verified verbatim
    against the aseanbriefing-hosted India-Ireland DTAA PDF at
    https://www.aseanbriefing.com/userfiles/resources-pdfs/India/DTA/Asia_DTA_Ireland_India.pdf
    (Article 10 lines 324-353 and Article 23 lines 629-682 of
    pdftotext -layout extract).

    The 10% rate is a SINGLE-RATE cap — Article 10(2) does NOT have
    sub-paragraph split for substantial-holder vs portfolio (unlike
    India-US 15%/25% or India-Singapore 10%/15%). The vendored value
    is the literal Article 10(2) rate applying to every beneficial
    owner type. So col 9 cites "Article 10(2)" without sub-paragraph
    qualifier.

    Article 23 in India-Ireland is the "Elimination of Double
    Taxation" article (numbered earlier than India-US/SG Article 25
    because India-Ireland has fewer articles overall). Article 23(2)
    is the operative paragraph for Indian residents granting India
    the right to allow Irish tax as a credit."""
    entry = lookup_dtaa_dividend("IE")
    assert entry is not None
    assert entry.iso_alpha2 == "IE"
    assert entry.country_name == "Ireland"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(2)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "ireland" in entry.applicability_notes.lower()
    assert "aseanbriefing.com" in entry.source_citation


def test_hong_kong_row_matches_india_hk_dtaa_verbatim_authoritative_source() -> None:
    """Hong Kong is DTAA, Article 10(2) / 23(2)(a), 5% (#1146). The
    India-Hong Kong Agreement (signed 19 March 2018, in force 30
    November 2018) is notified under section 90(1) by CBDT
    Notification No. 89/2018, S.O. 6247(E), and applies in India from
    FY 2019-20. Verified verbatim against the CBDT record of the
    notification and Agreement and the Inland Revenue Department's
    synthesised text
    https://www.ird.gov.hk/eng/pdf/Synthesised_Text_HKSAR_India.pdf
    (Article 10 lines 681-735 and Article 23 lines 1206-1253 of
    pdftotext -layout extract).

    Article 10(2) caps source-state tax at "5 per cent of the gross
    amount of the dividends", one rate with no substantial-holder
    split, so col 9 cites "Article 10(2)". Article 23(2)(a) is
    India's credit clause. The row used to be no_dtaa."""
    entry = lookup_dtaa_dividend("HK")
    assert entry is not None
    assert entry.iso_alpha2 == "HK"
    assert entry.country_name == "Hong Kong"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("5")
    assert "Notification No. 89/2018" in entry.source_citation
    assert "S.O. 6247(E)" in entry.source_citation
    assert "ird.gov.hk" in entry.source_citation
    assert "India-China" not in entry.applicability_notes


def test_british_virgin_islands_row_no_dtaa_with_india_tiea_only() -> None:
    """BVI is no_dtaa. Vendored because VG-prefixed ISINs are common
    among USD-listed holdings: BVI-incorporated issuers list on US
    exchanges. Tri-state lookup must distinguish BVI from unvendored
    countries.

    BVI has only a TIEA under §90 (signed 2011-02-09) which
    does not provide treaty relief on doubly taxed income — only
    exchange of information. Structurally identical to Bermuda
    (BM) and Cayman Islands (KY) no_dtaa entries.

    In practice BVI-incorporated entities trade as US ADRs / NYSE
    / NASDAQ listings and the dividend WHT is the US WHT (paid
    by the US-listed entity), not a BVI WHT — so the §91 path
    is typically not exercised for BVI even when the underlying
    issuer is BVI-domiciled."""
    entry = lookup_dtaa_dividend("VG")
    assert entry is not None
    assert entry.iso_alpha2 == "VG"
    assert entry.country_name == "British Virgin Islands"
    assert entry.applicability is DtaaApplicability.NO_DTAA
    assert entry.withholding_article is None
    assert entry.relief_article is None
    assert entry.portfolio_dividend_rate_percent is None
    notes_lower = entry.applicability_notes.lower()
    assert "tiea" in notes_lower
    assert "bermuda" in notes_lower or "cayman" in notes_lower


def test_canada_row_matches_india_ca_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Canada is DTAA, Article 10(2)(b) / 23(3)(a), 25%. Verified
    verbatim against the taxsutra-hosted India-Canada DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/CANADA%20DTAA.pdf
    (Article 10 lines 312-344 and Article 23 lines 593-657 of
    pdftotext -layout extract).

    The 25% rate is the cap for "all other cases" under Article
    10(2)(b) — including portfolio Indian individuals. The 15%
    rate under Article 10(2)(a) applies ONLY to corporate
    beneficial owners controlling directly or indirectly at least
    10% of the voting power in the Canadian distributor;
    structurally identical to the India-US DTAA Article 10 split.

    Note India-Canada's Article 23 (Elimination of Double Taxation)
    is split into separate paragraphs for Canadian-resident relief
    (paragraph 2) and Indian-resident relief (paragraph 3) — so
    the Schedule FSI relief citation for Indian residents is "Article 23(3)(a)"
    (sub-paragraph (a) is the dividend / income credit clause vs
    (b) which is the capital credit clause). This differs from
    India-US Article 25 (Indian-resident clause in 25(2)) and
    India-Singapore Article 25 (single shared Indian-resident
    clause in 25(2))."""
    entry = lookup_dtaa_dividend("CA")
    assert entry is not None
    assert entry.iso_alpha2 == "CA"
    assert entry.country_name == "Canada"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)(b)"
    assert entry.relief_article == "Article 23(3)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("25")
    assert "portfolio" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_france_row_matches_india_fr_dtaa_pdf_verbatim_authoritative_source() -> None:
    """France is DTAA, Article 11(2) / 25(1)(a), 10%. Verified
    verbatim against the taxsutra-hosted India-France DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/FRANCE%20DTAA.pdf
    (Article 11 lines 240-281 and Article 25 lines 517-583 of
    pdftotext -layout extract; Article 12 begins at line 282 and
    Article 26 at line 584; Protocol Section 7 MFN clause at
    lines 736-744).

    NOTABLE ARTICLE NUMBERING: in this treaty Dividends is
    Article 11 (NOT Article 10 as in most Indian DTAAs); Article
    10 is Associated Enterprises. The withholding_article field
    is therefore "Article 11(2)" — different from US/IE/SG/CA/
    DE/JP/NL/IL/AE which all use Article 10(2) or 10(2)(b). The
    relief_article is "Article 25(1)(a)" — Article 25 has
    paragraph 1 = Indian-resident relief sub-paragraphs (a)(b);
    paragraph 2 = French-resident relief sub-paragraphs (a)-(f).
    Paragraph 1(a) is the Indian-resident credit clause for
    income (paragraph 1 also covers capital tax out of scope).

    France IS the second MFN-list country vendored (after NL).
    Per CBDT Circular 3/2022 India has MFN clauses with 6 treaty
    partners: Netherlands / France / Switzerland / Sweden /
    Spain / Hungary. The Protocol Section 7 of this treaty
    carries the MFN clause that triggered the 2023 Indian
    Supreme Court Nestlé-case ruling. Indian taxpayers
    historically argued the MFN clause triggers a 5% rate via
    later India-Slovenia / India-Lithuania / India-Colombia
    treaties. The Supreme Court resolved this in favour of the
    literal 10% rate per Article 11(2): MFN auto-application
    requires the third State to have been an OECD member at the
    time of its India treaty signature AND requires explicit
    CBDT notification (India has not issued such a notification
    for France). Vendoring the literal 10% rate per the Supreme
    Court position; the applicability_notes document the MFN
    context for auditor reference.

    Article 11(3)-(4) contain historic French "avoir fiscal"
    tax-credit-imputation provisions and prepayment (precompte)
    refund provisions — now largely defunct since the French
    2004 tax reform abolished the avoir fiscal system; for
    vendoring purposes only the Article 11(2) rate matters."""
    entry = lookup_dtaa_dividend("FR")
    assert entry is not None
    assert entry.iso_alpha2 == "FR"
    assert entry.country_name == "France"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 11(2)"
    assert entry.relief_article == "Article 25(1)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "france" in entry.applicability_notes.lower()
    assert "mfn" in entry.applicability_notes.lower()
    assert "nestl" in entry.applicability_notes.lower()
    assert "supreme court" in entry.applicability_notes.lower()
    assert "article 11" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_germany_row_matches_india_de_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Germany is DTAA, Article 10(2) / 23(2), 10%. Verified verbatim
    against the dsa-library-hosted India-Germany DTAA PDF at
    https://s3-ap-northeast-1.amazonaws.com/dsa-library/Resources/3685/Asia_DTA_Germany_India.pdf
    (Article 10 lines 317-351 and Article 23 lines 622-689 of
    pdftotext -layout extract).

    The 10% rate is a SINGLE-RATE cap (no substantial-holder split
    unlike India-US 15%/25% or India-Singapore 10%/15%);
    structurally similar to India-Ireland's Article 10(2) flat 10%
    cap. So col 9 cites "Article 10(2)" without sub-paragraph
    qualifier.

    Article 23 in India-Germany is "Relief from Double Taxation"
    (numbered earlier than India-US/SG Article 25 because the
    convention has fewer articles overall — but numbered the same as
    India-Ireland Article 23 and India-Canada Article 23). Article
    23(2) is the Indian-resident operative clause (a single
    paragraph; no sub-paragraphs like India-Canada Article 23(3)(a)).

    Verified separately: the India-Germany treaty has NO most-
    favoured-nation (MFN) clause (absent from the PDF body), so
    Germany is unaffected by the 2023 Indian Supreme Court
    Nestlé-case MFN ruling that addressed Netherlands / France /
    Switzerland."""
    entry = lookup_dtaa_dividend("DE")
    assert entry is not None
    assert entry.iso_alpha2 == "DE"
    assert entry.country_name == "Germany"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(2)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "germany" in entry.applicability_notes.lower()
    assert "dsa-library" in entry.source_citation


def test_japan_row_matches_india_jp_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Japan is DTAA, Article 10(2) / 23(2)(a), 15%. Verified verbatim
    against the aseanbriefing-hosted India-Japan DTAA PDF at
    https://www.aseanbriefing.com/userfiles/resources-pdfs/India/DTA/Asia_DTA_Japan_India.pdf
    (Article 10 lines 283-314 and Article 23 lines 563-602 of
    pdftotext -layout extract).

    The 15% rate is a SINGLE-RATE cap (Article 10(2) — no
    substantial-holder split). Higher than the more modern
    India-Germany / India-Ireland 10% single-rate treaties because
    this is one of India's older treaties (1989 / notified 1990).

    Article 23 in India-Japan is split into separate paragraphs for
    India-resident relief (paragraph 2 sub-paragraphs (a)/(b)) and
    Japan-resident relief (paragraph 3 sub-paragraphs (a)/(b)/(c));
    the Indian-resident credit clause is Article 23(2)(a).

    Verified separately: the India-Japan treaty has NO MFN clause
    (absent from the PDF body; also Japan is NOT on the CBDT
    Circular 3/2022 list of 6 MFN-clause treaties: Netherlands /
    France / Switzerland / Sweden / Spain / Hungary).

    Methodology note: web-search summaries returned THREE incorrect
    values for India-Japan during initial research (rate 10% vs
    actual 15%; notification S.O. 789(E) vs actual G.S.R. 101(E);
    date 1990-08-10 vs actual 1990-03-01). All corrected via direct
    PDF verification — reinforces the established convention to
    NEVER trust web-search treaty summaries for rates / article
    numbers / notification IDs."""
    entry = lookup_dtaa_dividend("JP")
    assert entry is not None
    assert entry.iso_alpha2 == "JP"
    assert entry.country_name == "Japan"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("15")
    assert "japan" in entry.applicability_notes.lower()
    assert "aseanbriefing.com" in entry.source_citation


def test_uae_row_matches_india_ae_dtaa_pdf_verbatim_authoritative_source() -> None:
    """UAE is DTAA, Article 10(2) / 25(2), 10%. Verified verbatim
    against the taxsutra-hosted India-UAE DTAA PDF at
    https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/UAE%20DTAA.pdf
    (Article 10 lines 232-253 and Article 25 lines 503-531 of
    pdftotext -layout extract; Article 11 begins at line 254 and
    Article 26 at line 532).

    The 10% rate is a SINGLE-RATE cap (Article 10(2) — no
    substantial-holder split) — similar in structure to
    India-Germany / India-Ireland / India-Israel flat 10% caps.

    The literal Article 10(2) text is uncharacteristically terse:
    verbatim it reads "the tax so charged shall not exceed 10 per
    cent." (with period; the "of the gross amount of the
    dividends" base is implicit). Compare Article 12(2) of the
    same treaty which does spell out "10 per cent of the gross
    amount of such royalties". This brevity is unique to the
    India-UAE treaty and does NOT indicate ambiguity.

    Article 25 is structured with paragraph 1 = general savings
    clause; paragraph 2 = Indian-resident credit clause (no
    sub-paragraphs splitting income vs capital tax); paragraph 3
    = UAE-resident credit clause; paragraph 4 = UNILATERAL
    tax-sparing provision explicitly scoped "For the purpose of
    paragraph (3)" — applicable ONLY to UAE-residents receiving
    Indian income (NOT relevant to Form 67 vendoring which is for
    Indian residents). The Schedule FSI relief citation is therefore
    "Article 25(2)" — different from India-Israel Article
    24(2)(a) which DOES split sub-paragraphs.

    Verified separately: the India-UAE treaty has NO MFN clause
    (absent from the PDF body; also UAE is NOT on the CBDT
    Circular 3/2022 list of 6 MFN-clause treaties: Netherlands /
    France / Switzerland / Sweden / Spain / Hungary).

    The 2007 amendment notification S.O. 2001(E) introduced
    tie-breaker residency rules in Article 4 plus a revised
    Article 28 (Exchange of Information) and added Article 29
    (Limitation of Benefits); it did NOT amend Article 10 rates.
    The current consolidated treaty carries the post-amendment
    article numbering and content."""
    entry = lookup_dtaa_dividend("AE")
    assert entry is not None
    assert entry.iso_alpha2 == "AE"
    assert entry.country_name == "United Arab Emirates"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 25(2)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "uae" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


def test_israel_row_matches_india_il_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Israel is DTAA, Article 10(2) / 24(2)(a), 10%. Verified
    verbatim against the aseanbriefing-hosted India-Israel DTAA PDF
    at https://www.aseanbriefing.com/userfiles/resources-pdfs/India/DTA/Asia_DTA_Israel_India.pdf
    (Article 10 lines 325-356 and Article 24 lines 674-708 of
    pdftotext -layout extract; Article 11 begins at line 357 and
    Article 25 at line 709).

    The 10% rate is a SINGLE-RATE cap (Article 10(2) — no
    substantial-holder split) — structurally similar to
    India-Germany / India-Ireland flat 10% caps and different from
    the India-US (15%/25%) or India-Singapore (10%/15%) two-tier
    structures.

    Article 24 is the Elimination-of-Double-Taxation clause —
    paragraph 1 covers Israel-resident relief; paragraph 2(a)
    covers Indian-resident credit for income tax (paragraph 2(b)
    covers capital tax which is not in Form 67 scope). The
    relief_article (cited by Schedule FSI; Form 67 col 9 cites the
    withholding article, Article 10(2)) is "Article 24(2)(a)" —
    different from India-Japan (which numbers its elimination
    article 23 not 24) or India-Canada (which puts the
    Indian-resident credit at 23(3)(a) not 24(2)(a)).

    Verified separately: the India-Israel treaty has NO MFN clause
    (absent from the PDF body; also Israel is NOT on the CBDT
    Circular 3/2022 list of 6 MFN-clause treaties: Netherlands /
    France / Switzerland / Sweden / Spain / Hungary).

    NOTABLE TAX-SPARING PROVISION: Article 24(3) is an override
    clause ("Notwithstanding the provisions of paragraphs 1 and 2")
    granting a deemed 15% credit on dividends regardless of the
    actual foreign tax paid — i.e. even if Israel only withholds
    the 10% Article 10(2) cap the Indian-resident may nonetheless
    claim a 15% deemed credit per Article 24(3). The vendored
    relief_article is the standard Article 24(2)(a) because the
    tool never claims a credit above the foreign tax withheld: the
    §90 credit is min(col 7, eligible foreign tax), and a
    [[form67.tax_payable_overrides]] entry supplies col 7 only, so
    it cannot unlock the 24(3) deemed credit. Claiming that
    tax-sparing benefit is manual work outside the tool. The
    applicability_notes document the 24(3) override fully so a
    future auditor reading the row can apply for the tax-sparing
    benefit independently."""
    entry = lookup_dtaa_dividend("IL")
    assert entry is not None
    assert entry.iso_alpha2 == "IL"
    assert entry.country_name == "Israel"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 24(2)(a)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "israel" in entry.applicability_notes.lower()
    assert "tax-sparing" in entry.applicability_notes.lower()
    assert "24(3)" in entry.applicability_notes
    assert "aseanbriefing.com" in entry.source_citation


def test_netherlands_row_matches_india_nl_dtaa_pdf_verbatim_authoritative_source() -> None:
    """Netherlands is DTAA, Article 10(2) / 23(4), 10%. Verified
    verbatim against the taxsutra-hosted India-Netherlands DTAA PDF
    at https://www.taxsutra.com/sites/taxsutra.com/files/dtaa/NETHERLANDS.pdf
    (Article 10 lines 300-330 and Article 23 lines 607-661 of
    pdftotext -layout extract).

    The 10% rate is the post-1999-protocol single-rate cap (Article
    10(2)). The original 1989 treaty had a 10%/15% sub-paragraph
    split — the 1999 Protocol (S.O. 693(E)) REPLACED Article 10(2)
    with a single 10% rate. The PDF marks the substitution with
    footnote brackets around the current Article 10(2) text.

    Netherlands IS on the CBDT Circular 3/2022 list of 6 MFN-clause
    treaties (NL / FR / CH / SE / ES / HU) — the Protocol Section
    IV.2 carries the famous MFN clause that triggered the 2023
    Indian Supreme Court Nestlé-case ruling. Per the Supreme Court
    + CBDT position, the MFN clause does NOT auto-apply the 5%
    rate from third-party-state OECD-membership treaties (Slovenia
    / Lithuania / Colombia); the literal 10% rate per Article 10(2)
    applies until India issues an explicit notification (which it
    has not). Vendoring the literal 10% rate per the Supreme Court
    position; the applicability_notes document the MFN context for
    auditor reference.

    Article 23 in India-Netherlands has paragraphs 1-3 covering
    Netherlands-resident relief and paragraph 4 covering Indian-
    resident relief — so the Schedule FSI relief citation for Indian residents
    is "Article 23(4)" (not 23(2)(a) like India-Japan or 23(3)(a)
    like India-Canada)."""
    entry = lookup_dtaa_dividend("NL")
    assert entry is not None
    assert entry.iso_alpha2 == "NL"
    assert entry.country_name == "Netherlands"
    assert entry.applicability is DtaaApplicability.DTAA
    assert entry.withholding_article == "Article 10(2)"
    assert entry.relief_article == "Article 23(4)"
    assert entry.portfolio_dividend_rate_percent == Decimal("10")
    assert "mfn" in entry.applicability_notes.lower()
    assert "nestl" in entry.applicability_notes.lower()
    assert "supreme court" in entry.applicability_notes.lower()
    assert "taxsutra.com" in entry.source_citation


# ============================================================================
# Tri-state lookup contract (the central design point of step 4/N)
# ============================================================================


def test_tristate_dtaa_country_returns_dtaa_entry() -> None:
    """First state: vendored DTAA country returns an entry whose
    applicability == DTAA. The §90 / §90A pathway applies."""
    entry = lookup_dtaa_dividend("US")
    assert entry is not None
    assert entry.applicability is DtaaApplicability.DTAA


def test_tristate_no_dtaa_country_returns_no_dtaa_entry() -> None:
    """Second state: vendored NO_DTAA country returns an entry
    whose applicability == NO_DTAA. The §91 unilateral pathway
    applies."""
    entry = lookup_dtaa_dividend("KY")
    assert entry is not None
    assert entry.applicability is DtaaApplicability.NO_DTAA


def test_tristate_unknown_country_returns_none() -> None:
    """Third state: unvendored country returns None — UNKNOWN.

    The compute layer MUST NOT silently default to §91 for these
    countries; doing so would mis-attribute relief for any DTAA
    country we have not yet vendored. This is the blocking design
    finding from the step-4/N planning rubber-duck."""
    # ZZ is the user-assigned ISO code (never assigned to a real
    # country); guaranteed to be unvendored.
    assert lookup_dtaa_dividend("ZZ") is None


def test_tristate_unknown_vs_no_dtaa_are_distinct() -> None:
    """Sanity: lookup returns None for unknown, not a NO_DTAA
    sentinel. The two states have different downstream behaviour
    (NO_DTAA → §91 pathway; None → leave cols 9-13 None)."""
    unknown = lookup_dtaa_dividend("ZZ")
    no_dtaa = lookup_dtaa_dividend("KY")
    assert unknown is None
    assert no_dtaa is not None
    assert no_dtaa.applicability is DtaaApplicability.NO_DTAA


# ============================================================================
# Shape contract: DTAA rows have DTAA fields; no_dtaa rows don't
# ============================================================================


def test_dtaa_rows_have_all_dtaa_fields_populated() -> None:
    """The model invariant on Form67Entry enforces that
    section_90_credit_inr requires both section_90_dtaa_article
    AND section_90_dtaa_rate_percent. The compute layer can only
    populate those from a DTAA row if both this loader-side fields
    are present, so the loader enforces presence at materialisation
    time."""
    table = load_dtaa_dividend_rates()
    for entry in table.values():
        if entry.applicability is DtaaApplicability.DTAA:
            assert entry.withholding_article is not None
            assert entry.withholding_article != ""
            assert entry.relief_article is not None
            assert entry.relief_article != ""
            assert entry.portfolio_dividend_rate_percent is not None
            assert entry.portfolio_dividend_rate_percent >= 0


def test_no_dtaa_rows_have_dtaa_fields_none() -> None:
    """Form67Entry's mutex on §91 credit ↔ DTAA fields requires
    that no_dtaa rows do NOT carry DTAA fields. The §91 unilateral
    pathway has no treaty article to cite."""
    table = load_dtaa_dividend_rates()
    for entry in table.values():
        if entry.applicability is DtaaApplicability.NO_DTAA:
            assert entry.withholding_article is None
            assert entry.relief_article is None
            assert entry.portfolio_dividend_rate_percent is None


# ============================================================================
# Vendored coverage — minimum country set
# ============================================================================


def test_minimum_vendored_country_set_present() -> None:
    """Regression guard: the v1 vendored set MUST include at least
    the US (the dominant IBKR-investor case via the India-US DTAA)
    and at least one NO_DTAA country (so the tri-state contract is
    exercised by real data not just synthetic test fixtures)."""
    table = load_dtaa_dividend_rates()
    assert "US" in table
    no_dtaa_iso_set = {
        iso for iso, e in table.items() if e.applicability is DtaaApplicability.NO_DTAA
    }
    assert no_dtaa_iso_set, "vendored CSV must include >= 1 no_dtaa row"


def test_vendored_iso_codes_are_well_formed() -> None:
    """Every vendored ISO code must be 2 uppercase ASCII letters.
    The loader enforces this at materialisation; this is the
    smoke-check on real data."""
    table = load_dtaa_dividend_rates()
    for iso in table:
        assert len(iso) == 2
        assert iso.isupper()
        assert iso.isalpha()
        assert iso.isascii()


def test_vendored_notes_and_citations_are_non_empty() -> None:
    """Every row must document its provenance (notes) AND cite a
    primary source (citation). Both are surfaced into downstream
    review CSVs for auditor reconciliation."""
    table = load_dtaa_dividend_rates()
    for entry in table.values():
        assert entry.applicability_notes.strip() != ""
        assert entry.source_citation.strip() != ""


# ============================================================================
# Caching
# ============================================================================


def test_load_dtaa_dividend_rates_is_cached() -> None:
    """Repeated calls return the same dict object (functools.cache).
    Matches the country_codes.py convention."""
    a = load_dtaa_dividend_rates()
    b = load_dtaa_dividend_rates()
    assert a is b


# ============================================================================
# Header-drift rejection
# ============================================================================


def _install_fake_csv(monkeypatch: pytest.MonkeyPatch, csv_text: str) -> None:
    """Helper to replace the vendored CSV at the importlib.resources
    layer for a single test. Mirrors the country_codes.py test
    pattern."""

    class _FakeFile:
        def read_text(self, encoding: str) -> str:
            return csv_text

    class _FakeFiles:
        def joinpath(self, _name: str) -> _FakeFile:
            return _FakeFile()

    monkeypatch.setattr(resources, "files", lambda _pkg: _FakeFiles())


def test_load_rejects_unexpected_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: schema drift without a loader update must fail
    loudly with a pointer to fix both sides together."""
    load_dtaa_dividend_rates.cache_clear()
    bad_csv = (
        "wrong,header,format,here,with,fewer,columns,than_expected\n"
        "US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,citation\n"
    )
    _install_fake_csv(monkeypatch, bad_csv)
    try:
        with pytest.raises(InvariantError, match="unexpected header"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_skips_blank_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank lines mid-CSV must not blow up the loader."""
    load_dtaa_dividend_rates.cache_clear()
    csv_with_blanks = (
        "iso_alpha2,country_name,applicability,withholding_article,"
        "relief_article,portfolio_dividend_rate_percent,"
        "applicability_notes,source_citation\n"
        "US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,citation\n"
        "\n"
        "KY,Cayman Islands,no_dtaa,,,,No DTAA notes,No DTAA citation\n"
    )
    _install_fake_csv(monkeypatch, csv_with_blanks)
    try:
        table = load_dtaa_dividend_rates()
        assert set(table) == {"US", "KY"}
    finally:
        load_dtaa_dividend_rates.cache_clear()


# ============================================================================
# Per-row invariants (load-time rejection of bad data)
# ============================================================================


_RATE_SYNTAX = "portfolio_dividend_rate_percent must be plain ASCII digits"


def _csv_for(rows: list[str]) -> str:
    header = (
        "iso_alpha2,country_name,applicability,withholding_article,"
        "relief_article,portfolio_dividend_rate_percent,"
        "applicability_notes,source_citation\n"
    )
    return header + "".join(row + "\n" for row in rows)


def test_load_rejects_invalid_iso_alpha2(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["us,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="iso_alpha2"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_unknown_applicability_value(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,maybe,Article 10(2)(b),Article 25(2)(a),25,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(InvariantError, match="applicability must be one of"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_missing_withholding_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,,Article 25(2)(a),25,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="withholding_article"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_missing_relief_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),,25,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="relief_article"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_missing_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="portfolio_dividend_rate_percent"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_negative_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),-5,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(InvariantError, match=_RATE_SYNTAX):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_rate_above_100(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),101,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="100"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_dtaa_row_non_numeric_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(
        ["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),fifteen,notes,citation"]
    )
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(InvariantError, match=_RATE_SYNTAX):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_no_dtaa_row_with_withholding_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["KY,Cayman Islands,no_dtaa,Article 10,,,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="withholding_article must be EMPTY"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_no_dtaa_row_with_relief_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["KY,Cayman Islands,no_dtaa,,Article 25,,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="relief_article must be EMPTY"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_no_dtaa_row_with_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["KY,Cayman Islands,no_dtaa,,,15,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="must be EMPTY for a no_dtaa row"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_empty_country_name(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="country_name"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_empty_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,,citation"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="applicability_notes"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_empty_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(["US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,"])
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="source_citation"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_duplicate_iso(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    bad = _csv_for(
        [
            "US,United States,dtaa,Article 10(2)(b),Article 25(2)(a),25,notes,citation",
            "US,United States Two,dtaa,Article X,Article Y,15,notes2,citation2",
        ]
    )
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="duplicate iso_alpha2"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


def test_load_rejects_row_with_wrong_column_count(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dtaa_dividend_rates.cache_clear()
    # Header is the EXPECTED 8 cols (matches the loader expectation),
    # but the body row has only 5 — the loader must catch this BEFORE
    # the per-row destructuring assignment would have crashed with a
    # bare "not enough values to unpack" ValueError.
    bad = (
        "iso_alpha2,country_name,applicability,withholding_article,"
        "relief_article,portfolio_dividend_rate_percent,"
        "applicability_notes,source_citation\n"
        "US,United States,dtaa,Article 10(2)(b),Article 25(2)(a)\n"
    )
    _install_fake_csv(monkeypatch, bad)
    try:
        with pytest.raises(IbkrFaError, match="exactly 8 columns"):
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()


# ============================================================================
# Strict parsing and whole-cell checks (#945)
# ============================================================================


def _load_refusal(monkeypatch: pytest.MonkeyPatch, csv_text: str) -> InvariantError:
    load_dtaa_dividend_rates.cache_clear()
    _install_fake_csv(monkeypatch, csv_text)
    try:
        with pytest.raises(InvariantError) as excinfo:
            load_dtaa_dividend_rates()
    finally:
        load_dtaa_dividend_rates.cache_clear()
    return excinfo.value


def _us_row(*, iso: str = "US", rate: str = "25", notes: str = "notes", citation: str = "c") -> str:
    return f"{iso},United States,dtaa,Article 10(2)(b),Article 25(2)(a),{rate},{notes},{citation}"


@pytest.mark.parametrize(
    ("rate", "what"),
    [
        ('"2\n5"', "portfolio_dividend_rate_percent must be printable"),
        ('"2"5', "malformed CSV"),
        ("NaN", _RATE_SYNTAX),
        (" 25", "portfolio_dividend_rate_percent must be printable with no leading"),
        ("2_5", _RATE_SYNTAX),
        ("+25", _RATE_SYNTAX),
        ("\u0662\u0665", _RATE_SYNTAX),
    ],
)
def test_load_refuses_a_rate_that_is_not_plain_ascii_digits(
    monkeypatch: pytest.MonkeyPatch, rate: str, what: str
) -> None:
    """All but ``NaN`` became a valid 25 before: the line split joined
    the two halves, the default dialect dropped the stray quote, or
    ``Decimal`` took the spelling. ``NaN`` raised a bare
    ``InvalidOperation`` at the bound check."""
    err = _load_refusal(monkeypatch, _csv_for([_us_row(rate=rate)]))
    assert what in err.what
    assert "dtaa_dividend_rates.csv" in err.what


_MALFORMED_CSV = (
    "InvariantError: dtaa_dividend_rates.csv: malformed CSV (text after a closing quote, "
    "a quote left open or a cell over the csv field size limit) (context keys: line=3)"
)


def test_load_names_the_line_of_malformed_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message is fixed text and the line, with no cell (#1046)."""
    bad_row = _us_row(iso="GB", notes='"Synthetic"Holder Two')
    err = _load_refusal(monkeypatch, _csv_for([_us_row(), bad_row]))
    assert str(err) == _MALFORMED_CSV
    assert "Synthetic" not in str(err)
    assert err.context == (("line", 3),)


@pytest.mark.parametrize("iso", ['"U\nS"', '"US\n"'])
def test_load_refuses_an_iso_cell_with_a_line_break(
    monkeypatch: pytest.MonkeyPatch, iso: str
) -> None:
    err = _load_refusal(monkeypatch, _csv_for([_us_row(iso=iso)]))
    assert "iso_alpha2 must be printable" in err.what
    assert err.context == (("iso_alpha2", None),)


@pytest.mark.parametrize(
    ("row", "what"),
    [
        (_us_row(notes="tab\there"), "applicability_notes must be printable"),
        (_us_row(citation="cited "), "source_citation must be printable with no leading"),
        (",,,,,,,", "iso_alpha2 must be exactly 2"),
    ],
)
def test_load_refuses_a_cell_that_is_not_plain(
    monkeypatch: pytest.MonkeyPatch, row: str, what: str
) -> None:
    """A tab, a trailing space or a row of empty cells refuses; the old
    loader skipped the empty row and only stripped cells to test them."""
    err = _load_refusal(monkeypatch, _csv_for([row]))
    assert what in err.what


def test_load_refuses_a_file_ending_in_an_unclosed_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = _load_refusal(monkeypatch, _csv_for([_us_row()]) + '"GB,United Kingdom')
    assert str(err) == _MALFORMED_CSV


# ============================================================================
# Loader-side load_dtaa_dividend_rates() integrity smoke check on real data
# ============================================================================


def test_vendored_csv_passes_load_time_invariants() -> None:
    """Smoke check that the bundled dtaa_dividend_rates.csv satisfies
    all per-row invariants (well-formed ISO, valid applicability,
    DTAA-fields-populated-iff-DTAA-row, etc.). The invariants fire
    inside load_dtaa_dividend_rates(); this test just confirms the
    happy path on real data."""
    table = load_dtaa_dividend_rates()
    assert len(table) > 0
    for iso, entry in table.items():
        assert iso == entry.iso_alpha2
        assert isinstance(entry.applicability, DtaaApplicability)
        if entry.applicability is DtaaApplicability.DTAA:
            assert entry.withholding_article is not None
            assert entry.relief_article is not None
            assert isinstance(entry.portfolio_dividend_rate_percent, Decimal)
            assert entry.portfolio_dividend_rate_percent >= 0
            assert entry.portfolio_dividend_rate_percent <= Decimal("100")
        else:
            assert entry.withholding_article is None
            assert entry.relief_article is None
            assert entry.portfolio_dividend_rate_percent is None


# ============================================================================
# __all__ surface
# ============================================================================


def test_module_all_surface_is_complete() -> None:
    """Every public name we want callers to import should be in
    __all__. Catches accidental omission when adding new helpers."""
    expected = {
        "DtaaApplicability",
        "DtaaDividendEntry",
        "load_dtaa_dividend_rates",
        "lookup_dtaa_dividend",
    }
    assert set(dtd.__all__) == expected
