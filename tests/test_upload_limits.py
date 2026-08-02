"""Upload size caps are enforced *during* the read, not after it.

The three upload endpoints all used to do ``raw = await file.read()`` and then
measure ``len(raw)``, which means the allocation the cap exists to prevent has
already happened before the cap is consulted. These tests pin both halves of the
fix: the 413 still comes back (behaviour callers depend on), and the bytes never
accumulate past the cap (the thing that was actually broken).
"""

import io

import pytest
from fastapi import HTTPException, UploadFile
from httpx import AsyncClient

from app.services import uploads


class _CountingUpload:
    """An ``UploadFile``-shaped stub that reports its peak in-flight read total.

    Stands in for a client streaming far more than the cap. ``read(n)`` honours
    the requested size, so a reader that stops at the cap stops pulling bytes —
    which is exactly what ``total_served`` measures.
    """

    def __init__(self, total_size: int, chunk_byte: bytes = b"x"):
        self.total_size = total_size
        self.chunk_byte = chunk_byte
        self.served = 0

    async def read(self, size: int = -1) -> bytes:
        remaining = self.total_size - self.served
        if remaining <= 0:
            return b""
        n = remaining if size is None or size < 0 else min(size, remaining)
        self.served += n
        return self.chunk_byte * n


@pytest.mark.asyncio
async def test_read_capped_returns_content_under_the_cap():
    upload = _CountingUpload(1000)
    data = await uploads.read_capped(upload, 5000)
    assert data == b"x" * 1000
    assert upload.served == 1000


@pytest.mark.asyncio
async def test_read_capped_allows_exactly_the_cap():
    """A file of precisely ``max_bytes`` is legal — the cap is inclusive."""
    upload = _CountingUpload(2048)
    data = await uploads.read_capped(upload, 2048)
    assert len(data) == 2048


@pytest.mark.asyncio
async def test_read_capped_rejects_and_stops_reading_past_the_cap():
    """The point of the exercise: an oversized upload is not fully buffered.

    100 MB offered, 1 KB cap. A read-then-measure implementation pulls all
    100 MB before raising; this one must stop within a chunk of the cap.
    """
    cap = 1024
    upload = _CountingUpload(100 * 1024 * 1024)

    with pytest.raises(HTTPException) as exc:
        await uploads.read_capped(upload, cap)

    assert exc.value.status_code == 413
    # Bounded by cap + one chunk, and nowhere near the 100 MB on offer.
    assert upload.served <= cap + uploads.CHUNK_SIZE


