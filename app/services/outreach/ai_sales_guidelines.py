"""
Landline AI Sales Guidelines Module

Condensed sales psychology principles and outreach guidelines for the AI tactics engine.
Synthesized from: Cialdini (Influence), Voss (Never Split the Difference),
Ross (Predictable Revenue), Berman (Cold Email Manifesto), Hormozi ($100M Leads),
Rackham (SPIN Selling), Dixon & Adamson (The Challenger Sale),
Blount (Fanatical Prospecting), Weinberg (New Sales Simplified),
Godin (Permission Marketing).

Usage:
    from app.services.outreach.ai_sales_guidelines import (
        INDUSTRY_PROFILES,
        SUBJECT_LINE_FORMULAS,
        EMAIL_STRUCTURE,
        FOLLOW_UP_CADENCE,
        ANTI_SPAM_RULES,
        PERSONALIZATION_TOKENS,
        OUTREACH_PRINCIPLES,
        get_industry_profile,
        get_system_prompt_for_industry,
    )
"""

from typing import Any

# ---------------------------------------------------------------------------
# CORE OUTREACH PRINCIPLES
# These are injected into every AI-generated email as guiding constraints.
# ---------------------------------------------------------------------------

OUTREACH_PRINCIPLES = {
    "reciprocity": (
        "Lead with value before asking for anything. Offer a relevant insight, "
        "case study, benchmark, or resource in the first touch. The prospect "
        "should gain something from reading your email even if they never reply."
    ),
    "social_proof": (
        "Reference similar companies by name, industry, or size that have "
        "achieved specific, quantified results. Manufacturing is a small world — "
        "peers validate more than any sales claim."
    ),
    "authority": (
        "Demonstrate expertise through technical specificity — reference "
        "tolerances, cycle times, regulatory standards (ISO 9001, AS9100, SQF), "
        "and operational metrics. Never claim authority; show it."
    ),
    "scarcity": (
        "Only use genuine scarcity: limited pilot slots, seasonal timing, "
        "capacity constraints. Never fabricate urgency. Manufacturing buyers "
        "see through fake deadlines instantly."
    ),
    "tactical_empathy": (
        "Name the prospect's likely emotion or concern before they voice it. "
        "'You probably get a dozen emails a week from vendors who have never "
        "set foot on a shop floor.' This disarms skepticism."
    ),
    "challenger_teaching": (
        "Lead with an insight the prospect does NOT already have. Reframe "
        "their problem — don't just describe it. Show them a cost they haven't "
        "calculated or an approach they haven't considered."
    ),
    "referral_wedge": (
        "When unsure of the right contact, email above the target and ask: "
        "'I'm not sure if you're the right person — could you point me to "
        "whoever handles [specific function]?' Referral emails convert at "
        "2-3x cold emails."
    ),
    "permission_marketing": (
        "Every touchpoint must deliver value. If a follow-up email has nothing "
        "new to offer, do not send it. Earn the right to the next contact."
    ),
    "spin_questioning": (
        "Seed curiosity with Problem and Implication questions. Make the cost "
        "of the status quo feel larger than the cost of change. 'A 3% reject "
        "rate doesn't sound bad until you add up the rework, expedited "
        "shipping, and lost quotes.'"
    ),
    "micro_commitments": (
        "Ask for small yeses that build momentum. First email: reply. "
        "Second: 10-minute call. Third: deeper discovery. Never leap from "
        "cold email to full demo request."
    ),
}


# ---------------------------------------------------------------------------
# EMAIL STRUCTURE
# ---------------------------------------------------------------------------

