# AI Cold Calling Guidelines

*Landline Sales Platform — AI System Rules for Cold Call Automation and Optimization*

---

## Purpose

This document defines the rules and logic that Landline's AI system uses to generate, prioritize, personalize, and optimize cold calling activities. It governs when the system triggers call notifications, how it ranks the call queue, how scripts are personalized, how outcomes are tracked and learned from, and how cold calls integrate with email sequences.

These guidelines are designed for two audiences: the engineering team implementing the AI logic, and the sales managers configuring and tuning the system for their teams.

---

## Part 1: When to Trigger a Cold Call Notification

### Signal-Based Triggers

The AI system should monitor engagement signals continuously and trigger call notifications when specific thresholds are met. Each trigger has a priority level (P1 = call immediately, P2 = call within 4 hours, P3 = call within 24 hours) and a confidence score that determines queue ranking.

### Email Engagement Signals

**Trigger 1: Multiple Email Opens (P1)**
- Condition: Prospect opens the same email 3+ times within a 24-hour period
- Rationale: Multiple opens indicate active interest — the prospect is re-reading, potentially sharing with colleagues, or evaluating the content
- Action: Push immediate call notification to assigned rep with context: "This prospect has opened your email [X] times in the last [Y] hours. Call now while interest is high."
- Confidence Score: 0.85

**Trigger 2: Link Click — Demo or Pricing Page (P1)**
- Condition: Prospect clicks a link in an email that leads to a demo request page, pricing page, or ROI calculator
- Rationale: Clicking through to high-intent pages signals active buying consideration
- Action: Push immediate call notification with context: "This prospect just clicked through to the [demo/pricing/ROI] page from your email. Call within the next 30 minutes."
- Confidence Score: 0.90

**Trigger 3: Link Click — Content (P2)**
- Condition: Prospect clicks a link to a case study, whitepaper, blog post, or video
- Rationale: Content engagement indicates interest but is lower intent than pricing/demo pages
- Action: Queue call for next available calling block with context: "This prospect engaged with your [content type] about [topic]. Reference it in your opening."
- Confidence Score: 0.65

**Trigger 4: Email Reply — Positive or Neutral (P1)**
- Condition: Prospect replies to an email with a positive or neutral response (not an unsubscribe or negative reply)
- Rationale: Any reply is a high-value engagement signal
- Action: Push immediate notification. The AI should analyze reply sentiment and categorize as: interested, question, referral, or timing objection. Provide suggested response and call script based on category.
- Confidence Score: 0.95

**Trigger 5: Email Reply — Objection or "Not Now" (P2)**
- Condition: Prospect replies with a timing objection ("not right now," "maybe next quarter," "too busy")
- Rationale: A timing objection is not a disqualification — it's a signal to maintain contact
- Action: Schedule follow-up call for the timeframe mentioned (e.g., "next quarter" = 90 days). Add to nurture sequence in the meantime.
- Confidence Score: 0.50

**Trigger 6: Email Forward Detection (P1)**
- Condition: Email tracking detects that the prospect forwarded the email to another recipient (detected through additional unique opens from different IPs/devices within the same organization)
- Rationale: Forwarding suggests the prospect is bringing others into the evaluation — a strong buying signal
- Action: Push immediate call notification with context: "This prospect appears to have forwarded your email internally. They may be socializing the idea with colleagues."
- Confidence Score: 0.88

### Website Engagement Signals

**Trigger 7: Website Visit — High-Intent Pages (P1)**
- Condition: Known prospect visits Landline website and views pricing, demo, case study, or comparison pages
- Rationale: Active research on your website indicates buying intent
- Action: Push immediate call notification with context: "This prospect is currently on the Landline website viewing [specific page]."
- Confidence Score: 0.85

**Trigger 8: Website Visit — Return Visitor (P2)**
- Condition: Known prospect returns to the Landline website after 7+ days of no activity
- Rationale: Return visits suggest renewed interest, possibly triggered by an internal conversation or a competitor evaluation
- Action: Queue call for next available block with context: "This prospect returned to the website after [X] days. Something may have reignited their interest."
- Confidence Score: 0.70