@pytest.mark.asyncio
async def test_read_capped_error_names_the_thing_and_the_limit():
    upload = _CountingUpload(20)
    with pytest.raises(HTTPException) as exc:
        await uploads.read_capped(upload, 10)
    assert "10 bytes" in exc.value.detail

    upload = _CountingUpload(6 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc:
        await uploads.read_capped(upload, 5 * 1024 * 1024, what="Image")
    assert exc.value.detail.startswith("Image too large")
    assert "5 MB" in exc.value.detail


@pytest.mark.asyncio
async def test_read_capped_handles_an_empty_upload():
    assert await uploads.read_capped(_CountingUpload(0), 1024) == b""


@pytest.mark.asyncio
async def test_read_capped_works_on_a_real_uploadfile():
    """Guard against the stub drifting from Starlette's actual read() contract."""
    real = UploadFile(filename="x.csv", file=io.BytesIO(b"a,b,c\n" * 100))
    assert await uploads.read_capped(real, 10_000) == b"a,b,c\n" * 100

    real = UploadFile(filename="x.csv", file=io.BytesIO(b"z" * 5000))
    with pytest.raises(HTTPException) as exc:
        await uploads.read_capped(real, 100)
    assert exc.value.status_code == 413


# --- Endpoint-level coverage -------------------------------------------------
#
# These patch the cap down rather than posting a genuinely oversized body. The
# constant's *value* is a policy choice with no behaviour to verify; what needs
# pinning is that each endpoint routes its upload through the bounded read and
# turns the overflow into a 413. Sending real megabytes would test httpx's
# multipart encoder and spend CI minutes doing it.


@pytest.mark.asyncio
async def test_suppression_import_rejects_an_oversized_file(client: AsyncClient, monkeypatch):
    monkeypatch.setattr("app.routers.dialtone._MAX_SUPPRESSION_UPLOAD", 32)
    resp = await client.post(
        "/api/v1/dialtone/suppression/import",
        files={"file": ("big.csv", io.BytesIO(b"a@b.com\n" * 50), "text/csv")},
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_suppression_import_still_accepts_a_normal_file(client: AsyncClient):
    resp = await client.post(
        "/api/v1/dialtone/suppression/import",
        files={"file": ("ok.csv", io.BytesIO(b"one@x.com\ntwo@x.com\n"), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] == 2


@pytest.mark.asyncio
async def test_csv_import_rejects_an_oversized_file(client: AsyncClient, monkeypatch):
    monkeypatch.setattr("app.routers.import_export.MAX_UPLOAD_SIZE", 32)
    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("big.csv", io.BytesIO(b"company_name\n" + b"Acme\n" * 50), "text/csv")},
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_scan_card_rejects_an_oversized_image(client: AsyncClient, monkeypatch):
    monkeypatch.setattr("app.routers.ai.MAX_CARD_IMAGE_SIZE", 32)
    resp = await client.post(
        "/api/v1/ai/scan-card",
        files={"file": ("card.png", io.BytesIO(b"\x89PNG" + b"\x00" * 500), "image/png")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"].startswith("Image too large")


# --- Row cap on CSV import ---------------------------------------------------


@pytest.mark.asyncio
async def test_csv_import_rejects_too_many_rows(client: AsyncClient, monkeypatch):
    """The byte cap does not bound the work; the row cap does.

    A well-formed file under the size limit can still carry millions of rows,
    and every one of them costs a round of queries inside the request. The
    rejection has to happen before the first row is ingested.
    """
    monkeypatch.setattr("app.routers.import_export.MAX_IMPORT_ROWS", 10)
    body = b"company_name\n" + b"".join(f"Acme {i}\n".encode() for i in range(25))

    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("many.csv", io.BytesIO(body), "text/csv")},
    )
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "25" in detail and "10" in detail


@pytest.mark.asyncio
async def test_csv_import_rejects_before_importing_anything(
    client: AsyncClient, db_session, monkeypatch
):
    """An over-cap file must be refused whole, not applied up to the cap."""
    from sqlalchemy import func, select

    from app.models.company import Company

    monkeypatch.setattr("app.routers.import_export.MAX_IMPORT_ROWS", 3)
    before = (await db_session.execute(select(func.count(Company.id)))).scalar()

    body = b"company_name\n" + b"".join(f"Overflow {i}\n".encode() for i in range(9))
    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("many.csv", io.BytesIO(body), "text/csv")},
    )
    assert resp.status_code == 413

    after = (await db_session.execute(select(func.count(Company.id)))).scalar()
    assert after == before


@pytest.mark.asyncio
async def test_csv_import_accepts_a_file_at_exactly_the_row_cap(
    client: AsyncClient, monkeypatch
):
    """Off-by-one guard: the header is not a data row, and the cap is inclusive."""
    monkeypatch.setattr("app.routers.import_export.MAX_IMPORT_ROWS", 3)
    body = b"company_name\n" + b"".join(f"Exactly {i}\n".encode() for i in range(3))

    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("three.csv", io.BytesIO(body), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported_companies"] == 3


@pytest.mark.asyncio
async def test_csv_import_accepts_an_empty_and_header_only_file(
    client: AsyncClient, monkeypatch
):
    """Zero rows must not underflow the count into a rejection."""
    monkeypatch.setattr("app.routers.import_export.MAX_IMPORT_ROWS", 3)

    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("header.csv", io.BytesIO(b"company_name\n"), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported_companies"] == 0

    resp = await client.post(
        "/api/v1/import-export/import/csv",
        files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported_companies"] == 0
