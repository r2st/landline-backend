"""The SES fallback transport ships the one-click unsubscribe headers too.

Gmail and SMTP both set ``List-Unsubscribe`` + ``List-Unsubscribe-Post``. The
third transport — Amazon SES, the branch ``send_outreach_email`` takes when no
Gmail/SMTP account resolves — could not: SES's ``send_email`` action takes a
structured message and has nowhere to put a custom header. So the one path that
carries the volume on the day an operator's mailboxes are down was the one
rating itself, to every bulk filter, as a sender with no one-click affordance.

Three things are pinned here:

* the header builder is shared, so the three transports cannot drift apart
  again (the divergence is what let this survive — the gap is only visible from
  the transport that lacks it, and that one is a *fallback*);
* SES switches to ``send_raw_email`` when there is a URL, and still uses the
  cheaper structured call when there is not (the kill-switch admin notice);
* ``send_outreach_email``'s SES branch passes the same URL its Gmail/SMTP
  branch does, rather than building a second one or none.

And one thing about the *cost* of switching actions. ``ses:SendRawEmail`` is a
separate IAM action from ``ses:SendEmail``, so an identity provisioned back when
this transport only ever made the structured call is not allowed to make the new
one — and the switch would turn every fallback send into an ``AccessDenied`` on
the one day the fallback runs. Missing headers are a deliverability problem;
sending nothing at all is an outage. The permission failure specifically falls
back to the structured call and logs what to grant; every *other* SES error is
reported as-is, because a throttle or a rejected address fails identically on
both actions and retrying it is a wasted round trip.
"""

import pytest

from app.services.opt_out import unsubscribe_headers


# --- The shared builder -------------------------------------------------------


def test_headers_carry_both_methods_and_one_click():
    headers = unsubscribe_headers(
        "https://api.example.com/api/v1/track/unsubscribe?token=abc",
        "sender@example.com",
    )
    assert "<mailto:sender@example.com?subject=unsubscribe>" in headers["List-Unsubscribe"]
    assert (
        "<https://api.example.com/api/v1/track/unsubscribe?token=abc>"
        in headers["List-Unsubscribe"]
    )
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_headers_empty_without_url():
    """No URL → no headers at all, so callers can splat unconditionally."""
    assert unsubscribe_headers(None, "sender@example.com") == {}
    assert unsubscribe_headers("", "sender@example.com") == {}


def test_all_three_transports_use_the_shared_builder():
    """Gmail, SMTP and SES produce the *same* header pair for the same URL.

    The control on the fix: a fourth transport, or an edit to one of the three,
    that hand-rolls the pair again shows up as a difference here rather than as
    mail that quietly lacks the affordance.
    """
    url = "https://api.example.com/api/v1/track/unsubscribe?token=xyz"
    expected = unsubscribe_headers(url, "sender@example.com")

    from app.services.gmail_sender import _apply_unsubscribe_headers as gmail_apply
    from app.services.smtp_sender import _apply_unsubscribe_headers as smtp_apply
    from email.mime.multipart import MIMEMultipart

    gmail_msg = MIMEMultipart("alternative")
    gmail_apply(gmail_msg, url, "sender@example.com")
    smtp_msg = MIMEMultipart("alternative")
    smtp_apply(smtp_msg, url, "sender@example.com")
    ses_msg = _ses_raw_message(url)

    for name, value in expected.items():
        assert gmail_msg[name] == value
        assert smtp_msg[name] == value
        assert ses_msg[name] == value


# --- SES -----------------------------------------------------------------------


class _RecordingSesClient:
    """Stands in for the boto3 SES client, recording which action was called."""

    def __init__(self):
        self.raw_calls = []
        self.structured_calls = []

    def send_raw_email(self, **kwargs):
        self.raw_calls.append(kwargs)
        return {"MessageId": "raw-1"}

    def send_email(self, **kwargs):
        self.structured_calls.append(kwargs)
        return {"MessageId": "structured-1"}


def _ses_sender(monkeypatch) -> tuple:
    from app.services import email_sender as email_sender_module

    monkeypatch.setattr(
        email_sender_module.settings, "aws_access_key_id", "AKIA-test", raising=False
    )
    monkeypatch.setattr(
        email_sender_module.settings, "aws_secret_access_key", "secret", raising=False
    )
    sender = email_sender_module.EmailSender()
    sender.sender = "outbound@example.com"
    client = _RecordingSesClient()
    sender._client = client
    return sender, client


