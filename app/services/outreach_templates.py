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

Merge fields available (rendered by app.services.email_compose.render):
    {{first_name}} {{company_name}} {{title}} {{demo_link}}
"""

from __future__ import annotations

SEQUENCE_NAME = "Plastic Injection Molding — Deal Cracker"
SEQUENCE_DESCRIPTION = (
    "4-step emotionally-targeted cold sequence for plastic injection molders. "
    "Arc: personal stakes -> the real cost of scrap -> tribal-knowledge loss -> "
    "breakup + free 48-hour process pilot. Deliverability-hardened, CAN-SPAM "
    "compliant, no fabricated claims."
)

# Standard cadence (days to wait before sending each step). Non-decreasing so the
# pressure tapers, not escalates.
STEP_DELAYS = [0, 2, 5, 9]

# ---------------------------------------------------------------------------
# The four steps. Bodies are HTML so they render cleanly; the dispatcher adds
# the signature + unsubscribe footer and the text/plain alternative.
# ---------------------------------------------------------------------------

STEP_1_SUBJECT = "{{first_name}}, who takes the blame when cavity 4 goes bad?"
STEP_1_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>Every molder I talk to has one mold they nurse along — the one where a "
    "bad run means a frantic weekend, an angry customer call, and an air-freight "
    "bill that eats the whole job's margin.</p>"
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
    "<p>The pattern we see across the industry is simple: shops that catch drift "
    "in real time turn most of that scrap back into shipped parts. "
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
