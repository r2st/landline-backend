"""Per-sequence tone, and the truthfulness guardrail it must not undermine.

Two things are being protected here:

  * **Tone reaches the copy.** A tone that can be stored but has no prompt
    guidance is worse than no feature — it looks configured and does nothing.
    The model owns the storage vocabulary, ``ai_email_generator`` owns what each
    word means, and ``test_tone_vocabulary_matches_the_model`` pins them
    together so they cannot drift apart silently.

  * **No tone buys its way out of the rules.** "Urgent" is the dangerous one:
    the obvious way to write urgent copy is manufactured scarcity, which is
    both a lie and a spam-filter magnet. Its guidance points at the reader's
    real clock instead, and a test asserts it never smuggles in a banned
    trigger word.

The prompt-content tests are string assertions on a prompt, which is a blunt
instrument — they can prove a phrase is absent, not that a model behaves. They
earn their place as regression pins: each one names a specific instruction that
was in the prompt, contradicted the truthfulness rule, and got fixed.

File ordering note: this suite shares a session-scoped event_loop and is
order-fragile — async tests that hit the DB/client are defined first, async
tests that don't come after them, and sync tests last. See
tests/test_ai_compose.py for the same layout.
"""

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.models.sequence import DEFAULT_SEQUENCE_TONE, SEQUENCE_TONES
from app.services.ai_email_generator import (
    DEFAULT_TONE,
    SYSTEM_PROMPT,
    TEMPLATE_HINTS,
    TONE_GUIDANCE,
    generate_email,
    resolve_tone,
)

SEQUENCES = "/api/v1/outreach/sequences"


# ---------------------------------------------------------------------------
# API: tone round-trips (async client tests first — see module docstring)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_sequence_defaults_to_conversational(
    client: AsyncClient, admin_user, auth_headers
):
    """The stated default. A sequence created without an opinion writes in the
    house voice — the same copy it would have produced before this feature."""
    resp = await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "No opinion", "steps": []},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tone"] == "conversational"


@pytest.mark.asyncio
async def test_create_sequence_with_a_tone(client: AsyncClient, admin_user, auth_headers):
    resp = await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "Molders", "tone": "empathetic", "steps": []},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tone"] == "empathetic"