**Trigger 9: Website Visit — Multiple Pages in One Session (P2)**
- Condition: Known prospect views 3+ pages in a single website session
- Rationale: Multi-page browsing indicates active research and evaluation
- Action: Queue call for next available block with context: "This prospect viewed [X] pages on the website including [list of pages]. They're actively researching."
- Confidence Score: 0.75

### External Trigger Events

**Trigger 10: Leadership Change at Target Account (P3)**
- Condition: AI detects (via LinkedIn monitoring, news feeds, or data enrichment) that a target account has a new VP of Quality, VP of Operations, Plant Manager, or similar role
- Rationale: New leaders in their first 90 days are the strongest buying window in B2B sales
- Action: Queue call within 1 week of detection. Generate a personalized outreach strategy for the new leader, including research on their background, previous company, and likely priorities.
- Confidence Score: 0.60

**Trigger 11: Quality Incident or Recall (P2)**
- Condition: AI detects (via news monitoring) that a target account has experienced a quality recall, FDA warning letter, or public quality incident
- Rationale: Quality incidents create urgent demand for better quality management — but outreach must be empathetic, not opportunistic
- Action: Queue call within 48 hours. Generate a carefully worded script that leads with empathy and industry understanding, not product pitching. Flag this for manager review before outreach.
- Confidence Score: 0.70 (but requires sensitivity review)

**Trigger 12: Expansion or Growth Event (P3)**
- Condition: AI detects that a target account is opening a new plant, adding production lines, hiring significantly, or announcing capacity expansion
- Rationale: Growth creates new quality management needs that existing systems may not handle
- Action: Queue call within 1 week. Generate outreach that references the growth and positions Landline as a way to scale quality management alongside production capacity.
- Confidence Score: 0.55

**Trigger 13: Competitor Evaluation Signal (P1)**
- Condition: Intent data indicates that a target account is researching competitor solutions (via third-party intent data providers like Bombora or 6sense)
- Rationale: If they're evaluating competitors, they have an active quality management need — you need to be part of the conversation
- Action: Push immediate call notification with context: "This account appears to be evaluating [competitor]. Engage immediately to ensure Landline is part of their evaluation."
- Confidence Score: 0.80

### Cadence-Based Triggers

**Trigger 14: Post-Email Sequence Call Step (P2)**
- Condition: The email sequence cadence schedule calls for a phone touch (e.g., Day 2, Day 8, Day 15 of the cadence)
- Rationale: Scheduled cadence calls maintain consistent multi-channel pressure
- Action: Queue call according to cadence schedule. Provide script context based on which email the prospect has or hasn't engaged with.
- Confidence Score: 0.40 (baseline, adjusted upward if email engagement signals are present)

**Trigger 15: Cadence Breakup Proximity (P2)**
- Condition: Prospect is within 2 days of receiving the "breakup email" (final email in the sequence) with no engagement
- Rationale: A phone call before the breakup email can sometimes salvage an otherwise unresponsive prospect
- Action: Queue a "last chance" call with a direct, honest script: "I've reached out a few times and haven't connected. Before I close your file, I wanted to give you one more chance to tell me if reducing scrap rates and improving quality is on your radar."
- Confidence Score: 0.35

---

## Part 2: How to Prioritize the Call Queue

### Queue Ranking Algorithm

The call queue should be ranked using a composite score that considers signal strength, recency, account value, and rep workload. The formula:

```
Queue Priority Score = (Signal Confidence × Signal Recency Weight × Account Value Score) + Engagement Velocity Bonus
```

### Component Definitions

**Signal Confidence (0.0 - 1.0):** The confidence score assigned to each trigger event (defined in Part 1). Higher confidence signals rank higher.

**Signal Recency Weight (0.0 - 1.0):**
- Signal detected within the last 30 minutes: 1.0
- Signal detected 30 minutes to 2 hours ago: 0.85
- Signal detected 2-4 hours ago: 0.65
- Signal detected 4-8 hours ago: 0.45
- Signal detected 8-24 hours ago: 0.30
- Signal detected 24-48 hours ago: 0.15
- Signal detected 48+ hours ago: 0.05

