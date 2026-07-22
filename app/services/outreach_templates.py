"""Canonical outreach sequence templates for plastic injection molding.

This is the single source of truth for the "Deal Cracker" molding sequence:
it is consumed by the seed migration (to create the sequence in the DB), by the
AI email generator (as the house voice / few-shot reference), and by the test
suite.

Copy philosophy — emotionally compelling through SPECIFICITY, not hype:
- We name real molding pain by its real name: short shots, flash, sink marks,
  core-pin wear, check-ring wear, scrap bins, PPAP/corrective-action reports,
  the 2 AM call, the retiring 22-year operator.
- Numbers are framed as honest *estimates the reader can check against their own
  shop* ("run your own numbers"), never as fabricated customer outcomes. We do
  not invent named customers, quotes, or specific results presented as fact —
  that is deceptive and violates FTC testimonial rules. Social proof is phrased
  as the pattern we hear across the industry, not a fake case study.
- Loss aversion and urgency come from the real cost of doing nothing, not from
  "ACT NOW" pressure. No all-caps, no exclamation spam, no spam-trigger words —
  those tank deliverability.
- Every step ends in ONE low-friction ask. The sequence closes with a genuine
  FREE PILOT (48-hour, one-press process audit) as the primary CTA.

Research-backed design principles (2025-2026 benchmarks):
- Optimal sequence: 4-7 emails; first email captures 58% of replies (Instantly
  2026 Benchmark Report). We use 4 steps for SMB manufacturing.
- Cadence: Days 0, 3, 7, 14 — start tight (2-4 days) then widen. The first
  couple of follow-ups come quickly while the initial email is still in the
  prospect's recent memory.
- Each email under 125 words; subject lines under 7 words; single clear CTA.
- Zero images/links in step 1 body (except demo link) — fewer images and links
  in a first cold email, the better the placement.
- Best send window: Tuesday-Thursday, 9:30-11:30 AM recipient local time.
- Signal-based timing: an email sent within 24-48h of a buying trigger (funding,
  executive hire, technology change) achieves 3-5x higher response rates.
- Deliverability > copy: proper SPF/DKIM/DMARC, 35-40 emails/day/address max,
  warmup over 4-8 weeks.

Merge fields available (rendered by app.services.email_compose.render):
    {{first_name}} {{last_name}} {{full_name}} {{company_name}} {{title}}
    {{industry}} {{city}} {{demo_link}}
Prefer wrapping {{industry}} / {{city}} in a Jinja ``{% if %}`` so a prospect
with no city/industry on file never renders an awkward blank in the copy.
"""

from __future__ import annotations

# Vertical metadata (consumed by app.services.industry_verticals).
INDUSTRY_KEY = "injection_molding"
INDUSTRY_NAME = "Plastic Injection Molding"
DEMO_LINK = "https://best-molding-demo.pages.dev"
# Substrings matched (case-insensitive) against a company's industry/name to
# auto-route it to this vertical.
KEYWORDS = (
    "injection molding", "injection mold", "injection moulding", "molding",
    "moulding", "molder", "plastic", "polymer", "resin", "thermoplastic",
)
PAIN_POINTS = (
    "short shots", "flash", "sink marks", "warpage", "core-pin wear",
    "check-ring wear", "cavity-to-cavity variation", "scrap and rework",
    "PPAP rejections", "the retiring veteran operator",
)

SEQUENCE_NAME = "Plastic Injection Molding — Deal Cracker"
SEQUENCE_DESCRIPTION = (
    "4-step emotionally-targeted cold sequence for plastic injection molders. "
    "Arc: personal stakes -> the real cost of scrap -> tribal-knowledge loss -> "
    "breakup + free 48-hour process pilot. Deliverability-hardened, CAN-SPAM "
    "compliant, no fabricated claims."
)

# Standard cadence (days to wait before sending each step). Research-backed:
# start tight (3 days) while the first email is still in memory, then widen
# the gaps (7 days, then 14). Non-decreasing so the pressure tapers.
#   Day 0:  Initial outreach (personal stakes)
#   Day 3:  Financial pain + social proof (still in recent memory)
#   Day 7:  Knowledge-loss anxiety (new angle, wider gap)
#   Day 14: Breakup + free pilot (final offer, respectful close)
STEP_DELAYS = [0, 3, 7, 14]

# ---------------------------------------------------------------------------
# The four steps. Bodies are HTML so they render cleanly; the dispatcher adds
# the signature + unsubscribe footer and the text/plain alternative.
# ---------------------------------------------------------------------------

