"""Read models for the enrollments whose sends keep throwing.

The point of this surface is that a failing enrollment used to be invisible.
It stayed ``active`` with a due date, so every dashboard counted it as healthy
outreach; the only symptom was a contact who quietly stopped receiving mail
while the queue re-dispatched the same doomed send every five minutes.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class FailingEnrollment(BaseModel):
    """One enrollment that is currently failing, with enough to act on it."""

    enrollment_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    company_name: Optional[str] = None
    sequence_id: Optional[uuid.UUID] = None
    sequence_name: Optional[str] = None
    step_number: int

    status: str
    # True once the failures crossed the threshold and the enrollment was paused.
    # The distinction that matters: a *failing* enrollment is still trying and
    # may recover on its own; a *quarantined* one has stopped and will not move
    # again until a human releases it.
    quarantined: bool
    consecutive_failures: int
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    # When the next attempt is scheduled — on a quarantined row this is the
    # attempt that will not happen, kept because "it was backing off to here"
    # is useful context for whoever picks it up.
    next_send_at: Optional[datetime] = None


class FailingEnrollmentsResponse(BaseModel):
    """Everything currently failing, worst first."""

    total: int
    # Split out because they need different responses: quarantined rows need a
    # human, failing-but-retrying rows need watching. A single count of "23
    # problems" hides which of those it is.
    quarantined: int
    retrying: int
    # Most common ``last_error`` texts, truncated and counted. One fault usually
    # explains most of the list — a revoked token, a provider outage — and this
    # says so without reading every row.
    top_errors: list[dict] = []
    enrollments: list[FailingEnrollment] = []


class QuarantineReleaseResult(BaseModel):
    """The outcome of releasing one quarantined enrollment."""

    released: bool
    # Present when the release was refused — currently only ``not_quarantined``,
    # meaning the enrollment is paused for a reason this endpoint does not own
    # (a dead domain, a consent block, an operator's decision) and must not undo.
    reason: Optional[str] = None
    resumed: bool = False
    status: Optional[str] = None
    next_send_at: Optional[datetime] = None
