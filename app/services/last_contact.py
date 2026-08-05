"""When did we last actually email them?

One resolver, because there were three and they agreed on the wrong answer.

**Sent mail lives in two tables.** Manual sends, template blasts, inbox replies
and smart-reply auto-sends land in ``email_sends``; the sequence engine — where
the bulk of outreach volume goes — writes **no** ``EmailSend`` row at all and
records the send as ``OutreachEvent(event_type='sent')``. Every "have we emailed
them?" query in this codebase read the first table alone, so a prospect three
steps into a live cadence came back with *no send at all*:

* ``tasks.followups`` filed them as *"Active-pipeline lead has never been
  emailed — send the first touch"* and put a reminder in a human's queue;
* ``GET /follow-ups/stale-contacts`` listed them as stale with
  ``last_emailed_at: null``;
* ``suggestions._stale_leads`` proposed *"In 'outreach' but never emailed —
  re-engage"*, for a company whose pipeline stage was set **by** the enrollment
  that has been mailing it.

All three are "chase this lead, nobody has" surfaces, so the error compounds in
one direction: the contacts we mail most are the ones they nominate hardest, and
the operator's manual touch is not recorded by any of the paths they read, so
the same lead resurfaces on every run.

The second half is the opposite error from the same omission. ``EmailSend.status``
is a *lifecycle* column and ``skipped`` is one of its values: the template blast
writes it for a contact with no address, one who unsubscribed and one on the
suppression list, and ``send_outreach_email`` returns it for a spam-gate block, a
warm-up hold, a spent daily cap and an engaged kill switch. All of them stamp
``sent_at`` and none put a message on the wire — so counting them reads as
"contacted recently" and holds the lead *out* of the queue. The day a cap holds a
blast is the day these surfaces stop chasing the leads it held. Hence
``WIRE_STATUSES``, which deliberately keeps ``bounced`` (mail that went out and
was rejected afterwards) and every engagement-advanced status.

``OutreachEvent`` carries no ``contact_id`` — the enrollment holds it — which is
the whole reason it is the easy table to forget: including it means a join, so
the shortest correct query is longer than the wrong one.
"""

from sqlalchemy import func, select, union_all

from app.models.contact import Contact
from app.models.email_template import EmailSend, WIRE_STATUSES
from app.models.outreach import OutreachEnrollment, OutreachEvent


def _wire_sends():
    """Every message that reached the wire, as ``(contact_id, sent_at)`` rows."""
    direct = select(
        EmailSend.contact_id.label("contact_id"),
        EmailSend.sent_at.label("sent_at"),
    ).where(
        EmailSend.contact_id.isnot(None),
        EmailSend.status.in_(WIRE_STATUSES),
    )
    sequence = (
        select(
            OutreachEnrollment.contact_id.label("contact_id"),
            OutreachEvent.occurred_at.label("sent_at"),
        )
        .join(OutreachEnrollment, OutreachEnrollment.id == OutreachEvent.enrollment_id)
        .where(OutreachEvent.event_type == "sent")
    )
    return union_all(direct, sequence).subquery()


def last_send_by_contact():
    """Subquery of ``(contact_id, last_sent_at)`` across both send tables.

    Outer-join it on ``Contact.id`` and read ``.c.last_sent_at``; a NULL there
    means this contact has genuinely never been mailed by any path.
    """
    both = _wire_sends()
    return (
        select(
            both.c.contact_id.label("contact_id"),
            func.max(both.c.sent_at).label("last_sent_at"),
        )
        .group_by(both.c.contact_id)
        .subquery()
    )


def last_send_by_company():
    """Subquery of ``(company_id, last_sent_at)`` — the most recent mail to anyone there.

    The company-level question the suggestions feed asks. Joined through
    ``Contact`` rather than derived from :func:`last_send_by_contact` so a
    contact with no company (the scraper creates those) drops out here instead
    of grouping under a NULL key.
    """
    both = _wire_sends()
    return (
        select(
            Contact.company_id.label("company_id"),
            func.max(both.c.sent_at).label("last_sent_at"),
        )
        .join(Contact, Contact.id == both.c.contact_id)
        .where(Contact.company_id.isnot(None))
        .group_by(Contact.company_id)
        .subquery()
    )