**Account Value Score (0.0 - 1.0):**
- Tier 1 target account: 1.0
- Tier 2 target account: 0.75
- Tier 3 target account: 0.50
- Non-target account with ICP fit: 0.35
- Non-target account without ICP fit: 0.10

**Engagement Velocity Bonus (0.0 - 0.3):**
Applied when a prospect shows accelerating engagement — multiple signals within a short window:
- 2 signals within 24 hours: +0.10
- 3 signals within 24 hours: +0.20
- 4+ signals within 24 hours: +0.30

### Queue Management Rules

**Rule 1: Maximum Queue Size.** No rep should have more than 20 calls in their active queue at any time. If the queue exceeds 20, the lowest-priority calls roll to the next available calling block or are reassigned to another rep.

**Rule 2: Aging.** Calls that remain in the queue for more than 4 hours without being attempted drop in priority by 50%. Calls older than 24 hours are automatically moved to the next day's queue unless they are P1 priority.

**Rule 3: Deduplication.** If multiple signals fire for the same prospect within a 2-hour window, consolidate them into a single call notification with all signals listed. Do not create multiple queue entries for the same prospect.

**Rule 4: Cooling Period.** After a call is completed (regardless of outcome), the prospect enters a cooling period before another call can be triggered:
- Live conversation: 3-day cooling period (unless the prospect specifically requested a callback sooner)
- Voicemail left: 2-day cooling period
- No answer, no voicemail: 1-day cooling period

**Rule 5: Timezone Awareness.** Only queue calls during the prospect's local business hours (7:30 AM - 6:00 PM in the prospect's timezone). Calls triggered outside these hours queue for the next available window.

**Rule 6: Optimal Time Matching.** When scheduling calls for future windows, prefer the time slots with highest historical connect rates for the prospect's industry and role:
- Manufacturing plant managers: 7:30-9:00 AM or 4:00-5:00 PM
- Quality managers: 10:00-11:30 AM
- C-suite: 8:00-9:00 AM or 4:30-5:30 PM

---

## Part 3: Script Personalization Rules

### Personalization Levels

The AI system generates call scripts at three levels of personalization, depending on the data available and the account tier.

**Level 1: Industry Personalization (Minimum)**
Applied to: All prospects in the queue
Data required: Industry sub-sector, company size
Personalization elements:
- Industry-specific pain points (use scripts from the Industry Scripts document)
- Relevant industry benchmarks and statistics
- Industry-appropriate terminology and examples

**Level 2: Company Personalization (Standard)**
Applied to: Tier 2 and Tier 1 accounts, or any prospect with significant engagement signals
Data required: Industry + company news, recent events, technologies in use, competitive landscape
Personalization elements:
- Everything in Level 1, plus:
- Reference to a specific company event (expansion, hiring, award, quality incident)
- Mention of their likely current technology stack
- Customized value proposition based on estimated company size and production volume
- Relevant case study from a company in their specific sub-sector and size range

**Level 3: Contact Personalization (Premium)**
Applied to: Tier 1 accounts and high-signal prospects
Data required: Everything in Level 2 + contact's LinkedIn profile, role, tenure, previous company, shared connections, published content
Personalization elements:
- Everything in Level 2, plus:
- Reference to the contact's specific role and likely priorities
- Mention of their career background or a piece of content they've published
- Connection through a shared contact, group, or event
- Tailored value proposition specific to their role (plant manager vs. quality manager vs. CFO)

### Script Generation Rules

**Rule 1: Never Sound Scripted.** The AI generates talking points and key phrases, not word-for-word scripts. Reps should internalize the key messages and deliver them conversationally.

**Rule 2: Lead with Relevance.** The opening must reference something specific to the prospect's situation within the first 15 seconds. Generic openers ("I'm calling from Landline, we do AI quality management") are never acceptable.