EMAIL_STRUCTURE = {
    "max_body_words": 75,
    "ideal_body_words": 50,
    "max_sentences": 5,
    "paragraphs": "1-2 sentences each, separated by line breaks",
    "format": "plain text only — no HTML, images, logos, or formatting",
    "links": "none in body — signature only",
    "reading_level": "8th grade",
    "you_to_i_ratio": "2:1 — say 'you' twice for every 'I' or 'we'",
    "cta_count": 1,
    "template": (
        "HOOK (1 sentence): Specific observation about their company/situation\n"
        "PAIN/INSIGHT (1-2 sentences): Relevant problem or challenger reframe\n"
        "PROOF (1 sentence): Case study or social proof with specific metric\n"
        "CTA (1 sentence): Single, low-friction ask"
    ),
    "opening_patterns": {
        "observation": (
            "Reference a specific, researched fact about their company. "
            "Example: 'I noticed [Company] just added a second shift — "
            "that kind of growth usually puts pressure on QC consistency.'"
        ),
        "peer_comparison": (
            "Reference what similar companies are doing differently. "
            "Example: 'Two other [industry] shops your size told us their "
            "biggest bottleneck wasn't capacity — it was first-article time.'"
        ),
        "insight_reframe": (
            "Challenger approach — teach them something new. "
            "Example: 'Most CNC shops focus on machine utilization, but the "
            "shops winning the most quotes right now are winning on "
            "traceability documentation speed.'"
        ),
        "empathy_audit": (
            "Voss accusation audit — name their skepticism. "
            "Example: 'You probably get a dozen emails a week from people "
            "who have never set foot on a shop floor — I get it.'"
        ),
        "trigger_event": (
            "Reference real-time company news. "
            "Example: 'Saw that [Company] landed [customer] — congrats. "
            "That volume usually means [relevant challenge].'"
        ),
    },
    "pain_agitation_patterns": {
        "cost_amplification": (
            "Make the hidden costs visible. 'A 3% reject rate doesn't sound "
            "bad until you calculate the rework labor, the expedited shipping, "
            "and the quoting margin you're leaving on the table.'"
        ),
        "risk_amplification": (
            "Surface future risks. 'Right now it works because your team has "
            "the tribal knowledge. But what happens when Dave retires and takes "
            "30 years of setup knowledge with him?'"
        ),
        "competitive_pressure": (
            "Highlight competitive gaps. 'Your competitors are quoting with "
            "real-time SPC data attached. The RFQs you're losing — it might "
            "not be price.'"
        ),
    },
    "cta_patterns": {
        "micro_commitment": [
            "Worth a 10-minute conversation?",
            "Is this even on your radar right now?",
            "Would it be helpful if I sent over the case study?",
        ],
        "no_oriented": [
            "Would it be a terrible idea to show you what we found?",
            "Is this something you've completely ruled out?",
            "Have you given up on solving [pain point]?",
        ],
        "referral": [
            "Am I reaching the right person, or should I talk to someone else on your team?",
            "Who on your team handles [specific function]?",
            "Not sure if this is your area — could you point me the right way?",
        ],
    },
    "cta_blacklist": [
        "Let me know your thoughts",
        "I'd love to",
        "Please advise",
        "Looking forward to hearing from you",
        "When is a good time?",
    ],
}


# ---------------------------------------------------------------------------
# SUBJECT LINE FORMULAS
# Ranked by tier (1 = highest open rates). Each entry has a template,
# an example, and the psychological principle it leverages.
# ---------------------------------------------------------------------------

SUBJECT_LINE_FORMULAS = {
    "tier_1": [
        {
            "template": "Quick question",
            "example": "Quick question",
            "principle": "Curiosity gap, low threat (Berman)",
            "notes": "All-time top performer across industries",
        },
        {
            "template": "[Mutual connection] suggested I reach out",
            "example": "Mike Torres suggested I reach out",
            "principle": "Referral credibility (Ross)",
            "notes": "Only use with real referrals — never fabricate",
        },
        {
            "template": "[Specific metric] at [Company]",
            "example": "Scrap rates at Precision Plastics",
            "principle": "Specificity + proves research",
            "notes": "Must reference a real, relevant metric",
        },
        {
            "template": "Idea for [Company]",
            "example": "Idea for Midwest CNC",
            "principle": "Implies value, not a pitch (Weinberg)",
            "notes": "Works across all industries and roles",
        },
    ],
    "tier_2": [
        {
            "template": "[Company] + [what peer is doing]",
            "example": "Precision Plastics + what Apex is doing differently",
            "principle": "Social proof + competitive tension (Cialdini)",
        },
        {
            "template": "Saw [specific trigger event]",
            "example": "Saw the expansion into medical devices",
            "principle": "Relevance + proves attention (Godin)",
        },
        {
            "template": "[First name], quick thought on [topic]",
            "example": "Dave, quick thought on your tolerance issues",
            "principle": "Personal + specific",
        },
        {
            "template": "Question about [their process]",
            "example": "Question about your QC workflow",
            "principle": "Invites dialogue, not pitch (SPIN)",
        },
    ],
    "tier_3": [
        {
            "template": "Congratulations on [achievement]",
            "example": "Congrats on the AS9100 cert",
            "principle": "Liking (Cialdini)",
        },
        {
            "template": "[Number] [result] for [similar company]",
            "example": "12% scrap reduction for a shop like yours",
            "principle": "Social proof + specificity",
        },
        {
            "template": "Not sure if this is relevant",
            "example": "Not sure if this is relevant to [Company]",
            "principle": "Humility, low-threat (Ross)",
        },
    ],
    "blacklist": [
        "ALL CAPS subjects",
        "Excessive punctuation (!!!)",
        "Free, Guarantee, Act Now, Limited Time",
        "Following up (standalone)",
        "Re: or Fwd: when not a real reply/forward",
        "Subjects longer than 8 words",
        "Dear Sir/Madam",
        "Partnership opportunity",
        "Touching base",
    ],
}


# ---------------------------------------------------------------------------
# FOLLOW-UP CADENCE
# ---------------------------------------------------------------------------