def _ses_raw_message(url: str):
    """The MIME document SES would put on the wire for ``url``."""
    from email import message_from_string

    from app.services.email_sender import EmailSender

    sender = EmailSender()
    sender.sender = "sender@example.com"
    client = _RecordingSesClient()
    sender._client = client
    sender._send_raw(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        reply_to=None,
        list_unsubscribe_url=url,
    )
    return message_from_string(client.raw_calls[0]["RawMessage"]["Data"])


def test_ses_send_email_sets_unsubscribe_headers(monkeypatch):
    from email import message_from_string

    sender, client = _ses_sender(monkeypatch)
    url = "https://api.example.com/api/v1/track/unsubscribe?token=abc"

    result = sender.send_email(
        to_email="lead@example.com",
        subject="Quick question",
        body_html="<p>Hi</p>",
        body_text="Hi",
        include_unsubscribe=False,
        list_unsubscribe_url=url,
    )

    assert result["status"] == "sent"
    assert client.structured_calls == []
    assert len(client.raw_calls) == 1

    msg = message_from_string(client.raw_calls[0]["RawMessage"]["Data"])
    assert url in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["To"] == "lead@example.com"
    assert msg["Subject"] == "Quick question"


def test_ses_raw_message_keeps_text_before_html(monkeypatch):
    """RFC 2046: clients render the *last* alternative they understand."""
    from email import message_from_string

    sender, client = _ses_sender(monkeypatch)
    sender.send_email(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi there</p>",
        body_text="Hi there",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )

    msg = message_from_string(client.raw_calls[0]["RawMessage"]["Data"])
    subtypes = [part.get_content_subtype() for part in msg.get_payload()]
    assert subtypes == ["plain", "html"]


def test_ses_reply_to_survives_the_raw_path(monkeypatch):
    from email import message_from_string

    sender, client = _ses_sender(monkeypatch)
    sender.send_email(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        reply_to="replies@example.com",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )

    msg = message_from_string(client.raw_calls[0]["RawMessage"]["Data"])
    assert msg["Reply-To"] == "replies@example.com"


def test_ses_without_url_keeps_the_structured_call(monkeypatch):
    """The admin notification has no unsubscribe URL and must not change shape."""
    sender, client = _ses_sender(monkeypatch)

    result = sender.send_email(
        to_email="ops@example.com",
        subject="[Dialtone] PAUSED",
        body_html="<pre>halted</pre>",
        body_text="halted",
        include_unsubscribe=False,
    )

    assert result["status"] == "sent"
    assert client.raw_calls == []
    assert len(client.structured_calls) == 1
    assert client.structured_calls[0]["Destination"] == {"ToAddresses": ["ops@example.com"]}


