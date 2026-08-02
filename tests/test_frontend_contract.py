"""The routes the frontend calls must exist, with the methods it uses.

This file exists because the same failure has now happened twice.

June 2026: the frontend was built against guessed contracts; the reports,
clients and outreach-events endpoints diverged and the calls silently failed
into empty states. July 2026: the review queue read six candidate fields that
`CandidateRead` didn't declare, so FastAPI stripped them and every row rendered
"No email" — green frontend tests, mocked against a shape the server never
returned.

Neither side's unit tests can catch that: the frontend mocks the API, and the
backend never hears about a caller that doesn't exist yet. So this asserts the
join — that each path the frontend actually calls is registered here, with the
verb it uses. It is deliberately about *existence*, not behaviour: a route
whose response shape drifts is a different (harder) problem, but a route that
was renamed or never shipped is the one that keeps biting, and it's cheap to
pin.

Add a line here when the frontend starts calling a new endpoint. If a path here
has no caller in landline-frontend any more, delete the line rather than
keeping a route alive for a test.
"""

import pytest

from app.main import app

# (method, path) as registered, i.e. including the /api/v1 prefix and FastAPI's
# {param} placeholders. Grouped by the frontend surface that calls them.
FRONTEND_ROUTES = [
    # SendingKillSwitch.jsx + Settings.jsx (System)
    ("GET", "/api/v1/system/sending-status"),
    ("POST", "/api/v1/system/sending-toggle"),
    ("GET", "/api/v1/system/automation"),
    ("POST", "/api/v1/system/reset-warmup"),
    # DialtoneHealth.jsx + ResumeDialog.jsx — the auto-pause rail.
    ("GET", "/api/v1/dialtone/health/signals"),
    ("GET", "/api/v1/dialtone/health/signals/events"),
    ("POST", "/api/v1/dialtone/health/signals/test"),
    ("POST", "/api/v1/dialtone/resume"),
    # DialtoneConsole.jsx
    ("GET", "/api/v1/dialtone/console"),
    ("GET", "/api/v1/dialtone/activity"),
    # DialtoneCampaigns.jsx — cross-campaign comparison + the warm-lead list.
    ("GET", "/api/v1/dialtone/campaigns/analytics"),
    ("GET", "/api/v1/dialtone/leads/ranked"),
    # DialtoneInbox.jsx — the intent-classified reply queue.
    ("GET", "/api/v1/dialtone/inbox"),
    ("GET", "/api/v1/dialtone/inbox/stats"),
    ("GET", "/api/v1/dialtone/inbox/{reply_id}"),
    ("POST", "/api/v1/dialtone/inbox/{reply_id}/reclassify"),
    ("POST", "/api/v1/dialtone/inbox/{reply_id}/read"),
    # AiDrafts.jsx — the human gate on AI-composed first touches.
    ("GET", "/api/v1/ai-drafts"),
    ("POST", "/api/v1/ai-drafts/{draft_id}/approve"),
    ("POST", "/api/v1/ai-drafts/{draft_id}/reject"),
    # DomainHealth.jsx
    ("GET", "/api/v1/deliverability/domains"),
    ("GET", "/api/v1/deliverability/domains/{domain}"),
    ("POST", "/api/v1/deliverability/domains/{domain}/recheck"),
    # PlacesDiscovery.jsx
    ("POST", "/api/v1/dialtone/discovery/search"),
    ("GET", "/api/v1/dialtone/discovery/results"),
    ("POST", "/api/v1/dialtone/discovery/results/{business_id}/import"),
    ("GET", "/api/v1/dialtone/discovery/stats"),
    ("GET", "/api/v1/dialtone/discovery/searches"),
    ("POST", "/api/v1/dialtone/discovery/searches"),
    ("PATCH", "/api/v1/dialtone/discovery/searches/{search_id}"),
    ("DELETE", "/api/v1/dialtone/discovery/searches/{search_id}"),
    ("POST", "/api/v1/dialtone/discovery/searches/{search_id}/run"),
    # Prospecting.jsx
    ("GET", "/api/v1/prospecting/review-queue"),
    ("GET", "/api/v1/prospecting/runs"),
    ("GET", "/api/v1/prospecting/runs/{run_id}/candidates"),
    ("POST", "/api/v1/prospecting/run"),
    ("POST", "/api/v1/prospecting/candidates/{candidate_id}/approve"),
    ("POST", "/api/v1/prospecting/candidates/{candidate_id}/reject"),
    # Icp.jsx
    ("GET", "/api/v1/prospecting/icp"),
    ("PUT", "/api/v1/prospecting/icp"),
    ("POST", "/api/v1/prospecting/icp/learn"),
    # CompanyDetail.jsx — Dialtone provenance
    ("GET", "/api/v1/prospecting/companies/{company_id}/provenance"),
    # OrganizationSettings.jsx / TeamManagement.jsx / UsageDashboard.jsx (Phase 3)
    ("GET", "/api/v1/org/settings"),
    ("PUT", "/api/v1/org/settings"),
    ("GET", "/api/v1/org/members"),
    ("POST", "/api/v1/org/members/invite"),
    ("PUT", "/api/v1/org/members/{member_id}/role"),
    ("DELETE", "/api/v1/org/members/{member_id}"),
    ("GET", "/api/v1/org/usage"),
    # BookingAgent.jsx — the Meeting Booking Agent dashboard.
    ("GET", "/api/v1/meeting-agent/config"),
    ("PUT", "/api/v1/meeting-agent/config"),
    ("GET", "/api/v1/meeting-agent/meetings"),
    ("GET", "/api/v1/meeting-agent/meetings/{meeting_id}"),
    ("POST", "/api/v1/meeting-agent/meetings/{meeting_id}/cancel"),
    ("GET", "/api/v1/meeting-agent/stats"),
    # ClientSuccessAgent.jsx — the autonomous client-success agent surface.
    ("GET", "/api/v1/client-success-agent/config"),
    ("PUT", "/api/v1/client-success-agent/config"),
    ("GET", "/api/v1/client-success-agent/actions"),
    ("POST", "/api/v1/client-success-agent/actions/{action_id}/approve"),
    ("POST", "/api/v1/client-success-agent/actions/{action_id}/dismiss"),
    ("GET", "/api/v1/client-success-agent/stats"),
    ("GET", "/api/v1/client-success-agent/health-overview"),
    # DialtoneInbox.jsx — dispatching the operator-approved reply.
    ("POST", "/api/v1/dialtone/inbox/{reply_id}/approve-send"),
    # GdprControls.jsx — the right-to-access / right-to-erasure console. Both
    # take the subject as data, not as a path segment.
    ("GET", "/api/v1/gdpr/export"),
    ("POST", "/api/v1/gdpr/erase"),
    # Investors.jsx / InvestorDetail.jsx — the fundraising CRM. Documents and
    # interactions are addressed by their own id: a document can be
    # investor-agnostic, so the collection does not nest under one investor.
    ("PUT", "/api/v1/investors/{investor_id}"),
    ("POST", "/api/v1/investors/{investor_id}/outreach"),
    ("GET", "/api/v1/investors/documents"),
    ("POST", "/api/v1/investors/documents"),
    ("DELETE", "/api/v1/investors/documents/{document_id}"),
    ("DELETE", "/api/v1/investors/interactions/{interaction_id}"),
    # Platform.jsx — platform-admin promote/demote management.
    ("GET", "/api/v1/platform/admins"),
    ("POST", "/api/v1/platform/promote-admin"),
    ("POST", "/api/v1/platform/demote-admin"),
]