FOLLOW_UP_CADENCE = {
    "sequence_length": 5,
    "total_days": 21,
    "touches": [
        {
            "touch": 1,
            "day": 1,
            "channel": "email",
            "purpose": "Opening — Compliment + Case Study + CTA (3C framework)",
            "template_guidance": (
                "Lead with a specific, researched compliment about the company. "
                "Follow with a one-sentence case study showing a quantified result "
                "with a similar company. Close with a single micro-commitment CTA."
            ),
        },
        {
            "touch": 2,
            "day": 3,
            "channel": "email",
            "purpose": "Value-add — new angle, different pain point, or insight",
            "template_guidance": (
                "Do NOT say 'following up.' Provide a different angle on their "
                "situation — a new data point, a different case study, or an "
                "insight relevant to their role. Must contain new value."
            ),
        },
        {
            "touch": 3,
            "day": 7,
            "channel": "email + LinkedIn",
            "purpose": "Social proof + soft exit signal",
            "template_guidance": (
                "Share a new social proof data point. Signal you'll respect "
                "their time: 'I'll assume timing isn't right if I don't hear "
                "back.' Connect on LinkedIn the same day (no sales pitch in "
                "the connection request)."
            ),
        },
        {
            "touch": 4,
            "day": 14,
            "channel": "email",
            "purpose": "New value — trigger event, resource, or industry data",
            "template_guidance": (
                "Reference a new trigger event if available, or provide a "
                "genuinely useful resource (benchmark, checklist, insight). "
                "Frame as: 'Thought this might be useful regardless of whether "
                "we ever talk.'"
            ),
        },
        {
            "touch": 5,
            "day": 21,
            "channel": "email",
            "purpose": "Breakup — graceful close with door open",
            "template_guidance": (
                "Acknowledge you've reached out multiple times. Respect their "
                "decision. Leave the door open: 'If [pain point] ever becomes "
                "urgent, I'm here.' Reference something specific about their "
                "business to show you still care."
            ),
        },
    ],
    "post_sequence": {
        "action": "Move to monthly nurture list",
        "frequency": "One value-add email per month",
        "content": "Industry insight, case study, or benchmark — no CTA beyond 'thought this might be useful'",
        "re_engage_after_days": 90,
        "re_engage_trigger": "Only re-sequence if a new trigger event occurs",
    },
    "timing": {
        "best_days": ["tuesday", "wednesday", "thursday"],
        "best_window": "9:30-11:30 AM in recipient's local timezone",
        "worst_times": ["Monday morning", "Friday afternoon"],
        "send_timezone": "recipient's local timezone, not sender's",
    },
    "rules": [
        "Each follow-up MUST contain new value — never 'just following up'",
        "Vary the angle and pain point across touches",
        "If prospect replies at any point, exit the sequence and respond personally",
        "If prospect unsubscribes or says 'not interested', stop immediately",
        "Never send more than 5 touches before moving to nurture",
    ],
}


# ---------------------------------------------------------------------------
# ANTI-SPAM RULES
# Hard constraints for the AI engine when generating email content.
# ---------------------------------------------------------------------------

ANTI_SPAM_RULES = {
    "content_rules": {
        "must": [
            "Write plain text only — no HTML, images, or formatting",
            "Keep total email under 100 words",
            "Personalize the first line — no identical openers across batch",
            "Vary email copy across sends — never send identical content to multiple recipients",
            "Include physical mailing address (in signature)",
            "Include clear unsubscribe mechanism",
        ],
        "must_not": [
            "Include links in the email body (signature only)",
            "Use images, logos, banners, or HTML formatting",
            "Use spam trigger words: free, guarantee, buy now, risk-free, act now, "
            "limited time, click here, dear sir/madam, no obligation, winner, "
            "congratulations (as a sales hook), urgent, order now",
            "Use ALL CAPS anywhere in the email",
            "Use excessive punctuation (!! or ???)",
            "Use misleading Re: or Fwd: in subject lines",
            "Attach files to cold emails",
            "Use URL shorteners (bit.ly, tinyurl, etc.)",
            "Send identical emails to multiple recipients in the same batch",
        ],
    },
    "sending_limits": {
        "max_cold_emails_per_mailbox_per_day": 50,
        "recommended_per_mailbox_per_day": 35,
        "spread_sends_across_hours": True,
        "send_during_business_hours_only": True,
        "max_bounce_rate_percent": 2.0,
        "max_spam_complaint_rate_percent": 0.1,
        "google_hard_ceiling_spam_rate_percent": 0.3,
    },
    "domain_rules": {
        "never_use_primary_domain_for_cold_outreach": True,
        "use_separate_outreach_domains": True,
        "register_domains_days_before_first_send": 30,
        "warm_up_weeks_minimum": 3,
        "warm_up_schedule": {
            "week_1": "5-10 emails/day to engaged contacts",
            "week_2": "15-25/day, mix of warm and first cold",
            "week_3": "30-50/day, monitor bounce and spam rates",
            "week_4_plus": "Gradually scale to target volume",
        },
    },
    "authentication": {
        "spf": "required — must pass for all sending domains",
        "dkim": "required — signing enabled on all outbound",
        "dmarc": "required — p=quarantine minimum (never p=none)",
    },
    "compliance": {
        "unsubscribe_processing": "within 24 hours (same day best practice)",
        "treat_not_interested_as_unsubscribe": True,
        "never_re_add_opted_out_contacts": True,
        "maintain_global_suppression_list": True,
        "physical_address_required": True,
        "no_deceptive_subject_lines": True,
        "no_deceptive_from_headers": True,
    },
}