STEP_1_SUBJECT = "{{first_name}}, who takes the blame when cavity 4 goes bad?"
STEP_1_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>Every molder I talk to{% if city %} around {{city}}{% endif %} has one "
    "mold they nurse along — the one where a bad run means a frantic weekend, an "
    "angry customer call, and an air-freight bill that eats the whole job's "
    "margin.</p>"
    "<p>Here's the part nobody likes to sit with: when a press drifts at 2 AM and "
    "the night shift misses it, when 8,000 parts ship and 400 have sink marks "
    "that slipped past visual — whose name ends up on the corrective-action "
    "report? It's yours. You carry that every day.</p>"
    "<p>We built something that watches pressure curves, melt temp and cooling "
    "profiles in real time and flags drift <em>before</em> the first bad part — "
    "usually a few cycles early. Easier to show than describe: "
    '<a href="{{demo_link}}">here\'s the live dashboard</a>.</p>'
    "<p>Worth 15 minutes to see if it'd catch the mold you're thinking of right "
    "now?</p>"
)

STEP_2_SUBJECT = "the scrap math your CFO hasn't run yet"
STEP_2_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>Quick math that might sting. Take {{company_name}}'s annual resin spend, "
    "multiply by your real scrap-and-rework rate, then add the overtime to remake "
    "parts that should've been right the first time.</p>"
    "<p>For a typical molder running 8–15 presses, that number usually lands "
    "somewhere between <strong>$220K and $480K a year</strong> — and that's "
    "before the air freight on a blown delivery or the margin you give back on "
    "the next quote because the customer remembers the last quality issue. Run "
    "your own numbers; I'd bet they're bigger than the line item you track.</p>"
    "<p>The pattern we see across {% if industry %}{{industry}}{% else %}the "
    "industry{% endif %} is simple: shops that catch drift in real time turn "
    "most of that scrap back into shipped parts. "
    '<a href="{{demo_link}}">This is what watching it live looks like.</a></p>'
    "<p>Want me to walk your numbers with you for 15 minutes? No slides.</p>"
)

STEP_3_SUBJECT = "your best operator retires in a few years. then what?"
STEP_3_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>There's a conversation I have with almost every shop owner I meet: "
    "<em>\"We've got one guy — Mike, or Steve, or Dave — 22 years in. He can "
    "listen to a press and tell you something's off. He knows every mold, every "
    "material, every trick. And he's going to retire, and I don't know what we'll "
    "do.\"</em></p>"
    "<p>That knowledge isn't written down anywhere. It lives in his hands — in "
    "the way he nudges pack pressure by feel. When he walks out the door, it "
    "leaves with him.</p>"
    "<p>Our system learns what a good cycle looks like on every mold from your "
    "own production data — not a textbook. It becomes the institutional memory "
    "that doesn't retire, doesn't call in sick, and trains the next operator "
    "faster. <a href=\"{{demo_link}}\">See how it reads a press.</a></p>"
    "<p>Is this on your radar yet, or still a someday problem?</p>"
)

STEP_4_SUBJECT = "I'll leave you alone after this, {{first_name}}"
STEP_4_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>Three emails in — I know that's a lot from someone you've never met, so "
    "this is my last one. I keep writing because I've watched shops wait too long "
    "and lose their biggest account over two quality escapes in a single quarter. "
    "Too busy running parts to protect the business that runs the parts.</p>"
    "<p>So here's a real offer, no strings: a <strong>free 48-hour process "
    "pilot</strong> on one press. We connect our sensors, run for two days, and "
    "hand you a report of every anomaly, drift pattern and hidden inefficiency we "
    "find. Most shops uncover at least one issue they didn't know they had — a "
    "worn check ring quietly adding seconds to every cycle, that kind of thing.</p>"
    "<p>If you want it, just reply with the word <strong>\"pilot\"</strong> and "
    "I'll set it up. If not, no hard feelings — I'll close the loop and you won't "
    "hear from me again. You can "
    '<a href="{{demo_link}}">look at the dashboard</a> anytime.</p>'
)

# Ordered list of (step_number, delay_days, subject_template, body_template).
MOLDING_SEQUENCE_STEPS = [
    (1, STEP_DELAYS[0], STEP_1_SUBJECT, STEP_1_BODY),
    (2, STEP_DELAYS[1], STEP_2_SUBJECT, STEP_2_BODY),
    (3, STEP_DELAYS[2], STEP_3_SUBJECT, STEP_3_BODY),
    (4, STEP_DELAYS[3], STEP_4_SUBJECT, STEP_4_BODY),
]