def test_ses_raw_path_reports_transport_errors(monkeypatch):
    """A raise out of send_raw_email returns the error dict, never propagates."""
    sender, client = _ses_sender(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("SES is down")

    client.send_raw_email = _boom

    result = sender.send_email(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )
    assert result["status"] == "error"
    assert "SES is down" in result["detail"]


# --- ses:SendRawEmail is a permission the deployed identity may not hold -------


class _ClientError(Exception):
    """The shape botocore raises: an exception carrying a ``response`` dict."""

    def __init__(self, code: str, message: str = "denied"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def test_denied_raw_send_falls_back_to_the_structured_call(monkeypatch, caplog):
    """An IAM identity without ``ses:SendRawEmail`` still gets the mail out.

    Mutation control: without the fallback this returns ``status == "error"`` and
    nothing reaches ``send_email`` — which is the regression the switch to raw
    MIME would otherwise ship, on the only transport that runs when the
    operator's own mailboxes are down.
    """
    import logging

    sender, client = _ses_sender(monkeypatch)

    def _denied(**kwargs):
        raise _ClientError(
            "AccessDenied",
            "User arn:aws:iam::1:user/landline is not authorized to perform: "
            "ses:SendRawEmail",
        )

    client.send_raw_email = _denied

    with caplog.at_level(logging.ERROR):
        result = sender.send_email(
            to_email="lead@example.com",
            subject="Hi",
            body_html="<p>Hi</p>",
            body_text="Hi",
            include_unsubscribe=False,
            list_unsubscribe_url="https://api.example.com/u?token=abc",
        )

    assert result["status"] == "sent"
    assert result["message_id"] == "structured-1"
    assert len(client.structured_calls) == 1
    # The operator has to be told the affordance is missing and exactly why —
    # a silent downgrade is how a deliverability problem becomes permanent.
    assert "ses:SendRawEmail" in caplog.text


def test_a_denied_fallback_carries_the_same_message(monkeypatch):
    """The downgrade loses the headers and must lose nothing else."""
    sender, client = _ses_sender(monkeypatch)

    def _denied(**kwargs):
        raise _ClientError("AccessDeniedException")

    client.send_raw_email = _denied

    sender.send_email(
        to_email="lead@example.com",
        subject="Quick question",
        body_html="<p>Body</p>",
        body_text="Body",
        reply_to="replies@example.com",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )

    call = client.structured_calls[0]
    assert call["Destination"] == {"ToAddresses": ["lead@example.com"]}
    assert call["Message"]["Subject"]["Data"] == "Quick question"
    assert call["Message"]["Body"]["Html"]["Data"] == "<p>Body</p>"
    assert call["Message"]["Body"]["Text"]["Data"] == "Body"
    assert call["ReplyToAddresses"] == ["replies@example.com"]


@pytest.mark.parametrize("code", ["Throttling", "MessageRejected", "MailFromDomainNotVerified"])
def test_other_ses_errors_are_not_retried_structured(monkeypatch, code):
    """Only an authorization failure is about *which action* we called.

    Everything else fails the same way on ``send_email``, so a retry is a second
    round trip and a second identical log line — and, for a throttle, pressure on
    the thing that is already refusing us.
    """
    sender, client = _ses_sender(monkeypatch)

    def _fails(**kwargs):
        raise _ClientError(code)

    client.send_raw_email = _fails

    result = sender.send_email(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi</p>",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )

    assert result["status"] == "error"
    assert result["error_code"] == code
    assert client.structured_calls == []


def test_a_bare_exception_is_not_mistaken_for_a_permission_problem(monkeypatch):
    """No ``response`` attribute at all (a socket error) must not retry either."""
    sender, client = _ses_sender(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("connection reset")

    client.send_raw_email = _boom

    result = sender.send_email(
        to_email="lead@example.com",
        subject="Hi",
        body_html="<p>Hi</p>",
        include_unsubscribe=False,
        list_unsubscribe_url="https://api.example.com/u?token=abc",
    )

    assert result["status"] == "error"
    assert client.structured_calls == []


# --- The dispatcher's SES branch ----------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_ses_fallback_passes_the_unsubscribe_url(monkeypatch, db_session):
    """``send_outreach_email`` with no sending account still sets the headers.

    Mutation control: this fails against the previous code, where the SES branch
    simply did not pass ``list_unsubscribe_url``.
    """
    from app.services import email_dispatch

    captured = {}

    class _FakeSes:
        sender = "outbound@example.com"

        def send_email(self, **kwargs):
            captured.update(kwargs)
            return {"status": "sent", "message_id": "ses-1"}

    monkeypatch.setattr(email_dispatch, "EmailSender", lambda: _FakeSes())

    async def _no_account(db):
        return None

    monkeypatch.setattr(email_dispatch, "get_active_account", _no_account)
    monkeypatch.setattr(email_dispatch, "get_next_rotation_account", _no_account)

    result = await email_dispatch.send_outreach_email(
        db_session,
        to_email="lead@example.com",
        subject="Quick question",
        body_html="<p>Hi</p>",
        include_unsubscribe=True,
        skip_spam_scan=True,
    )

    assert result["status"] == "sent"
    assert captured["list_unsubscribe_url"], "SES fallback shipped without the header"
    assert "/track/unsubscribe?token=" in captured["list_unsubscribe_url"]
    # The footer is already in the body; SES must not append a second one.
    assert captured["include_unsubscribe"] is False


@pytest.mark.asyncio
async def test_dispatcher_ses_fallback_omits_url_when_unsubscribe_disabled(
    monkeypatch, db_session
):
    """Transactional mail (include_unsubscribe=False) gets no header either."""
    from app.services import email_dispatch

    captured = {}

    class _FakeSes:
        sender = "outbound@example.com"

        def send_email(self, **kwargs):
            captured.update(kwargs)
            return {"status": "sent", "message_id": "ses-1"}

    monkeypatch.setattr(email_dispatch, "EmailSender", lambda: _FakeSes())

    async def _no_account(db):
        return None

    monkeypatch.setattr(email_dispatch, "get_active_account", _no_account)
    monkeypatch.setattr(email_dispatch, "get_next_rotation_account", _no_account)

    await email_dispatch.send_outreach_email(
        db_session,
        to_email="lead@example.com",
        subject="Your password",
        body_html="<p>Hi</p>",
        include_unsubscribe=False,
        skip_spam_scan=True,
    )

    assert captured["list_unsubscribe_url"] is None
