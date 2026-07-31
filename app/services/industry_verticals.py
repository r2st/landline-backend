"""Industry-vertical framework for outreach sequences.

Each vertical Apprend's AI quality platform serves gets its own tailored, emotionally
targeted cold sequence: industry-specific pain language, a relevant demo link, and a
free-pilot close — all following the same sales-psychology arc as the flagship
plastic-injection-molding sequence (personal stakes -> the real cost of scrap ->
tribal-knowledge loss -> breakup + free 48-hour pilot).

This module is the single source of truth. It is consumed by:
- the seed migration (creates one ``OutreachSequence`` + steps per vertical),
- the sequences API (list industries, enroll-by-industry, suggest-industry),
- the test suite.

Adding a new vertical later is intentionally a one-place change: append an
``IndustryVertical`` to ``VERTICALS`` (and a new seed migration that calls
``seed_all_verticals``). Bodies reference ``{{demo_link}}`` and rely on
``resolve_demo_link`` to fill the correct industry demo at send time, so the same
copy stays correct even as demo URLs evolve.

Copy rules (enforced by tests): subjects < 60 chars and lowercase; bodies start
``<p>Hi {{first_name}},</p>`` and stay under ~200 words; no spam-trigger words, no
ALL-CAPS, no exclamation marks, no images, no raw URLs; step 4 offers a real free
pilot and asks for a one-word reply. No fabricated testimonials — numbers are
framed as estimates the reader checks against their own shop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services import outreach_templates as molding


@dataclass(frozen=True)
class IndustryVertical:
    """One industry vertical: its routing keywords, pain language, and sequence."""

    key: str                      # stable machine key, e.g. "cnc_machining"
    name: str                     # human label, e.g. "CNC Machining"
    sequence_name: str            # OutreachSequence.name in the DB
    description: str
    demo_link: str
    keywords: tuple[str, ...]     # case-insensitive substrings for auto-routing
    pain_points: tuple[str, ...]
    # (step_number, delay_days, subject_template, body_template)
    steps: list[tuple[int, int, str, str]]
    subject_ab: dict[int, list[str]] = field(default_factory=dict)

    def as_sequence(self) -> dict:
        return {
            "name": self.sequence_name,
            "description": self.description,
            "industry": self.key,
            "steps": [
                {
                    "step_number": n,
                    "delay_days": d,
                    "subject_template": s,
                    "body_template": b,
                }
                for (n, d, s, b) in self.steps
            ],
        }


# Standard non-decreasing cadence shared by every vertical.
STEP_DELAYS = [0, 2, 5, 9]


def _steps(s1, b1, s2, b2, s3, b3, s4, b4) -> list[tuple[int, int, str, str]]:
    return [
        (1, STEP_DELAYS[0], s1, b1),
        (2, STEP_DELAYS[1], s2, b2),
        (3, STEP_DELAYS[2], s3, b3),
        (4, STEP_DELAYS[3], s4, b4),
    ]


# ---------------------------------------------------------------------------
# Vertical 1 — Plastic Injection Molding (flagship; copy lives in outreach_templates)
# ---------------------------------------------------------------------------

INJECTION_MOLDING = IndustryVertical(
    key=molding.INDUSTRY_KEY,
    name=molding.INDUSTRY_NAME,
    sequence_name=molding.SEQUENCE_NAME,
    description=molding.SEQUENCE_DESCRIPTION,
    demo_link=molding.DEMO_LINK,
    keywords=molding.KEYWORDS,
    pain_points=molding.PAIN_POINTS,
    steps=molding.MOLDING_SEQUENCE_STEPS,
    subject_ab=molding.SUBJECT_AB_VARIANTS,
)


# ---------------------------------------------------------------------------
# Vertical 2 — CNC Machining
# ---------------------------------------------------------------------------

CNC_MACHINING = IndustryVertical(
    key="cnc_machining",
    name="CNC Machining",
    sequence_name="CNC Machining — Deal Cracker",
    description=(
        "4-step emotionally-targeted cold sequence for CNC machine shops. Arc: "
        "personal stakes -> the real cost of scrap -> tribal-knowledge loss -> "
        "breakup + free 48-hour process pilot."
    ),
    demo_link="https://cnc-process-optimizer.pages.dev",
    keywords=(
        "cnc", "machining", "machine shop", "machinist", "milling", "turning",
        "lathe", "swiss", "precision part", "precision machin",
    ),
    pain_points=(
        "tool wear", "out-of-tolerance parts", "first-article rejection",
        "spindle crash", "scrapped billets", "tight tolerances",
        "cycle-time loss", "quoting accuracy", "the retiring veteran machinist",
    ),
    steps=_steps(
        "{{first_name}}, who eats it when a part runs out of tol?",
        "<p>Hi {{first_name}},</p>"
        "<p>Every shop has the job that makes the floor tense — the tight-tolerance "
        "part where a little tool wear you didn't catch turns a full bar of titanium "
        "into a scrap bin, and a first-article rejection turns into a very "
        "uncomfortable call with the customer.</p>"
        "<p>At {{company_name}}, you probably know which machine and which part I "
        "mean.</p>"
        "<p>We watch spindle load, vibration and tool wear in real time and flag "
        "drift before the part walks out of tolerance — usually while there's still "
        "time to swap an insert, not scrap a run. "
        '<a href="{{demo_link}}">Here\'s the live dashboard.</a></p>'
        "<p>Worth 15 minutes to see if it'd have caught your last scrapped run?</p>",

        "the scrap math your shop doesn't put on a report",
        "<p>Hi {{first_name}},</p>"
        "<p>Quick math that tends to sting. Take a scrapped part: the raw material, "
        "plus the machine-hours you already burned on it, plus the rush to remake it "
        "before the ship date. On a tight-tolerance job in expensive stock, one bad "
        "run can wipe the margin on the whole order.</p>"
        "<p>For a shop running 10–20 machines, undetected tool wear and out-of-tol "
        "scrap usually cost between <strong>$150K and $400K a year</strong> once you "
        "count remakes and overtime. Run your own numbers — they're rarely on a "
        "report anyone reads.</p>"
        "<p>The pattern we see: catching wear in real time turns most of those "
        "scrapped parts back into shipped ones. "
        '<a href="{{demo_link}}">This is what watching it live looks like.</a></p>'
        "<p>Want me to walk your numbers with you for 15 minutes? No slides.</p>",

        "your best machinist retires. who reads the cut then?",
        "<p>Hi {{first_name}},</p>"
        "<p>There's a conversation I have with almost every shop owner: "
        "<em>\"We've got one machinist — 20-plus years — who can hear when a tool's "
        "about to go and dial in feeds and speeds by feel. When he retires, half our "
        "process knowledge walks out with him.\"</em></p>"
        "<p>That knowledge isn't in any setup sheet. It's in his hands and his ear.</p>"
        "<p>Our system learns what a good cut looks like on every job from your own "
        "machine data, so the next operator gets the veteran's instincts on a screen "
        "instead of guessing. "
        '<a href="{{demo_link}}">See how it reads a spindle.</a></p>'
        "<p>Is that succession on your radar yet, or still a someday problem?</p>",

        "I'll stop after this one, {{first_name}}",
        "<p>Hi {{first_name}},</p>"
        "<p>Three emails in — I know that's a lot from a stranger, so this is my "
        "last. I keep writing because I've watched good shops lose their best "
        "customer over a couple of quality escapes, too busy cutting parts to "
        "protect the business that cuts the parts.</p>"
        "<p>So here's a real offer, no strings: a <strong>free 48-hour process "
        "pilot</strong> on one machine. We connect our sensors, run for two days, "
        "and hand you a report of every tool-wear pattern, vibration anomaly and "
        "hidden cycle-time loss we find. Most shops uncover at least one issue they "
        "didn't know they had.</p>"
        "<p>If you want it, just reply with the word <strong>\"pilot\"</strong> and "
        "I'll set it up. If not, no hard feelings. You can "
        '<a href="{{demo_link}}">look at the dashboard</a> anytime.</p>',
    ),
    subject_ab={
        1: ["{{first_name}}, who eats it when a part runs out of tol?",
            "the tight-tolerance part that keeps scrapping"],
        2: ["the scrap math your shop doesn't put on a report",
            "your scrapped billets cost more than you think"],
        3: ["your best machinist retires. who reads the cut then?",
            "what happens when your 20-year machinist leaves?"],
        4: ["I'll stop after this one, {{first_name}}",
            "closing the loop (free machine pilot inside)"],
    },
)


# ---------------------------------------------------------------------------
# Vertical 3 — Metal Stamping / Forming
# ---------------------------------------------------------------------------

METAL_STAMPING = IndustryVertical(
    key="metal_stamping",
    name="Metal Stamping & Forming",
    sequence_name="Metal Stamping & Forming — Deal Cracker",
    description=(
        "4-step emotionally-targeted cold sequence for metal stamping and forming "
        "press shops. Arc: personal stakes -> the real cost of scrap -> "
        "tribal-knowledge loss -> breakup + free 48-hour process pilot."
    ),
    demo_link="https://stamping-defect-predictor.pages.dev",
    keywords=(
        "stamping", "metal forming", "metal stamp", "press shop", "stamp",
        "progressive die", "deep draw", "blanking", "coil",
    ),
    pain_points=(
        "die wear", "burrs", "cracks and splits", "springback", "slug pulling",
        "tonnage drift", "scrap coil", "sort-and-rework", "PPAP rejections",
        "the retiring die setter",
    ),
    steps=_steps(
        "{{first_name}}, the die that's quietly wearing out",
        "<p>Hi {{first_name}},</p>"
        "<p>Every press shop has the die it babies — the one where a little wear you "
        "can't see yet means burrs, splits, or a whole skid of parts that fail at "
        "the customer's line. By the time the press tells you, you've already "
        "stamped a few thousand bad ones.</p>"
        "<p>At {{company_name}}, you know the tool I mean.</p>"
        "<p>We watch press tonnage, signature curves and die condition in real time "
        "and flag the drift before the first cracked part — not after the reject "
        "comes back. "
        '<a href="{{demo_link}}">Here\'s the live dashboard.</a></p>'
        "<p>Worth 15 minutes to see if it'd catch your problem die?</p>",

        "the scrap coil nobody puts a number on",
        "<p>Hi {{first_name}},</p>"
        "<p>Quick math that stings. Take the coil you scrap when a die drifts, plus "
        "the press hours that ran bad parts, plus the sort-and-rework when a suspect "
        "lot ships. On a progressive die running fast, a missed drift turns into a "
        "skid of scrap before anyone notices.</p>"
        "<p>For a shop running 8–15 presses, die wear and out-of-spec scrap usually "
        "run <strong>$180K to $420K a year</strong> once you count remakes, sorting "
        "and overtime. Run your own numbers — most shops are surprised.</p>"
        "<p>The pattern we see: catching tonnage drift in real time keeps most of "
        "that coil out of the scrap bin. "
        '<a href="{{demo_link}}">This is what watching it live looks like.</a></p>'
        "<p>Want me to walk your numbers for 15 minutes? No slides.</p>",

        "your best die setter retires. then what?",
        "<p>Hi {{first_name}},</p>"
        "<p>There's a conversation I have with almost every press shop owner: "
        "<em>\"We've got one die setter — 20-plus years — who can listen to a press "
        "and tell you a tool's going before it cracks a part. When he retires, that "
        "instinct leaves with him.\"</em></p>"
        "<p>That knowledge isn't in any tooling binder. It's in his ear and his "
        "hands.</p>"
        "<p>Our system learns the tonnage signature of a good hit on every die from "
        "your own press data, so the next setter sees the warning the veteran would "
        "have heard. "
        '<a href="{{demo_link}}">See how it reads a press.</a></p>'
        "<p>Is that on your radar yet, or still a someday problem?</p>",

        "last note from me, {{first_name}}",
        "<p>Hi {{first_name}},</p>"
        "<p>Three emails in — I know that's a lot from a stranger, so this is my "
        "last. I keep writing because I've watched solid press shops lose their "
        "biggest account over a couple of quality escapes, too busy running parts to "
        "protect the business that runs the parts.</p>"
        "<p>So here's a real offer, no strings: a <strong>free 48-hour process "
        "pilot</strong> on one press. We connect our sensors, run for two days, and "
        "hand you a report of every tonnage drift, die-wear pattern and hidden scrap "
        "source we find. Most shops uncover at least one issue they didn't know they "
        "had.</p>"
        "<p>If you want it, just reply with the word <strong>\"pilot\"</strong> and "
        "I'll set it up. If not, no hard feelings. You can "
        '<a href="{{demo_link}}">look at the dashboard</a> anytime.</p>',
    ),
    subject_ab={
        1: ["{{first_name}}, the die that's quietly wearing out",
            "that die {{company_name}} keeps babying"],
        2: ["the scrap coil nobody puts a number on",
            "your scrap coil costs more than you think"],
        3: ["your best die setter retires. then what?",
            "what happens when your die setter leaves?"],
        4: ["last note from me, {{first_name}}",
            "closing the loop (free press pilot inside)"],
    },
)


# ---------------------------------------------------------------------------
# Vertical 4 — Food & Beverage Manufacturing
# ---------------------------------------------------------------------------

FOOD_BEVERAGE = IndustryVertical(
    key="food_beverage",
    name="Food & Beverage Manufacturing",
    sequence_name="Food & Beverage — Deal Cracker",
    description=(
        "4-step emotionally-targeted cold sequence for food and beverage "
        "manufacturers. Arc: personal stakes -> the real cost of waste -> "
        "tribal-knowledge loss -> breakup + free 48-hour process pilot."
    ),
    demo_link="https://food-safety-monitor.pages.dev",
    keywords=(
        "food", "beverage", "dairy", "bakery", "meat", "snack", "brewery",
        "co-pack", "cpg", "ingredient",
    ),
    pain_points=(
        "out-of-spec batches", "foreign-material flags", "temperature excursions",
        "product holds", "audit findings (SQF/BRC)", "line downtime",
        "spoilage and giveaway", "recall exposure", "the retiring QA lead",
    ),
    steps=_steps(
        "{{first_name}}, the batch you'd never want to explain",
        "<p>Hi {{first_name}},</p>"
        "<p>Every plant has the moment it dreads — a batch that drifts out of spec, "
        "a foreign-material flag, or a line that ran for an hour before someone "
        "caught the temperature excursion. The cost isn't just the scrapped product. "
        "It's the hold, the customer call, and the question of whether any of it "
        "shipped.</p>"
        "<p>At {{company_name}}, you know the line I mean.</p>"
        "<p>We watch your critical control points — temperature, fill, line "
        "condition — in real time and flag drift the moment it starts, before a "
        "whole run goes on hold. "
        '<a href="{{demo_link}}">Here\'s the live dashboard.</a></p>'
        "<p>Worth 15 minutes to see if it'd catch your last hold?</p>",

        "the waste line your P&L hides",
        "<p>Hi {{first_name}},</p>"
        "<p>Quick math that stings. Take the product you dump when a batch goes out "
        "of spec, plus the line hours that ran it, plus the giveaway when fills "
        "drift heavy to stay safe. Across a year, scrap, rework and overfill quietly "
        "eat more margin than most plants track.</p>"
        "<p>For a mid-size plant, out-of-spec waste and giveaway usually run "
        "<strong>$200K to $500K a year</strong> once you count holds, remakes and "
        "overtime. Run your own numbers — they rarely sit on one report.</p>"
        "<p>The pattern we see: catching drift in real time turns most of those "
        "holds into shipped, in-spec product. "
        '<a href="{{demo_link}}">This is what watching it live looks like.</a></p>'
        "<p>Want me to walk your numbers for 15 minutes? No slides.</p>",

        "your best QA lead leaves. who catches it then?",
        "<p>Hi {{first_name}},</p>"
        "<p>There's a conversation I have with almost every plant manager: "
        "<em>\"We've got one QA lead who can walk the floor and feel when something's "
        "off — a fill drifting, a line about to foul. When she's out, we hold our "
        "breath.\"</em></p>"
        "<p>That instinct isn't written in any food-safety plan. It's experience you "
        "can't hand over in a shift.</p>"
        "<p>Our system learns what a good run looks like on every line from your own "
        "production data, so the next person on shift gets that early warning on a "
        "screen instead of finding out at the metal detector. "
        '<a href="{{demo_link}}">See how it reads a line.</a></p>'
        "<p>Is that on your radar yet, or still a someday problem?</p>",

        "I'll stop after this one, {{first_name}}",
        "<p>Hi {{first_name}},</p>"
        "<p>Three emails in — I know that's a lot from a stranger, so this is my "
        "last. I keep writing because I've watched good plants take a brand-damaging "
        "recall or fail an audit they could have passed, too busy running product to "
        "protect the product.</p>"
        "<p>So here's a real offer, no strings: a <strong>free 48-hour process "
        "pilot</strong> on one line. We connect our sensors, run for two days, and "
        "hand you a report of every drift, excursion and hidden source of waste we "
        "find. Most plants uncover at least one issue they didn't know they had.</p>"
        "<p>If you want it, just reply with the word <strong>\"pilot\"</strong> and "
        "I'll set it up. If not, no hard feelings. You can "
        '<a href="{{demo_link}}">look at the dashboard</a> anytime.</p>',
    ),
    subject_ab={
        1: ["{{first_name}}, the batch you'd never want to explain",
            "the line at {{company_name}} that keeps you up"],
        2: ["the waste line your P&L hides",
            "your out-of-spec waste costs more than you think"],
        3: ["your best QA lead leaves. who catches it then?",
            "what happens when your QA lead is out?"],
        4: ["I'll stop after this one, {{first_name}}",
            "closing the loop (free line pilot inside)"],
    },
)


# ---------------------------------------------------------------------------
# Registry + lookup helpers
# ---------------------------------------------------------------------------

# Order is the display order in the picker; injection molding is the flagship.
VERTICALS: list[IndustryVertical] = [
    INJECTION_MOLDING,
    CNC_MACHINING,
    METAL_STAMPING,
    FOOD_BEVERAGE,
]

VERTICALS_BY_KEY: dict[str, IndustryVertical] = {v.key: v for v in VERTICALS}

# Default vertical used when nothing matches (mirrors resolve_demo_link's default).
DEFAULT_VERTICAL = INJECTION_MOLDING


def all_verticals() -> list[IndustryVertical]:
    return list(VERTICALS)


def get_vertical(key: str) -> IndustryVertical | None:
    return VERTICALS_BY_KEY.get(key)


# Generic manufacturing terms that mark an industry string as in-ICP even when
# it isn't a keyword of any specific vertical ("Precision Manufacturing",
# "Metal Fabrication", ...). Substring-matched case-insensitively. Shared by
# the companies scorer and the prospect-candidate qualifier so the two scoring
# paths agree on what counts as manufacturing.
MANUFACTURING_TERMS: tuple[str, ...] = (
    "manufactur", "fabricat", "molding", "moulding", "casting", "forging",
    "extrusion", "tool and die", "tool & die", "sheet metal", "welding",
    "plastics", "aerospace", "automotive supplier",
)


def matches_manufacturing(industry: str = "", company_name: str = "") -> bool:
    """True when the industry/name matches a vertical keyword or a generic
    manufacturing term."""
    if resolve_vertical(industry, company_name) is not None:
        return True
    search = f"{industry} {company_name}".lower()
    return any(term in search for term in MANUFACTURING_TERMS)


def resolve_vertical(industry: str = "", company_name: str = "") -> IndustryVertical | None:
    """Best-match vertical for a company's industry/name, or None if no match.

    Matches on the longest keyword first so specific terms ("injection molding")
    win over generic ones ("plastic"). Case-insensitive substring match.
    """
    search = f"{industry} {company_name}".lower()
    best: tuple[int, IndustryVertical] | None = None
    for vertical in VERTICALS:
        for kw in vertical.keywords:
            if kw in search:
                if best is None or len(kw) > best[0]:
                    best = (len(kw), vertical)
    return best[1] if best else None


def resolve_vertical_or_default(industry: str = "", company_name: str = "") -> IndustryVertical:
    """Like resolve_vertical but always returns a vertical (DEFAULT_VERTICAL)."""
    return resolve_vertical(industry, company_name) or DEFAULT_VERTICAL


def seed_all_verticals(conn) -> dict[str, str]:
    """Idempotently create one OutreachSequence (+ steps) per vertical.

    Used by Alembic data migrations. For each vertical, if a sequence with its
    ``sequence_name`` already exists, its ``industry`` tag is backfilled and its
    steps are left untouched; otherwise the sequence and steps are inserted.
    Returns a {vertical_key: sequence_id} map. Engine-agnostic (works on the
    Postgres bind in migrations).
    """
    import uuid as _uuid

    import sqlalchemy as sa

    out: dict[str, str] = {}
    for vertical in VERTICALS:
        existing = conn.execute(
            sa.text("SELECT id FROM outreach_sequences WHERE name = :name"),
            {"name": vertical.sequence_name},
        ).first()
        if existing:
            seq_id = str(existing[0])
            conn.execute(
                sa.text(
                    "UPDATE outreach_sequences SET industry = :industry "
                    "WHERE id = :id AND (industry IS NULL OR industry = '')"
                ),
                {"industry": vertical.key, "id": seq_id},
            )
            out[vertical.key] = seq_id
            continue

        seq_id = str(_uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO outreach_sequences "
                "(id, name, description, industry, is_active, status) "
                "VALUES (:id, :name, :description, :industry, true, 'active')"
            ),
            {
                "id": seq_id,
                "name": vertical.sequence_name,
                "description": vertical.description,
                "industry": vertical.key,
            },
        )
        for (n, d, subj, body) in vertical.steps:
            conn.execute(
                sa.text(
                    "INSERT INTO sequence_steps "
                    "(id, sequence_id, step_number, delay_days, subject_template, body_template) "
                    "VALUES (:id, :seq, :n, :d, :subj, :body)"
                ),
                {"id": str(_uuid.uuid4()), "seq": seq_id, "n": n, "d": d,
                 "subj": subj, "body": body},
            )
        out[vertical.key] = seq_id
    return out
