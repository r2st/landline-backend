"""A country name is not its first two letters.

Sources disagree on how to spell a country. Google Places returns ISO alpha-2
(``parse_place`` asks for the short form). Apollo returns a full name. A
business card returns whatever was printed on it. Both normalizers in this
codebase resolved that by slicing ``[:2]``, which is right only for input that
was already a code.

On a full name the slice does not fail — it produces a *different, plausible*
code, and that is what made it survive:

    "Germany"        -> "GE"   (GE is Georgia)
    "Singapore"      -> "SI"   (SI is Slovenia)
    "United Kingdom" -> "UN"
    "United States"  -> "UN"

India, Canada, France and Australia came out right by coincidence — each begins
with its own code — so the paths that mattered most in testing were exactly the
ones that worked. The two callers then diverged:

* ``ingestion.normalize_country`` refuses an unsupported country rather than
  relabelling it, so the record was **dropped**, with an error quoting a code
  ("country 'UN' is not supported") that no source had ever sent. Every reader
  of that message goes looking at Apollo.
* the card scanner's ``_normalize_country`` defaulted anything it didn't
  recognise to ``"US"``, so a German company was **filed as American** — the
  mislabelling ingestion exists to prevent. The consent gate downstream then
  applied CAN-SPAM to it.
"""

import pytest

from app.constants import ALLOWED_COUNTRIES, resolve_country
from app.routers.contacts import _normalize_country
from app.services.ingestion import IngestionError, IngestionService


# --- The resolver ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("United States", "US"),
        ("United States of America", "US"),
        ("USA", "US"),
        ("Canada", "CA"),
        ("United Kingdom", "GB"),
        ("Great Britain", "GB"),
        ("UK", "GB"),
        ("England", "GB"),
        ("Germany", "DE"),
        ("Deutschland", "DE"),
        ("Singapore", "SG"),
        ("United Arab Emirates", "AE"),
        ("UAE", "AE"),
        ("India", "IN"),
        ("France", "FR"),
        ("Australia", "AU"),
    ],
)
def test_full_names_resolve_to_their_own_code(raw, expected):
    assert resolve_country(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("Germany", "DE"), ("Singapore", "SG"), ("United Kingdom", "GB"),
     ("United States", "US")],
)
def test_the_truncation_answers_are_never_returned(raw, expected):
    """The specific regression: the slice's plausible-wrong codes.

    ``GE`` and ``SI`` are real countries, which is why this was never loud."""
    assert resolve_country(raw) != raw.strip().upper()[:2]
    assert resolve_country(raw) == expected


def test_codes_pass_through_and_tolerate_casing():
    for code in ALLOWED_COUNTRIES:
        assert resolve_country(code) == code
        assert resolve_country(code.lower()) == code
        assert resolve_country(f"  {code}  ") == code


def test_an_unknown_country_has_no_answer_rather_than_a_guess():
    """None, not a truncation. A caller must not be able to mistake a guess for
    a reading — which two callers did, in opposite directions."""
    for raw in ("Narnia", "Zzz", "", "   ", None, "Republic of Nowhere"):
        assert resolve_country(raw) is None


def test_the_resolver_never_invents_a_country_the_schema_rejects():
    from app.constants import COUNTRY_ALIASES

    bad = {v for v in COUNTRY_ALIASES.values() if v not in ALLOWED_COUNTRIES}
    assert not bad, f"{bad} would fail the companies CHECK constraint"


# --- Ingestion: a dropped record ------------------------------------------

@pytest.fixture
def ingestion(monkeypatch):
    svc = IngestionService()
    return svc


def _with_supported(svc, countries):
    class _S:
        supported_countries = countries
    svc.settings = _S()
    return svc


@pytest.mark.parametrize(
    "raw,expected",
    [("United States", "US"), ("Germany", "DE"), ("United Kingdom", "GB"),
     ("Singapore", "SG"), ("United Arab Emirates", "AE")],
)
def test_ingestion_accepts_a_full_country_name(ingestion, raw, expected):
    """The regression. Apollo reports full names; these were all refused."""
    svc = _with_supported(ingestion, list(ALLOWED_COUNTRIES))
    assert svc.normalize_country(raw) == expected


def test_ingestion_still_refuses_a_country_outside_policy(ingestion):
    """Policy is narrower than schema, and stays enforced."""
    svc = _with_supported(ingestion, ["US", "CA"])
    with pytest.raises(IngestionError) as exc:
        svc.normalize_country("Germany")
    assert "DE" in str(exc.value) or "Germany" in str(exc.value)


def test_ingestion_refusal_quotes_what_the_source_sent(ingestion):
    """Not a code we manufactured. 'country UN is not supported' pointed every
    reader at the provider's data for a slice we performed ourselves."""
    svc = _with_supported(ingestion, list(ALLOWED_COUNTRIES))
    with pytest.raises(IngestionError) as exc:
        svc.normalize_country("Narnia")
    msg = str(exc.value)
    assert "Narnia" in msg
    assert "'NA'" not in msg


def test_ingestion_still_relabels_nothing(ingestion):
    """The property the module is built on: refuse, never coerce."""
    svc = _with_supported(ingestion, ["US"])
    with pytest.raises(IngestionError):
        svc.normalize_country("India")


def test_ingestion_passes_blank_through_as_none(ingestion):
    svc = _with_supported(ingestion, list(ALLOWED_COUNTRIES))
    assert svc.normalize_country(None) is None
    assert svc.normalize_country("   ") is None


# --- Card scanner: a mislabelled record ------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("United Kingdom", "GB"), ("Germany", "DE"), ("Singapore", "SG"),
     ("India", "IN"), ("Australia", "AU"), ("France", "FR"),
     ("UAE", "AE"), ("Canada", "CA")],
)
def test_scanned_cards_keep_their_country(raw, expected):
    """Every one of these used to become ``US`` — a supported market filed as
    the wrong one, which the consent gate then reads to pick a regime."""
    assert _normalize_country(raw) == expected


def test_scanner_keeps_us_as_the_fallback_for_an_unreadable_country():
    """Unchanged, and deliberately: a card scan mostly yields nothing here, and
    the endpoint has always defaulted. What changed is that a country it *can*
    read is no longer swept into the same bucket."""
    assert _normalize_country(None) == "US"
    assert _normalize_country("") == "US"
    assert _normalize_country("Narnia") == "US"


def test_scanner_only_returns_countries_a_company_row_can_hold():
    for raw in ("United Kingdom", "Germany", "Narnia", None, "SG"):
        assert _normalize_country(raw) in ALLOWED_COUNTRIES
