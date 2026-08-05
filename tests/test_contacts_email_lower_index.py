"""``contacts.email`` is looked up lower-cased, so it is indexed lower-cased.

``ix_contacts_email`` (migration 001) indexes the raw column, and nothing in the
codebase filters the raw column. The reason is stated in
``send_limiter.can_send_to_contact``: the column has no unique constraint and is
not normalised on write, so one person holds several rows whose casing differs
by source. Every lookup that is about a *recipient* rather than a *row* compares
``lower(email)`` — which a b-tree on ``email`` cannot serve.

The failure mode is not "a report is slow". The scans sit on the send path (the
compliance gate, twice more for the limiter's cap and gap checks) and on the
inbound beats (reply attribution, bounce and complaint handling), against the
table the scraper grows fastest. It degrades in step with the product working.

Two things are pinned:

* the index exists and is on the **expression**, not on the bare column — the
  mutation that matters, because ``Index("ix_contacts_email_lower", "email")``
  looks right and indexes nothing anyone queries;
* the call sites still use ``lower()``, so the index and the queries cannot
  drift apart in the other direction either.
"""

import pathlib
import re

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.database import Base

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

INDEX_NAME = "ix_contacts_email_lower"


def _index():
    table = Base.metadata.tables["contacts"]
    for index in table.indexes:
        if index.name == INDEX_NAME:
            return index
    raise AssertionError(
        f"{INDEX_NAME} is not declared on the Contact model. "
        "Every recipient lookup filters lower(email) and cannot use "
        "ix_contacts_email."
    )


def test_the_index_is_declared_on_the_model():
    assert _index() is not None


def test_the_index_is_on_the_expression_not_the_bare_column():
    """The mutation that reads as correct and indexes the wrong thing."""
    expressions = [str(expr) for expr in _index().expressions]
    assert len(expressions) == 1
    assert "lower" in expressions[0].lower(), expressions
    assert expressions[0].strip().lower() != "email", (
        "indexing the raw column duplicates ix_contacts_email and serves none "
        "of the queries that motivated this index"
    )


def test_the_index_reaches_a_created_schema():
    """Tests build their schema from ``Base.metadata``; so must this index.

    An index absent from the test schema is one no future "does this query use
    an index?" check could ever observe.
    """
    from sqlalchemy.schema import CreateIndex
    from sqlalchemy.dialects import postgresql

    ddl = str(CreateIndex(_index()).compile(dialect=postgresql.dialect()))
    assert INDEX_NAME in ddl
    assert "lower" in ddl.lower()


def test_the_raw_column_index_is_kept():
    """001's index is not dropped in passing — a range query would still use it."""
    names = {i.name for i in Base.metadata.tables["contacts"].indexes}
    assert "ix_contacts_email" in names


def test_the_call_sites_still_compare_lower():
    """The other direction of drift: queries reverting to a raw comparison.

    If these stopped using ``lower()`` the index would be dead weight *and* the
    lookups would go back to being about a row rather than a person — which is
    the bug ``can_send_to_contact`` documents at length.
    """
    hits = 0
    for path in APP.rglob("*.py"):
        if re.search(r"lower\(\s*Contact\.email\s*\)", path.read_text()):
            hits += 1
    assert hits >= 8, (
        f"only {hits} modules compare lower(Contact.email); if the codebase "
        "moved to a normalised column, this index and its migration should go "
        "with it"
    )