**Rule 3: Include a Bridge.** Every script must include a "because" statement (Jeb Blount's framework) that connects Landline's capabilities to a specific, plausible pain point for the prospect.

**Rule 4: Ask, Don't Tell.** Scripts should include at least 2 qualifying questions before any value proposition is delivered. Discovery comes before prescription.

**Rule 5: Provide Escape Valves.** Scripts should include language that gives the prospect a graceful way to decline without feeling pressured: "I'm not sure if this is relevant to your situation, but..." or "If this isn't a priority right now, I completely understand."

**Rule 6: Include Signal Context.** When a call is triggered by a specific engagement signal, the script should reference it naturally:
- Email engagement: "I sent you some information about [topic] earlier this week and wanted to follow up..."
- Website visit: "I noticed some interest from your team in [topic area] and wanted to reach out..."
- Trigger event: "I saw the news about [event] and wanted to see how that's affecting your quality operations..."

**Rule 7: Match Tone to Industry.** Manufacturing communication tends to be direct, practical, and results-oriented. The AI should avoid corporate jargon, buzzwords, and overly polished language. Use plain, straightforward language that a plant manager would use.

### Dynamic Script Elements

The AI should dynamically insert the following elements based on available data:

- **Company name and contact name** (always)
- **Industry-specific pain point** (always — selected from the relevant industry script)
- **Relevant benchmark statistic** (when available — e.g., "injection molding companies typically see scrap rates of 3-5%")
- **Similar customer reference** (when available — e.g., "we recently helped a 150-person stamping operation...")
- **Trigger event reference** (when a trigger event is the call reason)
- **Previous interaction summary** (when the prospect has engaged before — "when we spoke last month, you mentioned...")
- **Time-appropriate greeting** (early morning: "I know it's early, I'll keep this brief"; late afternoon: "I know you're wrapping up your day")

---

## Part 4: Call Outcome Tracking and Learning

### Outcome Categories

Every call must be logged with one of the following outcome categories. The AI system uses these outcomes to learn and optimize future call strategies.

**Positive Outcomes:**
- `meeting_set` — Discovery meeting or demo scheduled
- `callback_requested` — Prospect asked for a callback at a specific time
- `referral_given` — Prospect referred to another contact (capture referral name, title, and contact info)
- `information_requested` — Prospect asked for specific information to be sent (capture what they want)
- `interest_expressed` — Prospect expressed general interest but no concrete next step

**Neutral Outcomes:**
- `voicemail_left` — Left a voicemail message
- `no_answer_no_voicemail` — Call not answered, voicemail not available
- `gatekeeper_no_transfer` — Spoke with gatekeeper, not transferred to target
- `gatekeeper_info_gathered` — Spoke with gatekeeper, gathered useful intelligence

**Negative Outcomes:**
- `not_interested_timing` — Not interested now, may be interested later (capture expected timeline)
- `not_interested_fit` — Not a good fit for Landline (capture reason for disqualification)
- `competitor_in_place` — Already using a competitive solution (capture competitor name)
- `do_not_call` — Explicitly requested no further calls
- `wrong_contact` — Not the right person (capture correct contact if mentioned)
- `wrong_number` — Phone number is incorrect
- `left_company` — Contact no longer works at the target company

### Data Captured Per Call

For every call, the system should capture:

- Timestamp (start and end)
- Duration
- Outcome category (from above)
- Signal that triggered the call (from Part 1)
- Script personalization level used (from Part 3)
- Prospect's stated pain points (free text)
- Objections encountered (categorized)
- Next step scheduled (if any)
- Rep notes (free text)
- Call quality score (optional — rep self-assessment or manager review)

### Learning and Optimization

The AI system should use call outcome data to continuously improve its performance across four dimensions:

**Signal Quality Optimization:**
Track the conversion rate (calls resulting in meetings) for each trigger signal. Over time, adjust the confidence scores in Part 1 based on actual performance. If a signal that was assigned 0.85 confidence only converts at 5% (below the average), reduce its confidence score. If a signal assigned 0.60 converts at 20%, increase it.

**Timing Optimization:**
Track connect rates and conversion rates by day of week, time of day, industry, and role. Use this data to refine the optimal calling windows for each segment. If data shows that CNC shop owners connect at 6:45 AM at 3x the rate of 10:00 AM calls, adjust the queue algorithm to prioritize early morning slots for that segment.

**Script Optimization:**
Track which opening hooks, qualifying questions, and value propositions correlate with positive outcomes. A/B test different script variations:
- Test opening hook A vs. opening hook B for injection molding prospects
- Test leading with ROI data vs. leading with a case study for plant managers
- Test asking 2 qualifying questions vs. 4 qualifying questions before pitching

Surface the winning variations and update the script generation rules accordingly.

**Objection Pattern Analysis:**
Track which objections are most common by industry, company size, and role. Identify patterns:
- If 60% of CNC shop owners object with "my machinists already know when something's off," ensure the script proactively addresses this before the prospect raises it
- If "budget" is the #1 objection from companies under $20M revenue, lead with ROI and payback period data for smaller companies

### Reporting and Dashboards

The AI system should generate the following reports:

**Daily Call Report (Per Rep):**
- Total calls attempted
- Connect rate
- Meetings set
- Top signals that triggered calls
- Common objections encountered

**Weekly Call Performance (Team Level):**
- Total calls, connects, meetings by rep
- Conversion rates by trigger signal
- Best performing call times
- Most common objections and trending patterns
- Pipeline created from cold calling activities

**Monthly Optimization Report:**
- Signal confidence score adjustments (recommended)
- Script variation A/B test results
- Timing optimization recommendations
- Industry-level conversion rate trends
- ROI of AI-triggered calls vs. manually initiated calls

---

## Part 5: Integration with Email Sequences

### The Call-Email Coordination Engine

Cold calls and email sequences should operate as a unified system, not as independent channels. The AI system manages the coordination between these channels to maximize engagement.

### Core Integration Rules

**Rule 1: The Post-Open Call**
When a prospect opens an email for the 2nd or 3rd time within 24 hours, trigger a call notification within 30 minutes. The call script should reference the email content: "I sent you some information about [topic] and wanted to make sure you had a chance to ask any questions."

**Rule 2: The Post-Click Call**
When a prospect clicks a link in an email, trigger a call notification based on the link destination:
- Demo/pricing page click: Call within 15 minutes (P1)
- Case study/content click: Call within 4 hours (P2)
- General website link: Call within 24 hours (P3)

**Rule 3: The Pre-Breakup Call**
Two days before the "breakup email" (final email in the sequence) is scheduled, queue a phone call. This gives the rep one last chance for a live conversation before the sequence ends.

**Rule 4: The Post-Reply Call**
When a prospect replies to any email in the sequence:
- Positive/interested reply: Call within 1 hour
- Question reply: Call within 2 hours with the answer prepared
- Objection reply: Call within 4 hours with objection handling prepared
- "Not now" reply: Schedule a follow-up call for the date mentioned, add to nurture in the meantime

**Rule 5: Sequence Pause on Live Conversation**
When a rep has a live conversation with a prospect, automatically pause the email sequence for 48 hours. The rep's notes from the call should inform what email (if any) goes next. Options:
- Prospect agreed to a meeting → Pause sequence, send calendar invite, send pre-meeting materials
- Prospect said "not now" → Resume sequence at the next scheduled touchpoint
- Prospect requested specific information → Pause sequence, send requested info, resume 3 days later
- Prospect said "not interested" → End sequence, move to long-term nurture or disqualify

**Rule 6: Voicemail-Email Pairing (The Triple)**
When a rep leaves a voicemail, the AI should immediately generate and queue a follow-up email for the rep to send within 2 minutes. The email should:
- Subject line: "Just left you a voicemail" or "Following up on my message"
- Reference the voicemail content briefly
- Include the key value proposition in written form
- Provide a one-click scheduling link for a meeting

**Rule 7: No Channel Collision**
The AI must prevent sending an email and triggering a call notification within the same 2-hour window (except for the intentional Triple pattern). If an email is scheduled to send at 10:00 AM, do not queue a cadence-based call for 10:00-12:00 PM. Either send the email and wait for engagement signals to trigger a call, or delay the email and call first.

### Cadence Flow with Call Integration

Here is how the standard 24-day manufacturing cadence integrates calls with emails:

```
Day 1:  Email #1 (Insight)
Day 2:  Phone Call #1 → [If voicemail] → Auto-email (Triple)
Day 3:  [Monitor for email engagement signals → trigger call if detected]
Day 4:  LinkedIn Connection
Day 5:  [Monitor signals]
Day 6:  Email #2 (Case Study)
Day 7:  [Monitor for email engagement signals → trigger call if detected]
Day 8:  Phone Call #2 → [If voicemail] → Auto-email
Day 9-11: [Monitor signals, LinkedIn engagement]
Day 12: Email #3 (Value-Add)
Day 13: [Monitor signals]
Day 14: Video Message
Day 15: [Monitor signals → trigger call if detected]
Day 16: Phone Call #3 → Auto-email
Day 17-19: [Monitor signals, LinkedIn DM on Day 18]
Day 20: Email #4 (Micro Case Study)
Day 21: [Monitor signals]
Day 22: Phone Call #4 (Persistence call)
Day 23: [Final monitoring window]
Day 24: Email #5 (Breakup)
Day 25: [If breakup email opened 3+ times → trigger final call]
```

### Signal-Triggered Cadence Acceleration

When high-priority engagement signals are detected, the cadence should accelerate:

**Acceleration Level 1: Moderate Interest**
Trigger: 2+ email opens OR 1 link click on non-pricing content
Action: Move the next phone call up by 1-2 days

**Acceleration Level 2: High Interest**
Trigger: 3+ email opens within 24 hours OR click on pricing/demo page
Action: Skip to the next phone call immediately, regardless of cadence position

**Acceleration Level 3: Very High Interest**
Trigger: Email forward detected OR multiple stakeholders from the same company engaging
Action: Immediately trigger a call + escalate to AE for multi-threaded outreach strategy

### Sequence Exit Conditions

The AI should automatically exit a prospect from the cold calling sequence under the following conditions:

- Meeting is set (move to pre-meeting preparation workflow)
- Prospect explicitly requests no further contact (add to do-not-call list)
- Prospect is disqualified (not a fit for Landline)
- 24-day cadence is completed with no engagement (move to long-term nurture)
- Prospect becomes a customer through another channel (move to customer onboarding)

---

## Part 6: System Configuration and Tuning

### Configurable Parameters

Sales managers should be able to adjust the following parameters without engineering involvement:

**Signal Thresholds:**
- Number of email opens to trigger a call (default: 3)
- Time window for email opens (default: 24 hours)
- Website pages viewed to trigger a call (default: 3 pages in one session)
- Return visitor dormancy period (default: 7 days)

**Queue Settings:**
- Maximum queue size per rep (default: 20)
- P1 response window (default: 30 minutes)
- P2 response window (default: 4 hours)
- P3 response window (default: 24 hours)
- Cooling period after live conversation (default: 3 days)
- Cooling period after voicemail (default: 2 days)
- Cooling period after no-answer (default: 1 day)

**Cadence Settings:**
- Total cadence length (default: 24 days)
- Number of phone touches in cadence (default: 4)
- Number of email touches in cadence (default: 5)
- Breakup email timing (default: Day 24)
- Pre-breakup call timing (default: Day 22)

**Personalization Settings:**
- Minimum personalization level (default: Level 1 — Industry)
- Auto-upgrade to Level 2 for accounts with engagement (default: on)
- Auto-upgrade to Level 3 for Tier 1 accounts (default: on)

### A/B Testing Framework

The AI system should support structured A/B testing of the following elements:

**Testable Elements:**
- Opening hooks (which opening gets the highest connect-to-conversation rate)
- Qualifying questions (which questions most effectively surface pain)
- Value propositions (which framing drives the most meetings)
- Call timing (which hours and days produce the highest connect rates)
- Voicemail scripts (which voicemails generate the most callbacks)
- Cadence structure (which day/channel combinations produce the highest engagement)

**Test Methodology:**
- Minimum sample size: 50 calls per variation before drawing conclusions
- Random assignment: Prospects should be randomly assigned to test groups, balanced by industry and account tier
- Statistical significance: Require 95% confidence before declaring a winner
- Test duration: Run each test for a minimum of 2 weeks to account for day-of-week and time-of-day variations

### Data Privacy and Compliance

**Do Not Call Compliance:**
- The system must maintain a do-not-call list and automatically exclude these contacts from all call triggers
- Prospects who request removal must be removed within 24 hours
- The system should check against the National Do Not Call Registry for consumer numbers (though most B2B numbers are exempt)

**Recording and Consent:**
- If call recording is enabled, the system must prompt the rep to obtain consent according to the applicable state law (one-party vs. two-party consent states)
- Call recordings should be stored securely and retained according to the company's data retention policy

**Data Handling:**
- All prospect data, call outcomes, and engagement signals should be stored in accordance with applicable data protection regulations
- Prospects have the right to request access to and deletion of their personal data
- Data used for AI training and optimization should be anonymized when possible

---

## Appendix A: Signal-to-Action Quick Reference

| Signal | Priority | Response Window | Action |
|---|---|---|---|
| 3+ email opens in 24h | P1 | 30 minutes | Call immediately |
| Demo/pricing page click | P1 | 15 minutes | Call immediately |
| Email reply (positive) | P1 | 1 hour | Call with response prepared |
| Email forward detected | P1 | 30 minutes | Call + escalate for multi-thread |
| Competitor evaluation signal | P1 | 2 hours | Call + competitive positioning |
| Content link click | P2 | 4 hours | Call in next block |
| Return website visitor | P2 | 4 hours | Call in next block |
| Multi-page website session | P2 | 4 hours | Call in next block |
| Quality incident/recall | P2 | 48 hours | Call with sensitivity review |
| Cadence-scheduled call step | P2 | Same day | Call per cadence schedule |
| Email reply (objection) | P2 | 4 hours | Call with objection prep |
| Leadership change | P3 | 1 week | New-leader outreach sequence |
| Expansion/growth event | P3 | 1 week | Growth-positioned outreach |
| Job posting for quality roles | P3 | 1 week | Hiring-referenced outreach |

## Appendix B: Queue Priority Score Calculation Example

**Scenario:** A quality manager at a Tier 1 injection molding company opened an email 4 times in the last hour and clicked through to the pricing page.

Calculation:
- Signal 1 (multiple opens): Confidence = 0.85
- Signal 2 (pricing click): Confidence = 0.90
- Use highest signal confidence: 0.90
- Recency (within 30 minutes): 1.0
- Account value (Tier 1): 1.0
- Engagement velocity (2 signals within 24h): +0.10

**Queue Priority Score = (0.90 × 1.0 × 1.0) + 0.10 = 1.00**

This prospect would be at or near the top of the call queue.

**Comparison Scenario:** A production manager at a Tier 3 account opened an email once 6 hours ago.

Calculation:
- Signal (single open, below threshold): No trigger — this prospect does not enter the call queue from this signal alone. The cadence-scheduled call step (if applicable) would have a baseline confidence of 0.40.
- If cadence-scheduled: (0.40 × 0.45 × 0.50) + 0.00 = 0.09

This prospect would rank near the bottom of the queue.

## Appendix C: Glossary

- **Cadence:** A structured sequence of multi-channel touchpoints designed to engage a prospect over a defined period
- **Combo/Triple:** A tightly sequenced phone call + voicemail + email executed within 2-5 minutes (Tony Hughes framework)
- **Confidence Score:** A numeric value (0.0-1.0) representing the system's confidence that a given signal indicates buying intent
- **Cooling Period:** The minimum time between call attempts to the same prospect
- **Engagement Velocity:** The rate at which a prospect is accumulating engagement signals — accelerating engagement is a strong buying indicator
- **ICP:** Ideal Customer Profile — the characteristics of companies most likely to buy and succeed with Landline
- **P1/P2/P3:** Priority levels for call triggers (P1 = immediate, P2 = within hours, P3 = within days)
- **PVP:** Possible Value Proposition — a hypothesis about the value you might bring, framed as a question (Art Sobczak framework)
- **Signal:** Any observable action, behavior, or event that suggests a prospect is entering or progressing through a buying cycle
- **Triple:** Phone call + voicemail + email executed within 2 minutes (Tony Hughes framework)
- **Trigger Event:** An organizational change or incident that creates a buying window
