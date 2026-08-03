"""A compliance upload is not allowed to be a per-row round trip.

The upload cap is 5 MB, which is about 150k addresses. The import ran
``add_entry`` per row — a ``SELECT`` plus a ``flush`` each — so a full-size file
was on the order of 300k sequential statements inside one request, holding a
worker and a connection for minutes.

The failure that produces is worse than "slow". Nothing commits until the end,
so when the proxy times the request out the transaction rolls back and **none**
of the list landed, with the operator told nothing either way. A do-not-contact
upload that silently doesn't apply is the one outcome this endpoint must not
have: the addresses on it are people who asked us to stop, and the next sequence
run will mail all of them.

So the count of statements is the property under test, not the wall clock —
a timing assertion would be flaky and would not say what went wrong.
"""

import io

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.suppression import SuppressionEntry
from app.services import suppression


class _StatementCounter:
    """Counts SQL statements issued on a session's connection."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    @property
    def selects(self) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith("SELECT"))

    @property
    def inserts(self) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith("INSERT"))


async def _count_statements(db: AsyncSession, coro_factory):
    counter = _StatementCounter()
    conn = await db.connection()
    engine = conn.sync_engine
    event.listen(engine, "before_cursor_execute", counter)
    try:
        result = await coro_factory()
    finally:
        event.remove(engine, "before_cursor_execute", counter)
    return result, counter


def _csv(n: int, *, start: int = 0) -> str:
    return "email\n" + "".join(f"user{i}@example.com\n" for i in range(start, start + n))


@pytest.mark.asyncio
async def test_a_large_import_is_batched_not_per_row(db_session: AsyncSession):
    rows = suppression._IMPORT_BATCH * 3
    result, counter = await _count_statements(
        db_session, lambda: suppression.import_csv(db_session, _csv(rows))
    )

    assert result["added"] == rows
    assert result["total"] == rows

    # The listener has to have seen the import, or every bound below is vacuous.
    assert counter.selects >= 1 and counter.inserts >= 1, (
        "the statement counter recorded nothing — it is not attached"
    )

    # Three batches → three existence reads, not one per address.
    assert counter.selects <= 5, (
        f"{counter.selects} SELECTs for {rows} addresses — the import is still "
        "reading row-at-a-time"
    )
    # The ORM coalesces same-table inserts, so a batch is a small number of
    # statements rather than one per row. The bound is generous on purpose: what
    # must not hold is a count that scales with the row count.
    assert counter.inserts <= rows // 10, (
        f"{counter.inserts} INSERTs for {rows} addresses — not batched"
    )


@pytest.mark.asyncio
async def test_every_address_actually_lands(db_session: AsyncSession):
    """Batching must not trade correctness for statement count."""
    rows = suppression._IMPORT_BATCH + 7
    result = await suppression.import_csv(db_session, _csv(rows))
    assert result["added"] == rows

    total = (
        await db_session.execute(select(func.count(SuppressionEntry.id)))
    ).scalar()
    assert total == rows
    assert await suppression.is_suppressed(db_session, "user0@example.com") is True
    assert await suppression.is_suppressed(db_session, f"user{rows - 1}@example.com") is True


@pytest.mark.asyncio
async def test_addresses_already_on_the_list_count_as_duplicates(db_session: AsyncSession):
    """Across the batch boundary, so the per-batch read is what's exercised."""
    first = suppression._IMPORT_BATCH + 10
    await suppression.import_csv(db_session, _csv(first))

    # Re-upload the same addresses plus 20 new ones.
    result = await suppression.import_csv(db_session, _csv(first + 20))
    assert result["duplicates"] == first
    assert result["added"] == 20
    assert result["total"] == first + 20

    total = (
        await db_session.execute(select(func.count(SuppressionEntry.id)))
    ).scalar()
    assert total == first + 20


@pytest.mark.asyncio
async def test_a_repeat_within_the_file_is_counted_once(db_session: AsyncSession):
    content = "email\na@b.com\nA@B.com\n a@b.com \nc@d.com\n"
    result = await suppression.import_csv(db_session, content)
    assert result == {"added": 2, "duplicates": 2, "invalid": 0, "total": 4}


@pytest.mark.asyncio
async def test_invalid_rows_are_counted_and_never_inserted(db_session: AsyncSession):
    content = "email\ngood@example.com\nnot-an-email\n\nalso bad\n"
    result = await suppression.import_csv(db_session, content)
    assert result["added"] == 1
    assert result["invalid"] == 2

    total = (
        await db_session.execute(select(func.count(SuppressionEntry.id)))
    ).scalar()
    assert total == 1


@pytest.mark.asyncio
async def test_per_row_reasons_survive_batching(db_session: AsyncSession):
    """The reason column decides whether an operator may lift the entry later."""
    content = (
        "email,reason\n"
        "a@b.com,complaint\n"
        "c@d.com,bounce\n"
        "e@f.com,\n"  # falls back to the caller's default
        "g@h.com,nonsense\n"  # unknown value → dropped by the parser, default applies
    )
    result = await suppression.import_csv(db_session, content, default_reason="unsubscribe")
    assert result["added"] == 4

    rows = (await db_session.execute(select(SuppressionEntry))).scalars().all()
    by_email = {r.email: r for r in rows}
    assert by_email["a@b.com"].reason == "complaint"
    assert by_email["c@d.com"].reason == "bounce"
    assert by_email["e@f.com"].reason == "unsubscribe"
    assert by_email["g@h.com"].reason == "unsubscribe"
    assert {r.source for r in rows} == {"import"}


@pytest.mark.asyncio
async def test_an_unknown_default_reason_falls_back(db_session: AsyncSession):
    result = await suppression.import_csv(db_session, _csv(3), default_reason="whatever")
    assert result["added"] == 3
    rows = (await db_session.execute(select(SuppressionEntry))).scalars().all()
    assert {r.reason for r in rows} == {"unsubscribe"}


@pytest.mark.asyncio
async def test_the_endpoint_still_reports_the_same_summary(client, db_session: AsyncSession):
    content = _csv(5)
    files = {"file": ("dnc.csv", io.BytesIO(content.encode()), "text/csv")}
    resp = await client.post("/api/v1/dialtone/suppression/import", files=files)
    assert resp.status_code == 200
    assert resp.json() == {"added": 5, "duplicates": 0, "invalid": 0, "total": 5}