# ---------------------------------------------------------------------------
# A/B subject-line variants. The same body works under either subject; rotating
# subjects is the cheapest, highest-signal A/B test in cold email.
# ---------------------------------------------------------------------------

SUBJECT_AB_VARIANTS: dict[int, list[str]] = {
    1: [
        "{{first_name}}, who takes the blame when cavity 4 goes bad?",
        "that mold {{company_name}} keeps nursing along",
        "the part that keeps you up at night, {{first_name}}",
    ],
    2: [
        "the scrap math your CFO hasn't run yet",
        "your scrap bin is more expensive than you think",
        "the $300K nobody at {{company_name}} is tracking",
    ],
    3: [
        "your best operator retires in a few years. then what?",
        "what happens when Mike retires?",
        "the shops winning quotes have a quiet advantage",
    ],
    4: [
        "I'll leave you alone after this, {{first_name}}",
        "closing the loop (free press pilot inside)",
        "last note from me, {{first_name}}",
    ],
}


def get_molding_sequence() -> dict:
    """Return the molding sequence as a plain dict (name, description, steps)."""
    return {
        "name": SEQUENCE_NAME,
        "description": SEQUENCE_DESCRIPTION,
        "steps": [
            {
                "step_number": n,
                "delay_days": d,
                "subject_template": s,
                "body_template": b,
            }
            for (n, d, s, b) in MOLDING_SEQUENCE_STEPS
        ],
    }


# ---------------------------------------------------------------------------
# Research-backed outreach best practices (2025-2026)
# ---------------------------------------------------------------------------

COLD_EMAIL_BEST_PRACTICES = {
    "deliverability": {
        "spf_dkim_dmarc": "Required since Feb 2024 (Google/Yahoo), May 2025 (Microsoft)",
        "max_emails_per_inbox_per_day": 35,
        "warmup_weeks": "4-8 weeks minimum for new domains",
        "warmup_ramp": "Start at 5/day, increase by 5-10/week, cap at 25-40/day",
        "bounce_threshold": "Keep under 2%",
        "complaint_threshold": "Keep under 0.1%",
        "target_inbox_placement": "95%+ (global avg is only 84%)",
    },
    "content": {
        "max_words": 125,
        "max_subject_words": 7,
        "subject_case": "lowercase (no title case)",
        "cta_count": 1,
        "links_in_first_email": "Minimize — fewer is better for placement",
        "images_in_first_email": "Zero — images hurt cold email placement",
        "tone": "Conversational, specific, no hype",
    },
    "timing": {
        "best_days": ["Tuesday", "Wednesday", "Thursday"],
        "best_hours": "9:30-11:30 AM recipient local time",
        "signal_timing": "Within 24-48h of buying trigger for 3-5x response lift",
        "avoid": "Monday (flooded inbox), Friday afternoon (attention drops)",
    },
    "sequence": {
        "optimal_length": "4-7 emails",
        "first_email_reply_share": "58% of all replies",
        "smb_cadence_days": [0, 3, 7, 14],
        "enterprise_cadence": "7-10 touches over 45-60 days",
        "multi_channel": "No more than 50% email; mix LinkedIn + phone",
    },
    "benchmarks": {
        "avg_reply_rate": "3.43%",
        "top_10_pct_reply_rate": "10.7%+",
        "personalized_reply_rate": "15-30%",
        "generic_reply_rate": "< 3%",
        "signal_based_reply_rate": "18% average",
    },
    "personalization": {
        "ai_personalization_lift": "Reply rates from 9% to 21%",
        "video_personalization": "15-35% reply rate",
        "multi_channel_lift": "280%+ vs single channel",
        "buyer_response_to_relevant": "74% of B2B buyers respond to relevant emails",
    },
}


# Recommended cadences by segment (days between steps)
CADENCE_PRESETS: dict[str, list[int]] = {
    "smb_manufacturing": [0, 3, 7, 14],       # Default: tight then widen
    "enterprise": [0, 5, 12, 21, 35, 50],     # Longer gaps, more touches
    "warm_lead": [0, 2, 5, 10],               # Faster follow-up for warm leads
    "re_engagement": [0, 7, 21, 45],           # Wider gaps for dormant leads
    "event_follow_up": [0, 1, 4, 10],          # Very fast after events/triggers
}


def get_recommended_cadence(segment: str = "smb_manufacturing") -> list[int]:
    """Get the research-backed cadence for a given segment."""
    return CADENCE_PRESETS.get(segment, CADENCE_PRESETS["smb_manufacturing"])