# ---------------------------------------------------------------------------
# INDUSTRY PROFILES
# Complete outreach configuration for each manufacturing vertical.
# ---------------------------------------------------------------------------

INDUSTRY_PROFILES: dict[str, dict[str, Any]] = {
    "plastic_injection_molding": {
        "display_name": "Plastic Injection Molding",
        "emotional_triggers": {
            "effective": [
                "Fear of losing top customers due to quality failures or failed audits",
                "Competitive anxiety — smaller shops losing quotes to larger operations",
                "Efficiency pride — desire to run a world-class operation",
                "Regulatory pressure from medical, automotive, food-contact compliance",
                "Scrap rate and material cost obsession (resin is expensive)",
            ],
            "avoid": [
                "Talking down to their expertise — these are technical people with decades of experience",
                "Leading with software jargon instead of operational outcomes",
                "Implying their current process is broken — frame as 'better documented' or 'more visible'",
                "Generic manufacturing messaging that doesn't acknowledge injection molding specifics",
            ],
        },
        "decision_makers": [
            {
                "role": "Owner/President",
                "cares_about": "Margin, growth, customer retention, shop valuation",
                "email_approach": "Business outcomes, competitive positioning",
                "priority": "Tier 1 for shops under 50 employees",
            },
            {
                "role": "VP/Director of Quality",
                "cares_about": "Audit readiness, reject rates, Cpk, customer complaints",
                "email_approach": "Compliance, data visibility, process control",
                "priority": "Tier 1 for shops 50-200 employees",
            },
            {
                "role": "Plant Manager",
                "cares_about": "Uptime, throughput, labor efficiency, scrap",
                "email_approach": "Operational efficiency, real-time monitoring",
                "priority": "Tier 1 for multi-plant operations",
            },
            {
                "role": "Quality Manager",
                "cares_about": "Day-to-day inspection, NCRs, corrective actions",
                "email_approach": "Time savings, error reduction, documentation speed",
                "priority": "Influencer — good for referral approach",
            },
            {
                "role": "Process Engineer",
                "cares_about": "Scientific molding, parameter optimization, validation",
                "email_approach": "Technical capability, data accuracy",
                "priority": "Technical champion — nurture relationship",
            },
        ],
        "approach_by_size": {
            "under_50": "Email Owner/President directly — they make all decisions",
            "50_to_200": "Referral approach: email VP Quality or VP Operations, ask for the right contact",
            "over_200": "Multi-thread: email VP Quality AND Plant Manager with tailored messages",
        },
        "subject_lines": [
            "Quick question about {company_name}'s reject rate tracking",
            "How {case_study_company} cut scrap 14% in Q3",
            "Saw {company_name}'s new {product_line} — question",
            "Idea for {company_name}'s {certification} audit prep",
            "{company_name} + what's changing in {industry_segment} quality requirements",
        ],
        "sample_email": (
            "Subject: Quick question about {company_name}'s scrap tracking\n\n"
            "{first_name},\n\n"
            "Saw that {company_name} is running {observation} — nice operation.\n\n"
            "Curious — are you tracking cavity-level reject data in real time, or "
            "mostly catching issues downstream at QC? We helped {case_study_company} "
            "cut their scrap rate from 4.2% to 1.8% by moving inspection upstream.\n\n"
            "Worth a quick call to see if that's relevant to your shop?\n\n"
            "{signature}"
        ),
        "personalization_triggers": [
            "New machine purchases or facility expansion",
            "New customer wins, especially medical or automotive",
            "Job postings for quality roles (signals growing pains)",
            "Recent ISO/IATF certifications or recertifications",
            "Trade show attendance (NPE, Amerimold)",
            "Raw material price spikes (pressure on margin)",
            "Mold shop acquisitions or partnerships",
        ],
        "key_certifications": ["ISO 9001", "IATF 16949", "ISO 13485", "FDA 21 CFR Part 820"],
        "key_metrics": ["scrap rate", "reject rate", "Cpk/Ppk", "cycle time", "OEE", "first-pass yield"],
        "technical_vocabulary": [
            "cavity pressure", "scientific molding", "process validation",
            "IQ/OQ/PQ", "dimensional inspection", "GD&T", "mold qualification",
            "tonnage", "shot size", "cooling time", "gate seal",
        ],
    },

    "cnc_machining": {
        "display_name": "CNC Machining",
        "emotional_triggers": {
            "effective": [
                "Quote win rate anxiety — anything that helps win more RFQs",
                "Skilled labor shortage — reducing dependency on tribal knowledge",
                "Customer documentation demands, especially aerospace/defense",
                "Setup time obsession — every minute of downtime is lost revenue",
                "First-article inspection bottleneck — hours spent per FAI report",
            ],
            "avoid": [
                "Assuming all CNC shops are the same (job shops vs production vs prototype)",
                "Leading with automation unless they're already exploring it",
                "Ignoring the craftsman identity — machinists take deep pride in their work",
                "Using fabrication or stamping terminology for machining operations",
            ],
        },
        "decision_makers": [
            {
                "role": "Owner/President",
                "cares_about": "Revenue, customer diversification, shop valuation",
                "email_approach": "Business growth, quote win rates, competitive edge",
                "priority": "Tier 1 for shops under 100 employees",
            },
            {
                "role": "VP of Operations",
                "cares_about": "Throughput, machine utilization, scheduling",
                "email_approach": "Efficiency metrics, capacity optimization",
                "priority": "Tier 1 for mid-size operations",
            },
            {
                "role": "Quality Director",
                "cares_about": "AS9100/ISO compliance, FAI, customer scorecards",
                "email_approach": "Compliance automation, documentation speed",
                "priority": "Tier 1 for aerospace/defense shops",
            },
            {
                "role": "Shop Floor Supervisor",
                "cares_about": "Setup times, first-pass yield, daily throughput",
                "email_approach": "Practical time savings, ease of use",
                "priority": "Influencer — builds internal champion",
            },
            {
                "role": "Quoting/Estimating Manager",
                "cares_about": "Quote turnaround time, accuracy, win rate",
                "email_approach": "Speed and accuracy improvements",
                "priority": "Underrated decision-maker — often overlooked",
            },
        ],
        "approach_by_size": {
            "under_100": "Email Owner directly — most are owner-operators",
            "100_to_500": "Referral approach to VP Operations or Quality Director",
            "aerospace_defense": "Lead with Quality Director — compliance drives purchasing",
            "multi_thread": "Owner + Quality Director is the strongest combination",
        },
        "subject_lines": [
            "How {case_study_company} cut FAI time by 60%",
            "Quick question about {company_name}'s AS9100 documentation",
            "Idea for speeding up first-article at {company_name}",
            "{company_name} + the new DCMA inspection requirements",
            "Saw {company_name} added 5-axis — question about programming workflow",
        ],
        "sample_email": (
            "Subject: How {case_study_company} cut FAI time by 60%\n\n"
            "{first_name},\n\n"
            "Saw that {company_name} is doing a lot of {industry_segment} work — "
            "that documentation burden is no joke.\n\n"
            "Quick question: how long is your team spending on first-article "
            "inspection reports right now? One AS9100 shop we work with was "
            "spending 4 hours per FAI — they got it under 90 minutes.\n\n"
            "Not sure if that's a pain point for you, but worth a 10-minute "
            "call if it is?\n\n"
            "{signature}"
        ),
        "personalization_triggers": [
            "New machine acquisitions (5-axis, Swiss-type, mill-turn)",
            "AS9100 or ISO 13485 certification announcements",
            "ITAR registration or defense contract wins",
            "Hiring for programmers, quality inspectors, or machinists",
            "Trade show participation (IMTS, Eastec, Houstex)",
            "Customer industry shifts (adding medical or aerospace work)",
            "Geographic expansion or second facility",
        ],
        "key_certifications": ["AS9100", "ISO 9001", "ISO 13485", "ITAR", "Nadcap"],
        "key_metrics": ["FAI time", "setup time", "first-pass yield", "machine utilization", "quote win rate"],
        "technical_vocabulary": [
            "GD&T", "CMM", "first-article inspection", "ballooned drawing",
            "5-axis", "Swiss-type", "mill-turn", "surface finish", "Ra",
            "true position", "toolpath", "feeds and speeds", "work offset",
        ],
    },

    "metal_stamping": {
        "display_name": "Metal Stamping",
        "emotional_triggers": {
            "effective": [
                "Die maintenance and unplanned downtime fear — die failures are catastrophic",
                "Automotive customer pressure — PPAP, SPC, continuous improvement demands",
                "Material cost sensitivity — scrap reduction directly impacts margin",
                "Volume and speed pride — frame quality as enabling higher speeds, not slowing down",
                "EV transition opportunity — new components, new materials, new capabilities needed",
            ],
            "avoid": [
                "Confusing stamping with machining or fabrication — different industries",
                "Ignoring the tooling perspective — the die is everything",
                "Overlooking the toolroom/die shop as internal influencers",
                "Generic quality messaging that doesn't acknowledge high-volume realities",
            ],
        },
        "decision_makers": [
            {
                "role": "Owner/GM",
                "cares_about": "Margin per hit, die investment ROI, customer retention",
                "email_approach": "Business impact, cost per part",
                "priority": "Tier 1 for independent stampers",
            },
            {
                "role": "VP Quality",
                "cares_about": "PPAP compliance, SPC, customer scorecards, audit readiness",
                "email_approach": "Compliance and documentation",
                "priority": "Tier 1 for automotive stampers",
            },
            {
                "role": "Plant Manager",
                "cares_about": "Press uptime, parts per hour, die changeover time",
                "email_approach": "Operational efficiency, downtime reduction",
                "priority": "Tier 1 for multi-press operations",
            },
            {
                "role": "Toolroom Manager",
                "cares_about": "Die life, maintenance scheduling, root cause of failures",
                "email_approach": "Predictive insights, maintenance optimization",
                "priority": "Key influencer — often overlooked",
            },
            {
                "role": "Continuous Improvement Manager",
                "cares_about": "Kaizen, lean, waste reduction",
                "email_approach": "Data-driven improvement, measurable results",
                "priority": "Good champion for pilot programs",
            },
        ],
        "approach_by_size": {
            "under_100": "Owner/GM directly",
            "automotive_supplier": "VP Quality — they own the OEM compliance relationship",
            "multi_plant": "Plant Manager + VP Quality dual approach",
            "always": "Ask about the toolroom — unexpected champions live there",
        },
        "subject_lines": [
            "Quick question about {company_name}'s SPC on the press floor",
            "How {case_study_company} reduced die maintenance downtime 30%",
            "{company_name} + the new {oem_name} quality requirements",
            "Idea for {company_name}'s PPAP process",
            "Saw {company_name}'s expansion into EV components — question",
        ],
        "sample_email": (
            "Subject: Quick question about {company_name}'s die maintenance tracking\n\n"
            "{first_name},\n\n"
            "I noticed {company_name} is running progressive dies for "
            "{customer_industry} — that volume puts serious pressure on die life.\n\n"
            "Curious: is your toolroom tracking hits-to-failure data in real time, "
            "or mostly reacting when something goes wrong? One stamper we work "
            "with extended average die life by 22% just by moving from calendar-"
            "based to data-driven maintenance.\n\n"
            "Worth a 10-minute call to compare notes?\n\n"
            "{signature}"
        ),
        "personalization_triggers": [
            "New press installations (servo press upgrades = modernization)",
            "Automotive customer wins or contract renewals",
            "EV/electrification component manufacturing",
            "Die shop expansion or toolmaking investments",
            "Lean/CI initiatives or awards",
            "Material changes (AHSS, aluminum, new alloys)",
            "Trade events (METALFORM, FABTECH)",
        ],
        "key_certifications": ["IATF 16949", "ISO 9001", "ISO 14001"],
        "key_metrics": ["hits per hour", "die life (hits)", "scrap rate", "PPM", "OEE", "changeover time"],
        "technical_vocabulary": [
            "progressive die", "transfer die", "servo press", "tonnage",
            "strip layout", "blank holder", "draw ratio", "springback",
            "PPAP", "SPC", "control plan", "PFMEA", "die tryout",
        ],
    },

    "food_and_beverage": {
        "display_name": "Food & Beverage Manufacturing",
        "emotional_triggers": {
            "effective": [
                "Regulatory fear — FDA, USDA, SQF, FSMA scrutiny is constant",
                "Consumer safety anxiety — recalls are existential events",
                "Retailer audit pressure — Walmart, Costco, Kroger have their own standards",
                "Speed-to-market pressure — faster product launches, more SKUs",
                "Traceability demands — FSMA 204 requirements rolling out",
            ],
            "avoid": [
                "Lumping food and beverage carelessly — dairy vs brewery vs snack vs meat are different",
                "Ignoring the FSQA team — they are the real power center",
                "Being casual about food safety — this is life-and-death for consumers",
                "Using manufacturing jargon that doesn't translate to food production",
            ],
        },
        "decision_makers": [
            {
                "role": "VP/Director of Quality & Food Safety",
                "cares_about": "SQF/FSMA compliance, HACCP, audit readiness, recall prevention",
                "email_approach": "Compliance, risk reduction, documentation speed",
                "priority": "Tier 1 — primary decision maker for quality/safety solutions",
            },
            {
                "role": "Plant Manager/Director of Operations",
                "cares_about": "OEE, changeover time, yield, labor productivity",
                "email_approach": "Operational efficiency, real-time visibility",
                "priority": "Tier 1 for operations-led organizations",
            },
            {
                "role": "Director of R&D/Innovation",
                "cares_about": "NPD speed, formulation accuracy, scale-up success",
                "email_approach": "Speed, accuracy, data management",
                "priority": "Important for ingredient/formulation companies",
            },
            {
                "role": "VP of Supply Chain",
                "cares_about": "Traceability, supplier quality, inventory accuracy",
                "email_approach": "End-to-end visibility, compliance",
                "priority": "Growing influence due to FSMA 204",
            },
            {
                "role": "CEO/Owner",
                "cares_about": "Growth, customer retention, regulatory risk",
                "email_approach": "Business outcomes, risk mitigation",
                "priority": "Tier 1 for companies under 200 employees",
            },
        ],
        "approach_by_size": {
            "under_200": "CEO/Owner directly — they wear all hats",
            "quality_safety_solutions": "VP/Director of Quality & Food Safety — they own the budget and the pain",
            "operations_solutions": "Plant Manager or Director of Operations",
            "always": "Acknowledge the regulatory context — opening with SQF/FSMA/HACCP awareness signals credibility",
        },
        "subject_lines": [
            "Quick question about {company_name}'s SQF audit prep",
            "How {case_study_company} passed their GFSI audit first time",
            "{company_name} + the new FSMA 204 traceability requirements",
            "Idea for reducing {company_name}'s CCP documentation time",
            "Saw {company_name}'s new product launch — question about scale-up QC",
        ],
        "sample_email": (
            "Subject: Quick question about {company_name}'s FSMA 204 readiness\n\n"
            "{first_name},\n\n"
            "With the FSMA 204 traceability requirements rolling out, I've been "
            "hearing from a lot of {industry_segment} teams that linking lot-level "
            "data across receiving, production, and shipping is the hardest part.\n\n"
            "Is that on your radar yet? One {case_study_company} we work with went "
            "from 48-hour trace-back to under 4 hours — their last retailer audit "
            "was a non-event.\n\n"
            "Worth a 10-minute call to see if our approach is relevant for "
            "{company_name}?\n\n"
            "{signature}"
        ),
        "personalization_triggers": [
            "FDA warning letters or 483 observations (public — handle sensitively)",
            "New facility openings or production line additions",
            "Product launches or SKU expansion",
            "Organic, non-GMO, or clean-label certifications",
            "Retailer wins (Costco, Whole Foods, etc.)",
            "Sustainability initiatives or certifications",
            "SQF/BRC/FSSC recertification timelines",
            "FSMA 204 implementation deadlines",
            "Recall events in their product category (do NOT name competitor recalls)",
        ],
        "key_certifications": ["SQF", "BRC", "FSSC 22000", "ISO 22000", "HACCP", "FDA registered"],
        "key_metrics": ["trace-back time", "audit scores", "customer complaints", "CCP deviations", "OEE", "yield"],
        "technical_vocabulary": [
            "HACCP", "CCP", "prerequisite programs", "HARPC", "preventive controls",
            "lot traceability", "mock recall", "environmental monitoring",
            "allergen control", "sanitation", "CIP", "kill step", "critical limits",
        ],
    },
}


