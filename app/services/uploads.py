"""Bounded reads for multipart file uploads.

Every upload endpoint here wants the same thing: "give me the bytes, but never
more than N of them". The obvious spelling of that is::

    raw = await file.read()
    if len(raw) > MAX:
        raise HTTPException(413, ...)

which is the shape three endpoints in this codebase had, each with a comment
explaining that the cap was there so a runaway upload could not exhaust memory.
It does not do that. ``UploadFile.read()`` with no argument returns the *entire*
body as one ``bytes`` object, so the allocation the check is meant to prevent
has already happened by the time the check runs — a 2 GB upload is 2 GB of heap
in this process, and then a 413. The limit was enforced on the response and not
on the resource.

:func:`read_capped` reads in fixed-size chunks and gives up as soon as the total
crosses the cap, so the peak allocation is the cap plus one chunk regardless of
how much the client sends.

What this does *not* cover, stated plainly so the next reader doesn't assume
more than is true: Starlette parses the multipart body before the handler is
called, spooling anything over ~1 MB to a temporary file. So an oversized upload
still costs disk and transfer time upstream of here. Bounding that as well means
rejecting on ``Content-Length`` at the edge (nginx ``client_max_body_size``),
which is where a body-size limit belongs. This bounds the part that is ours: the
heap.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile, status

# Read granularity. Large enough that a legitimate few-MB CSV is a handful of
# awaits, small enough that the overshoot past the cap is negligible.
CHUNK_SIZE = 64 * 1024


async def read_capped(
    file: UploadFile,
    max_bytes: int,
    *,
    what: str = "File",
) -> bytes:
    """Return the contents of *file*, or raise 413 once it exceeds *max_bytes*.

    Never holds more than ``max_bytes + CHUNK_SIZE`` bytes at once: the read
    stops at the first chunk that carries the total past the cap, and the
    accumulated chunks are dropped by raising rather than joined into a result.

    ``what`` names the thing in the error message ("File", "Image") so the
    caller's 413 reads the way its own endpoint's other errors do.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{what} too large (max {_human_size(max_bytes)}).",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _human_size(num_bytes: int) -> str:
    """Render a byte count the way a limit is written down: "5 MB", "900 KB"."""
    if num_bytes >= 1024 * 1024 and num_bytes % (1024 * 1024) == 0:
        return f"{num_bytes // (1024 * 1024)} MB"
    if num_bytes >= 1024 and num_bytes % 1024 == 0:
        return f"{num_bytes // 1024} KB"
    return f"{num_bytes} bytes"
