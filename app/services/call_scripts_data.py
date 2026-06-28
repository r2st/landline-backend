"""Canonical cold-call scripts — the single source of truth for call_scripts.

Mirrors the pattern used by ``industry_verticals`` for email sequences: the
script copy lives here as plain Python data so it can be consumed by

* the seed migration (inserts one ``call_scripts`` row per template),
* the calls API (``GET /calls/scripts`` falls back to these when the DB has no
  row, so a brand-new database — or the SQLite test DB — still serves scripts),
* the call-briefing service (attaches the right script to a rep's brief),
* the test suite.

Industry keys deliberately match ``app.services.industry_verticals`` so a
company's resolved vertical maps straight onto a call script:
``injection_molding``, ``cnc_machining``, ``metal_stamping``, ``food_beverage``.

Each template carries the full anatomy of a call: an opening hook, qualifying
questions, the industry's real pain points, a value proposition that names
Apprend's AI quality-management platform and its live demo, rebuttals to the
objections reps actually hear, and a concrete call-to-action.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services import industry_verticals as iv

# Script types we generate copy for. ``initial_cold_call`` is the detailed
# first-touch script; the other two are shorter, purpose-built.
SCRIPT_TYPES = ("initial_cold_call", "follow_up", "voicemail")


@dataclass(frozen=True)
class CallScriptTemplate:
    """One call script: industry + type, plus its full anatomy."""

    industry: str
    script_type: str
    title: str
    opening_hook: str
    qualifying_questions: list[str]
    pain_points: list[str]
    value_proposition: str
    objection_handling: list[dict]  # [{"objection": ..., "rebuttal": ...}]
    call_to_action: str

    def as_row(self) -> dict:
        return {
            "industry": self.industry,
            "script_type": self.script_type,
            "title": self.title,
            "opening_hook": self.opening_hook,
            "qualifying_questions": list(self.qualifying_questions),
            "pain_points": list(self.pain_points),
            "value_proposition": self.value_proposition,
            "objection_handling": list(self.objection_handling),
            "call_to_action": self.call_to_action,
        }


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

# Objections reps hear on almost every manufacturing cold call, regardless of
# vertical. Industry-specific scripts prepend their own first.
_COMMON_OBJECTIONS = [
    {
        "objection": "We already have a quality system / QMS.",
        "rebuttal": (
            "That's exactly who this is for — a QMS records what already happened. "
            "We catch the drift in real time, before the bad parts exist, so your "
            "QMS has fewer escapes to log. It sits alongside what you have."
        ),
    },
    {
        "objection": "We don't have budget right now.",
        "rebuttal": (
            "Totally fair, and that's why the first step is a free 48-hour pilot — "
            "no PO, no install cost. You only have a budget conversation if the pilot "
            "report shows real scrap we'd have caught. If it doesn't, we go away."
        ),
    },
    {
        "objection": "Send me some information / an email.",
        "rebuttal": (
            "Happy to — I'll send the live dashboard link so you can click around "
            "yourself. While I've got you, can I ask one thing so I send the right "
            "thing: which line or machine causes you the most scrap headaches?"
        ),
    },
    {
        "objection": "I'm not the right person.",
        "rebuttal": (
            "No problem — who owns quality or production on the floor? I'd rather "
            "spend two minutes with the right person than waste yours. Could you "
            "point me their way?"
        ),
    },
    {
        "objection": "Now's not a good time.",
        "rebuttal": (
            "I hear you, you're running a plant. I only need twenty seconds to say "
            "why I called, and if it's not relevant you'll never hear from me again. "
            "Or would later this week be easier for a proper fifteen minutes?"
        ),
    },
]


def _vertical(key: str) -> iv.IndustryVertical:
    v = iv.get_vertical(key)
    if v is None:  # pragma: no cover - keys are static and tested
        raise KeyError(f"Unknown industry vertical: {key}")
    return v


# ---------------------------------------------------------------------------
# Per-industry copy
# ---------------------------------------------------------------------------

INJECTION_MOLDING = _vertical("injection_molding")
CNC = _vertical("cnc_machining")
STAMPING = _vertical("metal_stamping")
FNB = _vertical("food_beverage")


_INITIAL: list[CallScriptTemplate] = [
    CallScriptTemplate(
        industry=INJECTION_MOLDING.key,
        script_type="initial_cold_call",
        title="Plastic Injection Molding — First-Touch Cold Call",
        opening_hook=(
            "Hi {first_name}, this is {rep_name} with Apprend Technologies — I'll be "
            "quick. I work with injection molders on cutting scrap and short shots, "
            "and most shops I call have one tool that quietly eats their margin. "
            "Did I catch you at an okay time for thirty seconds?"
        ),
        qualifying_questions=[
            "Which press or mold gives you the most scrap or rework right now?",
            "How do you find out a run drifted out of spec today — operator catch, "
            "or the customer's incoming inspection?",
            "Roughly what does a scrapped run of bad parts cost you when it happens?",
            "Who on the floor can 'feel' when a tool's about to go — and how close "
            "are they to retiring?",
            "If you could see cavity-pressure drift live and stop a run before it "
            "scrapped, would that change your day?",
        ],
        pain_points=list(INJECTION_MOLDING.pain_points),
        value_proposition=(
            "Apprend's AI quality platform watches cavity pressure, cycle time and "
            "machine signals in real time and flags drift before parts go out of "
            "spec — usually while there's still time to adjust, not scrap the run. "
            "Shops typically recover most of what they were throwing in the regrind "
            "bin. There's a live dashboard you can look at yourself: "
            f"{INJECTION_MOLDING.demo_link}"
        ),
        objection_handling=[
            {
                "objection": "Our scrap rate is already low.",
                "rebuttal": (
                    "Great — then the pilot is cheap insurance. Most 'low scrap' "
                    "shops still find one mold drifting they couldn't see. If we "
                    "find nothing, you've confirmed your floor is tight."
                ),
            },
            *_COMMON_OBJECTIONS,
        ],
        call_to_action=(
            "Let's put a free 48-hour pilot on your worst press — we connect "
            "sensors, run two days, and hand you a report of every drift and "
            "hidden scrap source. Does Tuesday or Thursday work to set it up?"
        ),
    ),
    CallScriptTemplate(
        industry=CNC.key,
        script_type="initial_cold_call",
        title="CNC Machining — First-Touch Cold Call",
        opening_hook=(
            "Hi {first_name}, this is {rep_name} with Apprend Technologies. I'll be "
            "quick — I work with machine shops on tool wear and out-of-tol scrap, "
            "and almost every shop has the tight-tolerance job that makes the floor "
            "tense. Got thirty seconds?"
        ),
        qualifying_questions=[
            "Which part or machine scraps the most expensive material when it goes "
            "out of tolerance?",
            "How do you catch tool wear today — operator feel, or a first-article "
            "rejection after the fact?",
            "What does one scrapped billet plus the remake actually cost you?",
            "Do you have a veteran machinist who can hear when a tool's going — and "
            "what happens to that knowledge when he leaves?",
            "Would catching wear live, in time to swap an insert instead of scrapping "
            "a run, be worth fifteen minutes?",
        ],
        pain_points=list(CNC.pain_points),
        value_proposition=(
            "Apprend's AI quality platform watches spindle load, vibration and tool "
            "wear in real time and flags drift before a part walks out of tolerance "
            "— usually in time to swap an insert, not scrap the run. It also learns "
            "what a good cut looks like from your own machine data, so the next "
            "operator gets your veteran's instincts on a screen. Live dashboard: "
            f"{CNC.demo_link}"
        ),
        objection_handling=[
            {
                "objection": "My machinists already know their machines.",
                "rebuttal": (
                    "No doubt — this gives them a second set of eyes that never gets "
                    "tired, and it captures what your best guy knows so it doesn't "
                    "walk out when he retires. It backs up your people, not replaces "
                    "them."
                ),
            },
            *_COMMON_OBJECTIONS,
        ],
        call_to_action=(
            "Let's run a free 48-hour pilot on one machine — sensors on, two days, "
            "then a report of every tool-wear pattern and hidden cycle-time loss we "
            "find. Can we look at Tuesday or Thursday to set it up?"
        ),
    ),
    CallScriptTemplate(
        industry=STAMPING.key,
        script_type="initial_cold_call",
        title="Metal Stamping & Forming — First-Touch Cold Call",
        opening_hook=(
            "Hi {first_name}, {rep_name} with Apprend Technologies — I'll keep it "
            "short. I work with press shops on die wear and scrap coil, and most "
            "shops have the die they baby because it cracks parts when it drifts. "
            "Thirty seconds okay?"
        ),
        qualifying_questions=[
            "Which die or press scraps the most coil when it drifts?",
            "How do you find out a tool's going today — a cracked part, or a PPAP "
            "rejection from the customer?",
            "When a die drifts on a fast progressive, how many bad parts run before "
            "anyone catches it?",
            "Do you have a die setter who can hear a press going bad — and how close "
            "is he to retiring?",
            "Would catching tonnage drift live, before the first cracked part, be "
            "worth a look?",
        ],
        pain_points=list(STAMPING.pain_points),
        value_proposition=(
            "Apprend's AI quality platform watches press tonnage, signature curves "
            "and die condition in real time and flags drift before the first cracked "
            "part — not after the reject comes back. It learns the tonnage signature "
            "of a good hit from your own press data, so the next setter sees what the "
            "veteran would have heard. Live dashboard: "
            f"{STAMPING.demo_link}"
        ),
        objection_handling=[
            {
                "objection": "We do regular die maintenance already.",
                "rebuttal": (
                    "Smart — but scheduled maintenance can't see a die drifting "
                    "between PMs. This watches every hit so you fix the tool when "
                    "it's actually drifting, not on a calendar that's too early or "
                    "too late."
                ),
            },
            *_COMMON_OBJECTIONS,
        ],
        call_to_action=(
            "Let's do a free 48-hour pilot on your problem press — sensors on for "
            "two days, then a report of every tonnage drift and die-wear pattern we "
            "catch. Would Tuesday or Thursday work to get it scheduled?"
        ),
    ),
    CallScriptTemplate(
        industry=FNB.key,
        script_type="initial_cold_call",
        title="Food & Beverage — First-Touch Cold Call",
        opening_hook=(
            "Hi {first_name}, this is {rep_name} with Apprend Technologies. I'll be "
            "quick — I work with food and beverage plants on out-of-spec batches and "
            "product holds, the kind that turn into a customer call nobody wants to "
            "make. Have thirty seconds?"
        ),
        qualifying_questions=[
            "Which line gives you the most holds or out-of-spec batches?",
            "How do you catch a temperature excursion or fill drift today — at the "
            "line, or downstream at the metal detector?",
            "When a batch goes on hold, what's the real cost once you count the "
            "dumped product and the line time?",
            "Do you have a QA lead who can walk the floor and feel when something's "
            "off — and what happens on their day off?",
            "Would catching drift at the critical control point, the moment it "
            "starts, be worth fifteen minutes?",
        ],
        pain_points=list(FNB.pain_points),
        value_proposition=(
            "Apprend's AI quality platform watches your critical control points — "
            "temperature, fill, line condition — in real time and flags drift the "
            "moment it starts, before a run goes on hold or ships out of spec. It "
            "learns what a good run looks like from your own production data, so the "
            "next person on shift gets the early warning on a screen. Live dashboard: "
            f"{FNB.demo_link}"
        ),
        objection_handling=[
            {
                "objection": "We're audited (SQF/BRC) and already compliant.",
                "rebuttal": (
                    "Exactly why this helps — auditors love continuous monitoring "
                    "data, and catching excursions live means fewer holds and "
                    "corrective actions to document. It strengthens the audit story "
                    "you already tell."
                ),
            },
            *_COMMON_OBJECTIONS,
        ],
        call_to_action=(
            "Let's run a free 48-hour pilot on one line — we monitor for two days "
            "and hand you a report of every excursion, drift and hidden source of "
            "waste. Does Tuesday or Thursday work to set it up?"
        ),
    ),
]


def _follow_up(v: iv.IndustryVertical, line_word: str) -> CallScriptTemplate:
    return CallScriptTemplate(
        industry=v.key,
        script_type="follow_up",
        title=f"{v.name} — Follow-Up / Callback Call",
        opening_hook=(
            "Hi {first_name}, {rep_name} with Apprend again — thanks for the time "
            "the other day. You mentioned you'd think about the pilot, so I wanted "
            "to follow up while it's fresh. Did anything jump out for you?"
        ),
        qualifying_questions=[
            "Did you get a chance to click through the dashboard I sent?",
            f"What's the one {line_word} you'd most want the pilot to prove out?",
            "Who else would need to be in the room to green-light a free pilot?",
            "Is there anything holding it back I can clear up right now?",
        ],
        pain_points=list(v.pain_points),
        value_proposition=(
            "Just to recap why I keep calling: the pilot costs you nothing and the "
            "report is yours either way. The only thing you're risking is two days of "
            f"sensor data — and most shops find at least one issue they didn't know "
            f"they had. Dashboard's here if you want another look: {v.demo_link}"
        ),
        objection_handling=[
            {
                "objection": "Still thinking about it.",
                "rebuttal": (
                    "Totally fair. How about we book a no-commitment pilot date now — "
                    "if you're not ready when it comes up, we just move it. That way "
                    "you're not starting from scratch later."
                ),
            },
            *_COMMON_OBJECTIONS,
        ],
        call_to_action=(
            "Can we lock a pilot date this month? I'll do all the setup — you just "
            "tell me which line to point it at."
        ),
    )


def _voicemail(v: iv.IndustryVertical) -> CallScriptTemplate:
    return CallScriptTemplate(
        industry=v.key,
        script_type="voicemail",
        title=f"{v.name} — Voicemail Script",
        opening_hook=(
            "Hi {first_name}, this is {rep_name} with Apprend Technologies, "
            "{rep_phone}."
        ),
        qualifying_questions=[],
        pain_points=list(v.pain_points[:3]),
        value_proposition=(
            "We help shops like yours catch quality drift in real time before it "
            "becomes scrap — there's a free 48-hour pilot with no cost or "
            "commitment."
        ),
        objection_handling=[],
        call_to_action=(
            "I sent a short link to a live dashboard so you can see it yourself — "
            "give me a call back at {rep_phone} or just reply to the email and I'll "
            "set up the pilot. Thanks {first_name}, talk soon. Keep the message under "
            "twenty seconds."
        ),
    )


# Full registry: one initial script per vertical, plus follow-up and voicemail.
_FOLLOWUP_LINE_WORD = {
    INJECTION_MOLDING.key: "press",
    CNC.key: "machine",
    STAMPING.key: "die",
    FNB.key: "line",
}

CALL_SCRIPTS: list[CallScriptTemplate] = [
    *_INITIAL,
    *[_follow_up(v, _FOLLOWUP_LINE_WORD[v.key]) for v in iv.all_verticals()],
    *[_voicemail(v) for v in iv.all_verticals()],
]

# (industry, script_type) -> template
_BY_KEY: dict[tuple[str, str], CallScriptTemplate] = {
    (s.industry, s.script_type): s for s in CALL_SCRIPTS
}


def all_scripts() -> list[CallScriptTemplate]:
    return list(CALL_SCRIPTS)


def get_script(industry: str, script_type: str = "initial_cold_call") -> CallScriptTemplate | None:
    """Best-effort script lookup, falling back to the initial cold-call script."""
    return _BY_KEY.get((industry, script_type)) or _BY_KEY.get((industry, "initial_cold_call"))


def resolve_script(
    industry: str = "",
    company_name: str = "",
    script_type: str = "initial_cold_call",
) -> CallScriptTemplate:
    """Map a company's free-text industry/name onto a script via the vertical
    router, always returning a script (defaults to the flagship vertical)."""
    vertical = iv.resolve_vertical_or_default(industry, company_name)
    script = get_script(vertical.key, script_type) or get_script(
        iv.DEFAULT_VERTICAL.key, script_type
    )
    # The default vertical always has an initial_cold_call script, so this is a
    # belt-and-braces fallback that keeps the return type non-optional.
    return script or _INITIAL[0]


def seed_call_scripts(conn) -> int:
    """Idempotently insert one ``call_scripts`` row per template.

    Engine-agnostic (raw SQL on the Alembic bind). Skips any (industry,
    script_type) pair that already exists so re-running is safe. Returns the
    number of rows inserted.
    """
    import json as _json
    import uuid as _uuid

    import sqlalchemy as sa

    inserted = 0
    for tpl in CALL_SCRIPTS:
        existing = conn.execute(
            sa.text(
                "SELECT id FROM call_scripts "
                "WHERE industry = :industry AND script_type = :script_type"
            ),
            {"industry": tpl.industry, "script_type": tpl.script_type},
        ).first()
        if existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO call_scripts "
                "(id, industry, script_type, title, opening_hook, qualifying_questions, "
                " pain_points, value_proposition, objection_handling, call_to_action) "
                "VALUES (:id, :industry, :script_type, :title, :opening_hook, "
                " :qualifying_questions, :pain_points, :value_proposition, "
                " :objection_handling, :call_to_action)"
            ),
            {
                "id": str(_uuid.uuid4()),
                "industry": tpl.industry,
                "script_type": tpl.script_type,
                "title": tpl.title,
                "opening_hook": tpl.opening_hook,
                "qualifying_questions": _json.dumps(list(tpl.qualifying_questions)),
                "pain_points": _json.dumps(list(tpl.pain_points)),
                "value_proposition": tpl.value_proposition,
                "objection_handling": _json.dumps(list(tpl.objection_handling)),
                "call_to_action": tpl.call_to_action,
            },
        )
        inserted += 1
    return inserted