# ---------------------------------------------------------------------------
# PERSONALIZATION TOKENS
# Fields the AI engine should populate when generating outreach.
# ---------------------------------------------------------------------------

PERSONALIZATION_TOKENS = {
    "required": [
        "{company_name}",
        "{first_name}",
        "{role_title}",
        "{industry_vertical}",
        "{pain_point}",
    ],
    "strongly_recommended": [
        "{trigger_event}",
        "{case_study_company}",
        "{case_study_metric}",
        "{key_certification}",
    ],
    "optional": [
        "{company_size_bucket}",
        "{competitor_or_peer}",
        "{geographic_region}",
        "{observation}",
        "{industry_segment}",
        "{product_line}",
        "{customer_industry}",
        "{oem_name}",
    ],
    "hierarchy": (
        "Personalization effectiveness ranked: "
        "1) Trigger-based (2-3x response) — reference a recent event at the company. "
        "2) Research-based (2x) — reference their equipment, customers, certifications. "
        "3) Industry-based (1.5x) — reference industry trends or regulatory changes. "
        "4) Role-based (1.3x) — tailor pain points to their specific role. "
        "5) Name-only (baseline) — first name only is table stakes, not real personalization."
    ),
    "data_sources": [
        "Company website (capabilities, certifications, equipment, customer logos)",
        "LinkedIn company page (headcount, recent hires, growth trajectory)",
        "Google News (press releases, awards, expansions)",
        "Industry publications (Plastics Technology, Modern Machine Shop, Food Engineering)",
        "Trade show exhibitor/attendee lists",
        "Job boards (roles being hired reveal pain points)",
        "Government databases (FDA warning letters, OSHA citations)",
        "LinkedIn profile (job history, posts, conference speaking)",
    ],
}


