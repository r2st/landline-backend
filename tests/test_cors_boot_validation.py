"""The CORS list is resolved at boot, because neither way it goes wrong is visible.

``allow_credentials=True`` is set on the middleware (the frontend authenticates
with a header), and that changes what ``"*"`` means: Starlette stops answering
``Access-Control-Allow-Origin: *`` and starts echoing the request's own
``Origin`` back, individually approving every site that asks. A wildcard reads
like the loosest setting available and is in fact looser than that.

The other half is duller and more likely: an entry that is not a bare
``scheme://host[:port]`` matches no browser ``Origin`` header at all, so the
frontend fails every request and nothing server-side logs a thing.

The two faults get different treatments, and the asymmetry is the point:

- the wildcard **grants** more than intended, so in production it is fatal;
- a malformed entry **grants nothing**, so it is dropped and logged at ERROR.
  Refusing to boot over a trailing slash would take the whole API — including
  every endpoint no browser calls — down on deploy, to fix an entry that was
  already inert.
"""

import pytest

from app.main import is_valid_origin, resolve_cors_origins


VALID = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://landline.apprendtechnologies.com",
    "https://app.example.co.uk:8443",
]

MALFORMED = [
    "apprend.tech",                       # no scheme — matches nothing
    "https://app.apprend.tech/",          # trailing path — Origin never has one
    "https://app.apprend.tech/callback",  # a URL, not an origin
    "*.apprend.tech",                     # CORS does not do wildcards per-host
    "https://*.apprend.tech",             # ...nor with a scheme attached
    "ftp://apprend.tech",                 # not a browser origin scheme
    "https://app.apprend.tech?x=1",
    "https://app.apprend.tech#frag",
    "",                                   # an empty entry from a trailing comma
]


# --------------------------------------------------------------------------
# is_valid_origin — the shape check on its own
# --------------------------------------------------------------------------

@pytest.mark.parametrize("origin", VALID)
def test_a_bare_origin_is_valid(origin):
    assert is_valid_origin(origin) is True


@pytest.mark.parametrize("origin", MALFORMED)
def test_anything_that_is_not_a_bare_origin_is_not(origin):
    assert is_valid_origin(origin) is False


# --------------------------------------------------------------------------
# The list that actually gets installed
# --------------------------------------------------------------------------

def test_a_normal_list_passes_through_unchanged():
    assert resolve_cors_origins(VALID, is_prod=True) == VALID


def test_an_empty_list_is_fine():
    """No configured origins is restrictive, not broken."""
    assert resolve_cors_origins([], is_prod=True) == []


def test_wildcard_is_refused_in_production():
    with pytest.raises(RuntimeError, match="Origin"):
        resolve_cors_origins(["*"], is_prod=True)


def test_wildcard_is_refused_even_alongside_real_origins():
    """One '*' is enough — it is matched before the specific entries."""
    with pytest.raises(RuntimeError):
        resolve_cors_origins(["https://landline.apprendtechnologies.com", "*"], is_prod=True)


def test_wildcard_only_warns_outside_production(caplog):
    """A dev box legitimately wants it; it should still say so."""
    with caplog.at_level("WARNING"):
        resolved = resolve_cors_origins(["*"], is_prod=False)
    assert resolved == ["*"], "the dev wildcard must survive the warning it triggers"
    assert any("CORS_ORIGINS" in r.message for r in caplog.records)


@pytest.mark.parametrize("origin", MALFORMED)
def test_a_malformed_origin_is_dropped_not_fatal(origin, caplog):
    with caplog.at_level("ERROR"):
        assert resolve_cors_origins([origin], is_prod=True) == []
    assert any("Ignoring CORS origin" in r.message for r in caplog.records), (
        "a dropped origin has to be loud — the frontend's only other symptom is "
        "a browser-console error nobody server-side ever sees"
    )


def test_a_malformed_entry_does_not_take_the_valid_ones_with_it():
    """The reason this drops rather than raises: the rest of the list still works."""
    resolved = resolve_cors_origins(
        ["https://app.apprend.tech/", "https://app.apprend.tech"], is_prod=True
    )
    assert resolved == ["https://app.apprend.tech"]


def test_the_shipped_default_is_valid():
    """The default in app.config must itself survive the check that runs at boot."""
    from app.config import Settings

    defaults = Settings().cors_origins
    assert resolve_cors_origins(defaults, is_prod=False) == defaults