def _registered() -> set[tuple[str, str]]:
    pairs = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            for m in methods:
                pairs.add((m, path))
    return pairs


@pytest.mark.parametrize("method,path", FRONTEND_ROUTES)
def test_frontend_route_is_registered(method: str, path: str):
    assert (method, path) in _registered(), (
        f"{method} {path} is called by landline-frontend but is not registered. "
        "Either the route moved (update the caller) or it was never shipped "
        "(the frontend is calling a contract that doesn't exist)."
    )


def test_candidate_read_declares_what_the_review_queue_renders():
    """The July 2026 regression, pinned.

    `CandidateRead` gates the response: FastAPI strips anything it doesn't
    declare, so a field the reviewer needs but the schema omits renders blank
    forever — and the frontend's own tests stay green against the mock.
    Prospecting.jsx reads each of these to decide whether to arm a cold email.
    """
    from app.routers.prospecting import CandidateRead

    required = {
        "raw_data",       # industry / city / state / country
        "has_email",      # is there anyone to email at all
        "contact_email",  # the address approval would actually write to
        "contact_name",
        "contact_title",
        "contact_phone",
        "fit_score",
        "reasoning",      # the model's rationale for the score
        "qualification",  # the deterministic component split
    }
    missing = required - set(CandidateRead.model_fields)
    assert not missing, (
        f"CandidateRead no longer declares {sorted(missing)}. The review queue "
        "renders these; undeclared means FastAPI strips them and the column "
        "silently goes blank."
    )