# ---------------------------------------------------------------------------
# SYSTEM PROMPT BUILDER
# Generates a complete system prompt for the AI email-writing agent
# tailored to a specific industry vertical.
# ---------------------------------------------------------------------------

def get_industry_profile(industry_key: str) -> dict[str, Any]:
    """Retrieve the full industry profile for a given vertical.

    Args:
        industry_key: One of 'plastic_injection_molding', 'cnc_machining',
                      'metal_stamping', 'food_and_beverage'.

    Returns:
        The complete industry profile dictionary.

    Raises:
        ValueError: If the industry key is not recognized.
    """
    if industry_key not in INDUSTRY_PROFILES:
        valid = ", ".join(INDUSTRY_PROFILES.keys())
        raise ValueError(
            f"Unknown industry '{industry_key}'. Valid options: {valid}"
        )
    return INDUSTRY_PROFILES[industry_key]


def get_system_prompt_for_industry(industry_key: str) -> str:
    """Build a complete system prompt for the AI email-generation agent,
    tailored to a specific industry vertical.

    This prompt includes all relevant principles, constraints, structure
    guidelines, and industry-specific context needed to generate a single
    cold outreach email.

    Args:
        industry_key: One of 'plastic_injection_molding', 'cnc_machining',
                      'metal_stamping', 'food_and_beverage'.

    Returns:
        A formatted string suitable for use as a system prompt.
    """
    profile = get_industry_profile(industry_key)

    # Build principle summary
    principles_text = "\n".join(
        f"- **{k.replace('_', ' ').title()}:** {v}"
        for k, v in OUTREACH_PRINCIPLES.items()
    )

    # Build emotional triggers
    effective_triggers = "\n".join(
        f"  - {t}" for t in profile["emotional_triggers"]["effective"]
    )
    avoid_triggers = "\n".join(
        f"  - {t}" for t in profile["emotional_triggers"]["avoid"]
    )

    # Build decision maker table
    dm_lines = []
    for dm in profile["decision_makers"]:
        dm_lines.append(
            f"  - **{dm['role']}**: Cares about {dm['cares_about']}. "
            f"Approach: {dm['email_approach']}. Priority: {dm['priority']}."
        )
    dm_text = "\n".join(dm_lines)

    # Build anti-spam content rules
    must_rules = "\n".join(f"  - {r}" for r in ANTI_SPAM_RULES["content_rules"]["must"])
    must_not_rules = "\n".join(f"  - {r}" for r in ANTI_SPAM_RULES["content_rules"]["must_not"])

    # Build CTA options
    cta_options = []
    for category, examples in EMAIL_STRUCTURE["cta_patterns"].items():
        cta_options.append(f"  {category.replace('_', ' ').title()}:")
        for ex in examples:
            cta_options.append(f"    - \"{ex}\"")
    cta_text = "\n".join(cta_options)

    # Build subject line examples
    sl_examples = "\n".join(f"  - {sl}" for sl in profile["subject_lines"])

    # Build personalization triggers
    triggers = "\n".join(f"  - {t}" for t in profile["personalization_triggers"])

    # Build technical vocabulary
    vocab = ", ".join(profile["technical_vocabulary"])

    return f"""You are an expert B2B cold email writer for Landline, specializing in
{profile['display_name']} outreach. Generate emails that are concise, specific,
value-first, and technically credible.

## CORE PRINCIPLES
{principles_text}

## EMAIL STRUCTURE
- Maximum {EMAIL_STRUCTURE['max_body_words']} words (ideal: {EMAIL_STRUCTURE['ideal_body_words']})
- {EMAIL_STRUCTURE['format']}
- {EMAIL_STRUCTURE['you_to_i_ratio']}
- Exactly {EMAIL_STRUCTURE['cta_count']} CTA per email
- Structure: {EMAIL_STRUCTURE['template']}

## ANTI-SPAM RULES (HARD CONSTRAINTS)
MUST:
{must_rules}

MUST NOT:
{must_not_rules}

## INDUSTRY: {profile['display_name'].upper()}

### Emotional Triggers That Work:
{effective_triggers}

### What to Avoid:
{avoid_triggers}

### Decision Makers:
{dm_text}

### Subject Line Templates:
{sl_examples}

### CTA Options:
{cta_text}

### Personalization Triggers to Reference:
{triggers}

### Technical Vocabulary (use naturally, not forced):
{vocab}

### Key Certifications to Reference:
{', '.join(profile['key_certifications'])}

### Key Metrics to Reference:
{', '.join(profile['key_metrics'])}

## SAMPLE EMAIL
{profile['sample_email']}

## CRITICAL REMINDERS
1. Shorter is ALWAYS better. Cut ruthlessly.
2. Every sentence must earn its place. If it doesn't add value, delete it.
3. Write for a busy manufacturing professional checking email on their phone.
4. Prove you researched them — generic emails get deleted.
5. One CTA. One ask. One clear next step.
6. Never fabricate case studies, metrics, or referrals.
7. Never use spam trigger words or include links in the body.
"""