@pytest.mark.asyncio
async def test_unknown_tone_is_rejected_at_the_door(
    client: AsyncClient, admin_user, auth_headers
):
    """"empathic" is the typo waiting to happen. Rejecting it here makes it a
    422 the caller can see, instead of a sequence that silently writes in the
    default voice forever because its tone matched no guidance entry."""
    resp = await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "Typo", "tone": "empathic", "steps": []},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_tone_can_be_patched(client: AsyncClient, admin_user, auth_headers):
    created = await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "Retune me", "tone": "friendly", "steps": []},
    )
    seq_id = created.json()["id"]

    resp = await client.patch(
        f"{SEQUENCES}/{seq_id}", headers=auth_headers(admin_user),
        json={"tone": "professional"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tone"] == "professional"


@pytest.mark.asyncio
async def test_patching_another_field_leaves_tone_alone(
    client: AsyncClient, admin_user, auth_headers
):
    """``exclude_unset`` earns its keep: renaming a sequence must not silently
    reset its voice to the default."""
    created = await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "Keep my voice", "tone": "urgent", "steps": []},
    )
    seq_id = created.json()["id"]

    resp = await client.patch(
        f"{SEQUENCES}/{seq_id}", headers=auth_headers(admin_user),
        json={"name": "Renamed"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["tone"] == "urgent"


@pytest.mark.asyncio
async def test_listing_sequences_reports_tone(client: AsyncClient, admin_user, auth_headers):
    """The picker surface (``/sequences``) must show the voice, not just the
    CRUD surface — it's the screen an operator actually chooses from."""
    await client.post(
        SEQUENCES, headers=auth_headers(admin_user),
        json={"name": "Listed", "tone": "friendly", "steps": []},
    )
    resp = await client.get("/api/v1/sequences", headers=auth_headers(admin_user))
    assert resp.status_code == 200, resp.text
    listed = [s for s in resp.json() if s["name"] == "Listed"]
    assert listed and listed[0]["tone"] == "friendly"


# ---------------------------------------------------------------------------
# The tone actually reaches the prompt (async, no DB)
# ---------------------------------------------------------------------------

def _capture_prompt(monkeypatch, reply='{"subject": "s", "body": "b"}'):
    """Stub the LLM transport, returning a dict that captures the call.

    Pins the Anthropic backend on purpose — these tests assert on the
    Anthropic call shape (``messages`` carrying the user turn, ``system``
    out-of-band), and the OpenRouter equivalents live in
    tests/test_email_generator_openrouter.py. The stub sits on ``ai_service``
    because that module owns the transport; ``ai_email_generator`` no longer
    holds an SDK client of its own.
    """
    import app.services.ai_email_generator as gen
    import app.services.ai_service as ai

    captured: dict = {}

    class _Block:
        text = reply

    class _Msg:
        content = [_Block()]

    class _Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Msg()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = _Messages()

    settings = _settings(anthropic_api_key="sk-test")
    monkeypatch.setattr(ai.anthropic, "AsyncAnthropic", _FakeClient)
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(gen, "get_settings", lambda: settings)
    return captured


def _settings(**overrides):
    """Real Settings, pinned to Anthropic, so these stubs can't drift from config."""
    kwargs = dict(
        ai_provider="anthropic",
        anthropic_api_key="sk-test",
        openrouter_api_key="",
        secret_key="test-secret-key-not-for-production",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.mark.asyncio
async def test_tone_guidance_reaches_the_user_prompt(monkeypatch):
    captured = _capture_prompt(monkeypatch)

    await generate_email({"company_name": "Acme Molding"}, tone="empathetic")

    prompt = captured["messages"][0]["content"]
    assert TONE_GUIDANCE["empathetic"] in prompt


@pytest.mark.asyncio
async def test_each_tone_sends_its_own_guidance_and_no_others(monkeypatch):
    """Guards against the copy-paste bug where every tone ships the same block."""
    for tone in SEQUENCE_TONES:
        captured = _capture_prompt(monkeypatch)
        await generate_email({"company_name": "Acme"}, tone=tone)
        prompt = captured["messages"][0]["content"]

        assert TONE_GUIDANCE[tone] in prompt, f"{tone} guidance missing"
        for other in SEQUENCE_TONES:
            if other != tone:
                assert TONE_GUIDANCE[other] not in prompt, f"{tone} leaked {other}"


@pytest.mark.asyncio
async def test_unknown_tone_falls_back_to_the_house_voice_rather_than_raising(monkeypatch):
    """A send is in flight by the time copy is written. An unrecognised tone is a
    cosmetic problem and must not become a dropped touch."""
    captured = _capture_prompt(monkeypatch)

    result = await generate_email({"company_name": "Acme"}, tone="shakespearean")

    assert result["ai_generated"] is True
    assert result["tone"] == DEFAULT_TONE
    assert TONE_GUIDANCE[DEFAULT_TONE] in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_generated_payload_reports_the_tone_it_used(monkeypatch):
    _capture_prompt(monkeypatch)
    result = await generate_email({"company_name": "Acme"}, tone="urgent")
    assert result["tone"] == "urgent"


@pytest.mark.asyncio
async def test_template_fallback_reports_no_tone(monkeypatch):
    """The fallback is a static string no tone was applied to. Reporting the
    requested tone would be a lie told exactly where someone is debugging why
    the voice looks wrong."""
    import app.services.ai_email_generator as gen
    import app.services.ai_service as ai

    # No credential for ANY backend — the only state that still means "template".
    no_key = _settings(anthropic_api_key="", openrouter_api_key="", ai_provider="auto")
    monkeypatch.setattr(ai, "get_settings", lambda: no_key)
    monkeypatch.setattr(gen, "get_settings", lambda: no_key)

    result = await generate_email({"company_name": "Acme"}, tone="urgent")
    assert result["ai_generated"] is False
    assert result["tone"] is None


# ---------------------------------------------------------------------------
# Vocabulary integrity (sync)
# ---------------------------------------------------------------------------

def test_tone_vocabulary_matches_the_model():
    """A tone that is storable but has no guidance is a config that does nothing.
    A tone with guidance that can't be stored is dead code. Neither is allowed."""
    assert set(TONE_GUIDANCE) == set(SEQUENCE_TONES)


def test_defaults_agree_across_modules():
    assert DEFAULT_TONE == DEFAULT_SEQUENCE_TONE
    assert DEFAULT_TONE in TONE_GUIDANCE


def test_resolve_tone_normalizes_and_fails_soft():
    assert resolve_tone("urgent") == "urgent"
    assert resolve_tone("  Urgent  ") == "urgent"  # operator/UI whitespace + case
    assert resolve_tone(None) == DEFAULT_TONE
    assert resolve_tone("") == DEFAULT_TONE
    assert resolve_tone("nonsense") == DEFAULT_TONE


# ---------------------------------------------------------------------------
# No tone buys its way out of the deliverability rules (sync)
# ---------------------------------------------------------------------------

# Lifted from the HARD RULES block of SYSTEM_PROMPT.
SPAM_TRIGGERS = ("act now", "free money", "limited time", "click here", "winner")


def test_no_tone_guidance_smuggles_in_a_spam_trigger():
    """"Urgent" is the tone most likely to reach for exactly the words the
    deliverability rules ban. It may name the reader's real clock; it may not
    write "act now"."""
    for tone, guidance in TONE_GUIDANCE.items():
        lowered = guidance.lower()
        for trigger in SPAM_TRIGGERS:
            # The urgent guidance quotes the banned words in order to forbid
            # them, so a bare substring check would flag its own prohibition.
            if f"'{trigger}'" in lowered:
                continue
            assert trigger not in lowered, f"{tone} guidance uses spam trigger {trigger!r}"


def test_urgent_tone_forbids_manufactured_urgency():
    """The whole risk of shipping an urgent tone: that it authorises the fake
    deadline. Its guidance must say the pressure is the reader's, not invented."""
    urgent = TONE_GUIDANCE["urgent"].lower()
    assert "manufactured urgency is forbidden" in urgent
    assert "scarcity" in urgent


def test_empathetic_tone_forbids_performed_empathy():
    empathetic = TONE_GUIDANCE["empathetic"].lower()
    assert "i understand how hard that is" in empathetic


# ---------------------------------------------------------------------------
# Truthfulness guardrail (sync)
# ---------------------------------------------------------------------------

def test_system_prompt_keeps_the_truthfulness_guardrail():
    assert "TRUTHFULNESS (non-negotiable)" in SYSTEM_PROMPT
    assert "Never invent a named customer, a quote, or a specific result" in SYSTEM_PROMPT


def test_truthfulness_outranks_the_email_type_hints():
    """The structural fix behind the follow_up_2 bug. Hints live in the user
    prompt and the guardrail lives in the system prompt; when they disagree the
    model needs to be told, in the prompt, which one wins. Without this line the
    next hint that asks for a customer story quietly gets one."""
    assert "THIS RULE OUTRANKS EVERY OTHER INSTRUCTION" in SYSTEM_PROMPT


def test_hedged_attribution_is_closed_off():
    """"A client of ours saw..." is the same fabrication with a hedge in front,
    and it's the obvious way around a rule that only bans *named* customers."""
    assert "Attribution is not a loophole" in SYSTEM_PROMPT


def test_follow_up_2_no_longer_invites_a_customer_story():
    """The reported bug: the hint asked the model to "mention what a similar shop
    saw after switching" while the system prompt forbade inventing exactly that.
    The model was being asked to fabricate by one half of its own prompt."""
    hint = TEMPLATE_HINTS["follow_up_2"].lower()
    assert "similar shop saw" not in hint
    assert "after switching" not in hint
    assert "pattern" in hint  # the honest replacement


def test_re_engage_no_longer_asks_for_a_case_study():
    """Same bug, second instance — "share something new: a feature, a case study,
    a relevant trend". A case study is a result, and there are none to report."""
    hint = TEMPLATE_HINTS["re_engage"]
    assert "a feature, a case study, a relevant trend" not in hint


def test_warm_intro_does_not_invite_an_invented_connection():
    """A third instance, fabricating a *connection* rather than a result: the
    hint used to offer "maybe someone mentioned them, or you saw them at an
    event" with no requirement that any of it be true."""
    hint = TEMPLATE_HINTS["warm_intro"].lower()
    assert "maybe someone mentioned them" not in hint
    assert "only if" in hint  # the connection must come from real context


def test_follow_up_1_anchors_its_number_to_the_readers_own_shop():
    """"Add ONE new thing — a relevant number" is an open invitation to invent a
    statistic. Numbers are allowed, but only as estimates the reader can check."""
    hint = TEMPLATE_HINTS["follow_up_1"].lower()
    assert "estimate" in hint
