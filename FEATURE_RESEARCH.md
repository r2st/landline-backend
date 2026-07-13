# Landline AI-Driven CRM — Feature Research & Roadmap

**Date**: July 13, 2026
**Author**: Claude (research phase — nothing implemented)
**Scope**: Evolving Landline from a B2B sales tool into a full AI-driven CRM for lead generation

---

## Current State Assessment

Landline already has a strong foundation across 34 API routers, 27 database models, 39 service modules, and 13 Celery Beat tasks. The platform covers company/contact management, multi-channel outreach (email, phone, LinkedIn), AI-generated content (emails, scripts, tactics), lead scoring, deal pipeline, client health tracking, email sentiment analysis, news monitoring, and a sales knowledge base.

**Existing AI integrations** (all via Anthropic Claude `claude-sonnet-4-6`):

- Email generation, improvement, and follow-ups (12 template types, industry-aware demo links)
- Lead scoring (rule-based composite + AI evaluation)
- Reply analysis with intent classification, sentiment scoring, and auto-reply
- Cold-call script generation with 7 sales frameworks and role-based personalization
- Company analysis with outreach strategy
- Multi-touch campaign planning across channels
- LinkedIn messages, referral drafts, event outreach
- News monitoring with AI relevance scoring
- Business card OCR via Claude Vision
- Email conversation simulation for testing
- Investor outreach generation

**Existing automations** (Celery Beat):

- Outreach queue processing (every 5 min)
- Smart reply — reads Gmail, classifies, drafts responses (every 10 min)
- Auto-enrichment via Apollo (hourly)
- Auto-scoring of unscored leads (every 30 min)
- News monitoring (every 6 hours)
- Stale lead follow-up generation (every 4 hours)
- Morning call queue generation with AI script pre-warming (daily 8 AM)
- Hot lead detection from engagement signals (every 30 min)

**External integrations**: Apollo.io (enrichment), Gmail OAuth (multi-account with rotation), SMTP, Amazon SES, Google News RSS, ThomasNet scraper, Census Bureau API.

The gap isn't basic CRM functionality — it's *intelligence depth*. The current AI features are mostly one-shot: generate an email, score a lead, analyze a reply. A true AI-driven CRM needs continuous learning, predictive capabilities, autonomous decision-making, and closed-loop optimization. The features below address that gap.

---

## Phase 1 — High-Impact Enhancements (builds on existing infrastructure)

These features extend what Landline already has. They require no new major infrastructure — just deeper AI integration into existing workflows.

---

### 1.1 Predictive Pipeline & Revenue Forecasting

**What it does**: Analyzes historical deal data (stages, velocity, conversion rates) combined with engagement signals to predict close probability per deal, forecast monthly/quarterly revenue, and flag deals that are stalling or at risk.

**Why it matters for lead gen**: Sales teams waste time on deals that won't close. Predictive pipeline lets reps focus effort where it counts and gives managers accurate forecasts instead of gut feelings. Stall detection catches slipping deals before they're lost.

**How AI powers it**:
- Trains a lightweight model on historical deal outcomes (won/lost) correlated with engagement patterns (email opens, reply sentiment, meeting cadence, time-in-stage). Given the likely dataset size (<10K deals), this would be a gradient-boosted tree (XGBoost/LightGBM) rather than deep learning.
- Claude analyzes qualitative signals (reply text sentiment trends, objection patterns, stakeholder engagement) that the statistical model can't capture, producing a composite AI confidence score.
- Time-series forecasting on pipeline value weighted by AI-adjusted probabilities, not static stage percentages.
- Anomaly detection flags deals where engagement patterns diverge from historical winners (e.g., no meeting in 14 days during negotiation stage when winners average 3 days).

**Key technical approach**:
- New model: `DealPrediction` — stores predicted close probability, risk flags, contributing factors, predicted close date.
- New service: `pipeline_forecaster.py` — runs weekly scoring of all active deals, stores predictions, computes aggregate forecasts.
- Celery task: `rescore_deal_predictions` — weekly batch + on-demand when deal stage changes.
- Extend `deals.py` router with `GET /deals/forecast` and `GET /deals/{id}/prediction`.
- Training data: join `deals` (outcome) with `email_sends` + `outreach_events` + `interactions` + `email_sentiment_scores` + `client_health` for feature engineering.

**Effort**: M (2-3 weeks). The data model and API are straightforward; the ML pipeline is the main work, but scikit-learn/XGBoost keeps it manageable.

---

### 1.2 AI-Optimized Sequence Engine

**What it does**: Instead of static outreach sequences (fixed delays, fixed content), the AI dynamically adjusts timing, channel, and messaging for each enrolled contact based on their engagement behavior and similar-contact patterns.

**Why it matters for lead gen**: Static sequences treat every lead the same. An operations VP who opened 3 emails in a row needs a different cadence than a plant manager who hasn't opened any. Dynamic optimization increases reply rates by matching outreach to individual behavior patterns.

**How AI powers it**:
- **Timing optimization**: Analyzes per-contact open/click patterns (existing `send_timing.py` does this at aggregate level — extend to individual). If a contact always opens at 2 PM Tuesday, schedule their emails for 1:55 PM Tuesday.
- **Channel switching**: When email engagement drops (3+ unopened), AI recommends switching to phone or LinkedIn for the next touch. Uses the existing multi-channel step types (`email/call/linkedin/video/direct_mail`) that are already modeled in `sequence_steps`.
- **Content adaptation**: AI rewrites the next email based on what happened — if they opened but didn't reply, shift angle; if they clicked the demo link, reference it; if sentiment analysis showed a specific objection in a prior reply, address it directly.
- **Send/skip decisions**: AI decides whether to send the next step at all. If a contact just replied positively, don't send the scheduled follow-up; if they unsubscribed from a competitor (intent signal), accelerate.

**Key technical approach**:
- New service: `sequence_optimizer.py` — wraps existing `process_outreach_queue` with an AI decision layer.
- Extend `OutreachEnrollment` with `optimization_context` (JSONB) to store per-enrollment AI decisions and reasoning.
- Modify `send_sequence_step` task to call optimizer before sending, which can adjust delay, rewrite content, switch channel, or skip.
- Claude prompt with full contact context (engagement history, sentiment scores, industry) produces structured JSON decision.
- A/B test optimized vs. static sequences to validate lift — extend existing `ab_testing_service.py`.

**Effort**: M (2-3 weeks). Most infrastructure exists (sequences, steps, enrollment, events, sentiment). The new work is the optimizer service and the modified task flow.

---

### 1.3 Intelligent Lead Prioritization & Routing

**What it does**: Goes beyond the current rule-based scoring to produce a dynamic priority score that updates in real-time based on engagement signals, then automatically routes leads to the best-matched rep based on their expertise, capacity, and historical win rates.

**Why it matters for lead gen**: Current scoring runs on a 30-minute batch cycle and uses static weights. Real-time scoring means the moment a lead clicks a demo link at 2 AM, they shoot to the top of the queue for the next business day. Routing ensures the CNC machining expert gets CNC leads, not injection molding ones.

**How AI powers it**:
- **Real-time signal processing**: Email opens, clicks, website visits, reply sentiment changes trigger immediate score recalculation (event-driven, not batch).
- **Propensity modeling**: Instead of just "how good is this lead?" (fit), predict "how likely is this lead to convert right now?" (timing). A tier-2 lead showing buying signals this week beats a tier-1 lead that's been dormant.
- **Rep matching**: Analyze each rep's historical conversion rates by industry, company size, contact role, and geography to find the best match. Factor in current workload (active enrollments, pending tasks, open deals).

**Key technical approach**:
- Extend `LeadScore` model with `propensity_score` (0-100), `signal_velocity` (rate of engagement change), `last_signal_at`, `recommended_rep_id`.
- New service: `lead_router.py` — scoring function that combines fit (existing) + propensity (new) + timing (new).
- Webhook/event handler: on email open/click/reply events (existing `tracking.py`), trigger async score update.
- Rep profile model: `RepProfile` — tracks industry expertise, win rates by segment, current capacity.
- Modify `suggestions.py` to incorporate propensity scores alongside existing rule-based suggestions.

**Effort**: M (2-3 weeks). Scoring extensions are moderate; rep matching adds a new dimension but the user model already exists.

---

### 1.4 AI Company Research Agent

**What it does**: Before any outreach, the AI autonomously researches a company — scraping their website, checking news, analyzing their tech stack, identifying pain points, and producing a research brief that feeds directly into email/script generation.

**Why it matters for lead gen**: Personalized outreach dramatically outperforms generic. The existing `web_scraper.py` extracts basic metadata; the existing `news_monitor.py` checks Google News. But neither produces actionable research. An AI research agent synthesizes multiple sources into "here's what matters for our pitch to this company."

**How AI powers it**:
- **Multi-source synthesis**: Combines website scrape (existing), news (existing), Apollo enrichment (existing), and new sources (job postings, press releases, LinkedIn company page) into a unified research brief.
- **Pain point extraction**: AI identifies specific operational pain points from website content ("we're ISO certified" → quality-conscious, "24/7 operations" → downtime is critical, "100+ SKUs" → changeover complexity).
- **Outreach angle generation**: Produces 3 ranked outreach angles with reasoning, not just a generic company summary.
- **Competitive landscape**: Identifies if they mention competitors or specific technologies that Apprend competes with or integrates with.

**Key technical approach**:
- New model: `CompanyResearch` — stores research brief, pain points (JSONB), outreach angles (JSONB), sources used, research date, staleness flag.
- New service: `research_agent.py` — orchestrates web scraper + Apollo + news + Claude synthesis.
- Extend `ai_email_generator.py` and `ai_script_generator.py` to consume research briefs when available.
- Celery task: `research_company` — triggered on company creation or before first outreach enrollment.
- Claude prompt includes scraped website text, news summaries, enrichment data; returns structured JSON with pain points, angles, competitive intel.

**Effort**: M (2-3 weeks). Individual data sources already exist; the new work is orchestration, the research model, and the Claude synthesis prompt.

---

### 1.5 Conversation Intelligence (Email Thread Analysis)

**What it does**: Analyzes entire email conversation threads (not just individual replies) to track how a deal is progressing conversationally — detecting momentum shifts, identifying decision-maker involvement, flagging stalled conversations, and suggesting the optimal next message.

**Why it matters for lead gen**: The existing sentiment analysis (`sentiment_service.py`) scores individual replies in isolation. But conversations have arcs — a lead might start warm, raise an objection, then go quiet. Thread-level analysis catches these patterns and recommends intervention before the deal goes cold.

**How AI powers it**:
- **Momentum tracking**: Scores conversation trajectory (accelerating, stable, decelerating, stalled) based on reply frequency, sentiment trend, and message length changes.
- **Stakeholder mapping**: Detects when new people join the thread (CC'd, forwarded) — a sign of internal buy-in or escalation. Identifies roles from email signatures.
- **Objection tracking**: Maintains a running list of raised objections and whether they've been addressed. Alerts when an objection was acknowledged but never resolved.
- **Next-best-message**: Given the full thread context, generates the ideal next message — not just a reply template, but a strategically crafted response that advances the deal.

**Key technical approach**:
- New model: `ConversationAnalysis` — stores thread_id, momentum_score, stakeholder_map (JSONB), unresolved_objections (JSONB), recommended_action, analysis_date.
- New service: `conversation_intelligence.py` — takes a gmail_thread_id, pulls all messages (existing `email_history.py`), sends full thread to Claude for analysis.
- Celery task: `analyze_active_threads` — runs on threads with recent activity, stores analysis.
- Extend `emails.py` router: `GET /thread/{thread_id}/intelligence` returns conversation analysis.
- Claude receives the full thread (chronological) with metadata and returns structured analysis.

**Effort**: S-M (1-2 weeks). Email history and threading already work; this is primarily a new Claude prompt + model + endpoint.

---

### 1.6 Enhanced Smart Suggestions Engine

**What it does**: Replaces the current rule-based suggestions (`suggestions.py` — 4 static rules) with an AI-powered recommendation engine that considers the full context of every lead and produces ranked, actionable next-best-actions with specific timing.

**Why it matters for lead gen**: The current suggestions are basic: overdue follow-ups, stale leads, news, high scores. An AI engine can cross-reference dozens of signals to produce recommendations like "Call Jane at Acme tomorrow at 10 AM — she opened your email 3 times yesterday, her company just posted a quality engineer job (expansion signal), and your last call with their VP went well."

**How AI powers it**:
- **Cross-signal correlation**: Combines engagement data, sentiment trends, news events, deal stage, health scores, call outcomes, and time-based patterns.
- **Contextual reasoning**: Claude explains *why* each action is recommended and *when* to take it, not just *what* to do.
- **Priority calibration**: Learns from action outcomes — if a rep follows a suggestion and it leads to a reply, similar future suggestions get boosted.
- **Proactive alerts**: Pushes time-sensitive suggestions (e.g., "competitor mentioned in Acme's reply 2 hours ago — call now") rather than waiting for the rep to check.

**Key technical approach**:
- Extend existing `suggestions.py` with `GET /suggestions/ai` endpoint.
- New service: `smart_suggestions.py` — aggregates signals across all models for a user's active leads, batches them to Claude, returns ranked recommendations.
- New model: `SuggestionOutcome` — tracks whether suggestions were acted on and the result, creating a feedback loop.
- Celery task: `generate_daily_suggestions` — produces a prioritized daily action plan per user.
- Integrate with existing `call_queue.py` and `follow_ups.py` to auto-create tasks from accepted suggestions.

**Effort**: M (2-3 weeks). Signal aggregation across existing models is the main complexity.

---

## Phase 2 — Differentiation Features (new capabilities)

These features introduce capabilities Landline doesn't have yet. They're what separate an AI-driven CRM from a regular one with AI bolted on.

---

### 2.1 Intent Signal Detection & Buyer Intent Scoring

**What it does**: Monitors external signals that indicate a company is actively in a buying cycle — job postings (hiring for roles your product addresses), technology changes, expansion announcements, funding rounds, leadership changes, regulatory events — and incorporates them into lead scoring.

**Why it matters for lead gen**: Cold outreach to a random list converts at 1-2%. Reaching a company *at the moment they're experiencing the problem you solve* converts at 10x that rate. Intent signals are the difference between "spray and pray" and precision targeting.

**How AI powers it**:
- **Signal classification**: AI categorizes detected signals by type (hiring, expansion, technology, leadership, regulatory) and relevance to Apprend's value proposition.
- **Urgency scoring**: Not all signals are equal. "Hiring a Quality Manager" (they're building capability) is weaker than "Hiring 50 operators" (they're scaling production and will have quality problems).
- **Timing inference**: AI estimates when the buying window opens based on signal type. A new plant manager has 90 days to make changes; a funding round means procurement budget in 60-90 days.
- **Outreach triggering**: High-intent signals auto-create outreach tasks with pre-written context-aware messaging.

**Key technical approach**:
- New model: `IntentSignal` — company_id, signal_type, source, raw_data (JSONB), relevance_score, urgency_score, detected_at, expires_at.
- New service: `intent_monitor.py` — scheduled checks across multiple sources:
  - Job postings: Scrape/API from job boards for relevant titles (Quality Engineer, Plant Manager, Production Supervisor) at tracked companies.
  - Funding/M&A: Extend existing `news_monitor.py` with funding-specific parsing.
  - Technology signals: Monitor for mentions of competitor products, ERP implementations, digital transformation keywords.
- Celery task: `scan_intent_signals` — daily scan across all active-pipeline companies.
- Integrate intent score into `lead_router.py` propensity model from Phase 1.
- Claude analyzes raw signals and produces structured relevance/urgency assessments.

**Effort**: L (3-5 weeks). Multiple new data sources, each with its own scraping/API integration. The AI analysis is straightforward; the data acquisition is the hard part.

---

### 2.2 Autonomous Multi-Channel Orchestration

**What it does**: An AI agent that autonomously manages the full outreach lifecycle for each lead — deciding not just *what* to send, but *which channel* to use, *when* to escalate from email to phone to LinkedIn, *when* to involve a human*, and *when to stop*. Essentially, the AI runs the SDR playbook.

**Why it matters for lead gen**: Manual multi-channel orchestration is the biggest bottleneck in outbound sales. Reps forget to follow up, use the wrong channel, send at bad times, or give up too early. An AI orchestrator maintains perfect cadence across all channels simultaneously with zero dropped balls.

**How AI powers it**:
- **Channel selection model**: For each touch, AI evaluates: Has email been effective for this persona type? Is there a LinkedIn profile? Do we have a phone number? What's the contact's demonstrated channel preference (where do they engage)?
- **Escalation logic**: If 3 emails go unopened, switch to LinkedIn. If LinkedIn message is read but no reply, trigger a phone call. If phone goes to voicemail, send a different-angle email. This is not rule-based — it's AI-driven based on what's working for similar contacts.
- **Human-in-the-loop triggers**: AI identifies moments that need human judgment: a complex objection, a request for pricing, a warm referral opportunity. Routes to the assigned rep with full context.
- **Autonomous operation**: Within configured guardrails (max touches, required channels, compliance rules), the AI operates independently, only flagging exceptions.

**Key technical approach**:
- New service: `orchestrator.py` — the central decision engine. Takes a contact + all context, produces a `TouchDecision` (channel, timing, content, or "flag for human").
- Extend `OutreachEnrollment` with `orchestration_mode` (manual/assisted/autonomous) and `orchestration_log` (JSONB array of decisions with reasoning).
- New Celery task: `run_orchestrator` — processes all autonomous enrollments, makes decisions, executes touches.
- Guardrails model: `OrchestrationPolicy` — per-user or global rules (max daily touches, required channels, human-review triggers, compliance overrides).
- Builds on Phase 1 sequence optimizer but goes further — the AI *creates* the sequence dynamically rather than optimizing a predefined one.

**Effort**: L (4-6 weeks). This is architecturally significant — it moves from "AI assists human workflow" to "AI runs the workflow with human oversight." Requires careful guardrail design and testing.

---

### 2.3 Relationship Intelligence & Org Chart Mapping

**What it does**: Automatically maps organizational hierarchies at target companies, identifies decision-makers, champions, blockers, and coaches, tracks relationship strength with each, and recommends multi-threading strategies (engaging multiple stakeholders).

**Why it matters for lead gen**: Deals die when they're single-threaded — one contact goes on vacation or changes roles, and you lose the deal. B2B sales requires multi-threading: engaging the economic buyer, the technical evaluator, the end user, and an internal champion simultaneously. Relationship intelligence makes this systematic.

**How AI powers it**:
- **Org chart inference**: From contact titles, email signatures, CC patterns, and LinkedIn data, AI infers reporting relationships and org structure.
- **Stakeholder classification**: AI categorizes each contact as Decision Maker (signs the check), Champion (advocates internally), Coach (provides intel), Blocker (resists change), or End User (daily operator).
- **Relationship strength scoring**: Based on email engagement, meeting attendance, reply sentiment, and interaction frequency, each relationship gets a strength score (cold/warm/strong).
- **Multi-thread recommendations**: AI identifies gaps ("you have strong engagement with Operations but zero contact with Finance — Finance signs POs over $50K") and suggests specific outreach to fill them.

**Key technical approach**:
- New model: `StakeholderMap` — company_id, contact_id, inferred_role (decision_maker/champion/coach/blocker/end_user), relationship_strength, last_interaction, engagement_trend.
- New model: `OrgRelationship` — contact_id, related_contact_id, relationship_type (reports_to/peers_with/manages), confidence, source.
- New service: `relationship_intelligence.py` — analyzes contact metadata, email CC patterns, meeting attendees, and interaction history to build org maps.
- Extend `tactics.py` with `GET /companies/{id}/stakeholder-map` and `POST /companies/{id}/multi-thread-plan`.
- Claude prompt with all contacts at a company + their interactions produces stakeholder classifications and multi-thread recommendations.

**Effort**: L (3-4 weeks). The inference logic is complex, and the org chart visualization needs frontend work, but the backend data mostly exists (contacts, interactions, email threads).

---

### 2.4 AI Sales Coach & Rep Performance Analytics

**What it does**: Analyzes each rep's outreach patterns, call outcomes, email effectiveness, deal velocity, and conversion rates to identify strengths, weaknesses, and specific coaching recommendations. Provides per-rep dashboards and manager-level team analytics.

**Why it matters for lead gen**: Most CRMs track activity (emails sent, calls made) but not *quality*. An AI coach can tell you "Rep A's emails get 2x the reply rate of Rep B because they use shorter subject lines and ask questions" — actionable insight, not just metrics.

**How AI powers it**:
- **Pattern analysis**: Compares each rep's email templates, subject lines, send times, and follow-up cadence against team averages and top performers.
- **Win/loss analysis**: For each lost deal, AI analyzes the conversation history to identify where the deal went wrong — was it a missed follow-up, an unaddressed objection, wrong channel, wrong stakeholder?
- **Skill gap identification**: Clusters rep performance by activity type (prospecting, discovery, negotiation, closing) to identify where each rep needs coaching.
- **Benchmarking**: "Your reply rate on first touches is 8% vs. team average 12%. Top performer achieves 18% by referencing specific pain points from recent news."

**Key technical approach**:
- New model: `RepPerformance` — user_id, period (weekly/monthly), metrics (JSONB: emails_sent, reply_rate, open_rate, calls_completed, connect_rate, deals_created, deals_won, avg_deal_velocity, etc.), ai_insights (JSONB), coaching_recommendations (JSONB).
- New service: `sales_coach.py` — aggregates per-rep metrics from existing tables (email_sends, call_tasks, deals, interactions), computes benchmarks, sends to Claude for qualitative analysis.
- New router: `coaching.py` — `GET /coaching/my-performance`, `GET /coaching/team` (admin/manager only), `GET /coaching/recommendations`.
- Celery task: `generate_weekly_coaching` — weekly analysis per rep.
- Claude receives rep metrics + anonymized top-performer patterns, produces specific coaching recommendations.

**Effort**: M (2-3 weeks). All underlying data exists; this is aggregation, analysis, and presentation. The `UserScope` system already handles per-user data access.

---

### 2.5 Smart Meeting Scheduler & Pre-Meeting Briefings

**What it does**: When a lead expresses interest (detected via reply analysis), the system automatically suggests meeting times, generates calendar invites, and produces comprehensive pre-meeting briefings with talking points, objection preparation, and competitive positioning.

**Why it matters for lead gen**: The gap between "interested" and "meeting booked" is where leads leak. Every hour of delay reduces conversion. Auto-scheduling eliminates back-and-forth; pre-meeting briefings ensure reps walk in prepared.

**How AI powers it**:
- **Intent-triggered scheduling**: When reply analysis detects `meeting_request` or `interested` intent (already exists in `sentiment_service.py`), automatically generate a scheduling link or propose times.
- **Briefing generation**: Synthesizes company research, conversation history, sentiment trends, stakeholder map, news, and industry pain points into a one-page briefing.
- **Objection preparation**: Based on the industry, company size, and any objections raised in prior emails, pre-generates objection responses.
- **Post-meeting action items**: After a meeting interaction is logged, AI generates follow-up tasks and recommended next steps.

**Key technical approach**:
- New service: `meeting_intelligence.py` — briefing generation (extends existing `call_briefing.py` pattern, which already does pre-call briefings).
- Calendar integration: Google Calendar API (natural extension of existing Gmail OAuth — same scopes).
- New model: `MeetingBrief` — company_id, contact_ids, briefing_content (JSONB with sections: background, pain_points, objection_prep, competitive_positioning, recommended_agenda), generated_at.
- Extend `smart_reply.py`: when auto-reply detects meeting interest, include scheduling link.
- Extend `interactions.py`: after meeting logged, trigger AI post-meeting analysis.

**Effort**: M-L (3-4 weeks). Briefing generation is straightforward (extends call_briefing pattern). Calendar integration is the main new infrastructure.

---

### 2.6 Competitive Intelligence Engine

**What it does**: Monitors competitor activity (product launches, pricing changes, customer wins/losses, job postings, news) and provides reps with real-time competitive positioning guidance during outreach and deals.

**Why it matters for lead gen**: In B2B manufacturing software, prospects are often evaluating 2-3 solutions simultaneously. Knowing what competitors are doing — and having ready responses — is the difference between winning and losing.

**How AI powers it**:
- **Competitor monitoring**: Extends existing `news_monitor.py` to also track named competitors, not just target companies.
- **Battle card generation**: AI produces per-competitor battle cards: their strengths, their weaknesses, how to position against them, landmine questions to ask prospects.
- **Deal-level competitive intel**: When a prospect mentions a competitor (detected in email replies via sentiment analysis or call notes), AI surfaces relevant battle card content and suggested responses.
- **Win/loss correlation**: Tracks which competitors appear in won vs. lost deals to identify competitive win rates and adjust strategy.

**Key technical approach**:
- New model: `Competitor` — name, domain, products (JSONB), strengths (JSONB), weaknesses (JSONB), battle_card (JSONB), last_updated.
- New model: `CompetitorMention` — company_id, competitor_id, source (email/call/news), context, detected_at.
- New service: `competitive_intelligence.py` — monitors news for competitors, detects mentions in email/call text, generates battle cards.
- Extend `sentiment_service.py` to detect competitor mentions in reply analysis.
- Extend `ai_script_generator.py` to include competitive positioning when a competitor is mentioned.

**Effort**: M (2-3 weeks). Leverages existing news monitoring and sentiment analysis infrastructure. Battle card generation is a Claude prompt.

---

## Phase 3 — Advanced AI & Autonomous Operations

These features push toward true AI autonomy — the system identifies, researches, and engages leads with minimal human intervention.

---

### 3.1 Autonomous Prospecting Agent

**What it does**: An AI agent that continuously identifies new companies matching the ICP (Ideal Customer Profile), researches them, scores them, and either adds them to the pipeline for human review or directly enrolls high-confidence matches into outreach sequences.

**Why it matters for lead gen**: Manual prospecting (searching Apollo, scraping directories, importing CSVs) is the lowest-value, most time-consuming activity in sales. Automating it means the pipeline never runs dry and reps spend 100% of their time on selling.

**How AI powers it**:
- **ICP learning**: Analyzes characteristics of won deals (industry, size, location, tech stack, pain points) to build a dynamic ICP model that evolves as you win more deals.
- **Source orchestration**: Queries Apollo, ThomasNet (existing scrapers), job boards, industry directories, and news sources to find companies matching the ICP.
- **Qualification filtering**: AI evaluates each found company against the ICP model, eliminates poor fits, and ranks the rest.
- **Research-to-outreach pipeline**: For high-confidence matches, triggers the research agent (Phase 1.4), then auto-generates personalized first-touch outreach, then enrolls in appropriate sequence — all autonomously.

**Key technical approach**:
- New model: `IcpProfile` — industry_weights, size_range, location_preferences, technology_signals, pain_point_indicators, exclusion_criteria — all learned from historical data.
- New model: `ProspectingRun` — tracks each autonomous prospecting session: sources queried, companies found, qualified, enrolled, outcomes.
- New service: `autonomous_prospector.py` — orchestrates ICP matching, source querying, qualification, and enrollment.
- Celery task: `run_prospecting_agent` — daily or weekly, configurable volume targets.
- Guardrails: maximum daily additions, human review queue for borderline cases, automatic pause if conversion rate drops.
- Builds on existing: `enrichment.py` (Apollo), `scoring.py` (qualification), `research_agent.py` (Phase 1.4), `sequence_optimizer.py` (Phase 1.2).

**Effort**: L (4-6 weeks). This is the most architecturally complex feature — it chains multiple existing services into an autonomous pipeline with quality controls and feedback loops.

---

### 3.2 Predictive Churn Prevention & Expansion Intelligence

**What it does**: For existing clients (companies with `pipeline_stage == 'client'`), predicts churn risk before it happens and identifies expansion opportunities — upsell, cross-sell, or referral potential — based on engagement patterns and account health trends.

**Why it matters for lead gen**: Retaining a client costs 5-7x less than acquiring a new one. Expansion revenue from existing clients is the highest-margin growth. The existing `client_health.py` tracks health reactively; this makes it predictive.

**How AI powers it**:
- **Churn prediction**: Trains on historical churn patterns (engagement decline curves, sentiment shifts, support ticket patterns, meeting frequency drops) to predict churn 30-60 days before it happens.
- **Churn driver analysis**: AI identifies *why* a client is at risk — not just "health score dropped" but "response time to your emails increased 3x, they haven't attended the last 2 scheduled calls, and their quality manager who was your champion left the company."
- **Expansion signals**: Detects signals that a client is ready for more — new plant openings, increased production volume (hiring operators), expanding to new product lines, positive sentiment trends.
- **Intervention recommendations**: For at-risk clients, generates specific save strategies. For expansion-ready clients, generates upsell outreach.

**Key technical approach**:
- Extend `ClientHealthSnapshot` with `predicted_churn_probability`, `churn_risk_factors` (JSONB), `expansion_signals` (JSONB).
- New service: `churn_predictor.py` — analyzes health trends, engagement velocity, sentiment trajectory.
- New service: `expansion_detector.py` — monitors client companies for growth signals (new contacts added, increased email engagement, news about expansion).
- Celery task: `predict_client_health` — weekly analysis of all clients.
- Extend `clients.py` router: `GET /clients/churn-risk`, `GET /clients/expansion-opportunities`.
- Alert system: high-risk churn triggers immediate notification to account owner.

**Effort**: M-L (3-4 weeks). Extends existing health scoring and news monitoring. The predictive model needs historical churn data to train on.

---

### 3.3 AI Email Deliverability Optimizer

**What it does**: Monitors email deliverability health (bounce rates, spam complaint rates, inbox placement) and automatically adjusts sending behavior to maximize inbox placement — managing sender reputation, warmup schedules, content optimization for spam filters, and domain rotation.

**Why it matters for lead gen**: All the AI-generated emails in the world are worthless if they land in spam. Deliverability is the invisible bottleneck that kills outbound programs. The existing warmup ramp (10/25/40/full) is static; a real optimizer adapts to actual deliverability signals.

**How AI powers it**:
- **Reputation monitoring**: Tracks bounce rates, complaint rates, and open rates per sender account to compute a sender reputation score.
- **Adaptive throttling**: If a sender's bounce rate spikes, automatically reduces volume and shifts sends to healthier accounts. If reputation improves, gradually increases.
- **Content scanning**: Before sending, AI reviews email content against known spam trigger patterns (excessive capitalization, spammy phrases, too many links, missing unsubscribe) and suggests fixes.
- **Domain health**: Monitors SPF/DKIM/DMARC alignment per sending domain and alerts on configuration issues.

**Key technical approach**:
- New model: `SenderHealth` — account_id, account_type (gmail/smtp), bounce_rate_7d, complaint_rate_7d, open_rate_7d, reputation_score, throttle_factor, last_assessed.
- New service: `deliverability_optimizer.py` — computes sender health, adjusts send allocation in `email_dispatch.py`'s round-robin rotation.
- Extend `send_limiter.py` with dynamic limits based on sender health (currently static).
- Content scanner: Claude reviews email content pre-send, returns spam risk assessment.
- Celery task: `assess_sender_health` — hourly health check across all sender accounts.

**Effort**: M (2-3 weeks). Sender health tracking is straightforward aggregation of existing event data. The adaptive throttling integrates into existing send limiter.

---

### 3.4 Knowledge-Augmented Selling (RAG for Sales)

**What it does**: Builds a retrieval-augmented generation (RAG) system over the sales knowledge base, past email conversations, call notes, deal histories, and product documentation so that any AI feature can draw on the full institutional knowledge when generating content.

**Why it matters for lead gen**: Currently, each AI call sends a fixed prompt with limited context. A RAG system means the AI can reference "the email that worked for a similar CNC machining company last quarter" or "the objection handling that closed the Acme deal" — institutional memory that makes every AI output smarter.

**How AI powers it**:
- **Embedding all content**: Emails, call scripts, deal notes, interactions, knowledge documents, successful outreach sequences — all embedded and indexed for semantic search.
- **Context retrieval**: Before any AI generation (email, script, analysis), retrieve the most relevant historical context and inject it into the prompt.
- **Learning from success**: Embeddings are weighted by outcome — emails that got replies rank higher than those that didn't. Scripts that led to meetings rank higher than those that led to hang-ups.
- **Natural language querying**: Reps can ask "how did we handle price objections from food & beverage companies?" and get synthesized answers from historical data.

**Key technical approach**:
- Vector database: pgvector extension in PostgreSQL (no new infrastructure — just an extension on existing DB).
- New model: `ContentEmbedding` — source_type, source_id, content_text, embedding (vector), outcome_weight, created_at.
- New service: `rag_service.py` — embedding generation (Anthropic/OpenAI embeddings API or sentence-transformers), similarity search, context assembly.
- Modify all AI services to call RAG for relevant context before Claude calls.
- New endpoint: `POST /knowledge/ask` — natural language query against the knowledge base.
- Celery task: `index_new_content` — processes new emails, interactions, and deal notes into embeddings.

**Effort**: L (4-5 weeks). pgvector setup, embedding pipeline, integration with all existing AI services, and the query interface. This is foundational infrastructure that improves every other AI feature.

---

### 3.5 Voice Intelligence (Call Recording Analysis)

**What it does**: Integrates with VoIP/phone systems to record calls, transcribe them, and analyze the conversation for coaching insights, objection patterns, competitive mentions, action items, and deal progression signals.

**Why it matters for lead gen**: Phone is a critical channel (the existing cold calling system is extensive), but currently call outcomes are manually logged. Automated analysis turns every call into structured data that feeds scoring, coaching, and pipeline prediction.

**How AI powers it**:
- **Transcription**: Whisper API or similar for speech-to-text on recorded calls.
- **Conversation analysis**: AI identifies key moments — objections raised, questions asked, commitments made, competitors mentioned, next steps agreed on.
- **Coaching feedback**: Per-call scoring on talk-to-listen ratio, question quality, objection handling effectiveness, and close technique.
- **CRM auto-population**: Extracts structured data (action items, next meeting date, stakeholder names mentioned) and auto-creates tasks, interactions, and follow-ups.

**Key technical approach**:
- New model: `CallRecording` — call_task_id, recording_url, duration, transcription (Text), analysis (JSONB), created_at.
- New service: `voice_intelligence.py` — handles transcription (Whisper API), analysis (Claude with full transcript), and CRM population.
- Integration: Twilio, Aircall, or similar VoIP API for call recording. Or browser-based recording via WebRTC.
- Extend `calls.py` router: `POST /calls/tasks/{id}/recording`, `GET /calls/tasks/{id}/analysis`.
- Claude prompt with transcript produces structured analysis: key moments, objections, action items, coaching notes, deal signals.

**Effort**: L (5-7 weeks). VoIP integration is significant new infrastructure. Transcription and analysis are straightforward Claude/Whisper calls, but reliable call recording requires telephony infrastructure.

---

## Phase 4 — Platform & Ecosystem (long-term vision)

---

### 4.1 Multi-Tenant White-Label Platform

**What it does**: Evolves Landline from a single-company tool into a multi-tenant platform that other B2B companies can use, with white-labeling, custom branding, and per-tenant configuration.

**Why it matters**: Monetization path — SaaS revenue from other B2B companies using the same AI-driven CRM infrastructure.

**Key technical approach**:
- Tenant model with org-level isolation (row-level security in PostgreSQL).
- Tenant-specific configuration: industry verticals, AI prompt customization, branding.
- Usage-based billing for AI features (track Claude API calls per tenant).
- Extend existing `UserScope` pattern to include org_id scoping.

**Effort**: XL (8-12 weeks). Fundamental architecture change affecting every query and endpoint.

---

### 4.2 Integration Marketplace

**What it does**: Standardized integration framework for connecting to external tools — CRM systems (HubSpot, Salesforce for migration), communication tools (Slack, Teams), data providers (ZoomInfo, Clearbit), and productivity tools (Google Workspace, Outlook).

**Why it matters**: An AI CRM needs data from everywhere. The more data it has, the smarter its recommendations. Integrations also reduce friction for adoption.

**Key technical approach**:
- Integration framework: standardized connector interface (OAuth, API key, webhook).
- Priority integrations: Slack (notifications), Google Calendar (meetings), HubSpot/Salesforce (migration/sync), ZoomInfo/Clearbit (enrichment alternatives to Apollo).
- Webhook framework for real-time event streaming.

**Effort**: L per integration (2-3 weeks each), M for the framework (2-3 weeks).

---

### 4.3 AI Workflow Builder

**What it does**: A visual workflow builder where users define custom AI-powered automation rules: "When a lead opens 3+ emails AND their company posts a job for a quality engineer, THEN research the company, generate a personalized email referencing the job posting, and enroll in the high-intent sequence."

**Why it matters**: Power users want to create their own AI workflows without writing code. This makes Landline a platform, not just a product.

**Key technical approach**:
- Workflow model: trigger conditions (event-based), action steps (AI generation, CRM updates, notifications), branching logic.
- Event bus: Redis Streams or similar for real-time event processing.
- Visual builder is frontend-heavy; backend provides a workflow execution engine.
- Leverage existing services as workflow actions (send email, generate script, score lead, create task, etc.).

**Effort**: XL (8-12 weeks). The workflow engine and execution runtime are complex; the visual builder is significant frontend work.

---

## Priority Matrix

| Feature | Phase | Impact | Effort | Priority |
|---------|-------|--------|--------|----------|
| Predictive Pipeline & Forecasting | 1.1 | High | M | **P0** |
| AI-Optimized Sequence Engine | 1.2 | Very High | M | **P0** |
| Intelligent Lead Prioritization | 1.3 | High | M | **P0** |
| AI Company Research Agent | 1.4 | High | M | **P1** |
| Conversation Intelligence | 1.5 | High | S-M | **P1** |
| Enhanced Suggestions Engine | 1.6 | Medium | M | **P1** |
| Intent Signal Detection | 2.1 | Very High | L | **P1** |
| Autonomous Multi-Channel Orchestration | 2.2 | Very High | L | **P2** |
| Relationship Intelligence | 2.3 | High | L | **P2** |
| AI Sales Coach | 2.4 | Medium | M | **P2** |
| Smart Meeting Scheduler | 2.5 | Medium | M-L | **P2** |
| Competitive Intelligence | 2.6 | Medium | M | **P2** |
| Autonomous Prospecting Agent | 3.1 | Very High | L | **P3** |
| Churn Prevention & Expansion | 3.2 | High | M-L | **P3** |
| Deliverability Optimizer | 3.3 | High | M | **P2** |
| Knowledge-Augmented Selling (RAG) | 3.4 | Very High | L | **P3** |
| Voice Intelligence | 3.5 | High | L | **P3** |
| Multi-Tenant Platform | 4.1 | Strategic | XL | **P4** |
| Integration Marketplace | 4.2 | High | L | **P4** |
| AI Workflow Builder | 4.3 | Strategic | XL | **P4** |

**Effort key**: S = 1-2 weeks, M = 2-3 weeks, L = 3-6 weeks, XL = 8-12 weeks

---

## Existing Features to Enhance (Quick Wins)

These are improvements to existing features that don't warrant their own section but deliver meaningful value:

**Lead Scoring** (existing `scoring.py`): Add engagement velocity as a first-class scoring dimension. The current implementation has signal recency decay and velocity bonus, but they're additive. Make velocity a multiplier — a lead going from 0 to 3 opens in 24 hours should jump dramatically, not just get +7 points. Also, start tracking score change history to show "this lead's score increased 40 points in the last week." Effort: S.

**Email A/B Testing** (existing `ab_testing_service.py`): Currently manual — the user creates variants and the system tracks. Automate it: AI generates 2-3 subject line variants for each sequence step, system auto-splits traffic, auto-declares winner, auto-rolls out winner. Multi-armed bandit instead of fixed-split A/B. Effort: S-M.

**Smart Reply** (existing `smart_reply.py`): Currently auto-replies to clear positive intents. Extend to handle objections — when a lead says "too expensive," auto-generate a value-focused response with ROI calculations. When they say "not now," auto-schedule a follow-up for 30 days. When they say "talk to [name]," auto-create a referral outreach task. Effort: S.

**News Monitor** (existing `news_monitor.py`): Currently checks Google News RSS. Add company blog/PR monitoring — scrape the company's own news/blog page for announcements. Also add trigger actions: when actionable news is found, auto-generate a timely outreach email referencing it. Effort: S-M.

**Call Scripts** (existing `ai_script_generator.py`): Add real-time script adaptation — after logging a call outcome (voicemail, gatekeeper, connected), the AI generates the optimal follow-up script for the next attempt incorporating what happened. Currently scripts are static once generated. Effort: S.

**Knowledge Base** (existing `knowledge.py`): Surface knowledge contextually — when a rep is composing an email to an injection molding company, automatically show relevant pain points, objection handling, and industry profile from the knowledge base without them having to look it up. Effort: S.

---

## Technical Infrastructure Needed Across Phases

**Vector Database (pgvector)**: Required for RAG (3.4) and beneficial for similarity-based features (finding similar companies, similar email patterns). Install as PostgreSQL extension — no new infrastructure.

**Event Bus**: Redis Streams (already have Redis) for real-time event processing. Needed for real-time scoring (1.3), intent signals (2.1), and the workflow builder (4.3).

**ML Training Pipeline**: For predictive models (pipeline forecasting 1.1, churn prediction 3.2, ICP learning 3.1). scikit-learn/XGBoost for tabular data — runs in Celery tasks, no GPU needed.

**Embedding Pipeline**: For RAG (3.4). Anthropic's embedding API or sentence-transformers (local). Batch processing via Celery.

**VoIP Integration**: For voice intelligence (3.5). Twilio or Aircall API. Significant new infrastructure.

**Calendar Integration**: For meeting scheduling (2.5). Google Calendar API via existing OAuth credentials (additional scopes).

---

## What Makes This an AI CRM, Not a CRM With AI

The distinction matters. A "CRM with AI" bolts AI features onto CRUD operations — generate an email, score a lead, summarize a call. Landline already does this.

An **AI CRM** means:

1. **AI is the primary operator, humans are supervisors.** The orchestration engine (2.2) and autonomous prospector (3.1) run the sales process; reps review and intervene when needed.

2. **The system learns and improves.** Every email reply, call outcome, and deal result feeds back into scoring models, content generation, and timing optimization. The system gets smarter with every interaction.

3. **Intelligence is contextual and continuous.** RAG (3.4) + conversation intelligence (1.5) + intent signals (2.1) mean every AI action draws on the full history and current context, not just the immediate input.

4. **Predictions drive actions.** Pipeline forecasting (1.1) + churn prediction (3.2) + intent scoring (2.1) mean the system acts on what's *about to happen*, not just what already happened.

5. **Multi-channel is native, not bolted on.** The orchestrator (2.2) treats email, phone, LinkedIn, and meetings as interchangeable tools, choosing the right one for each moment.

The roadmap above progresses from enhancing existing AI features (Phase 1) through adding differentiating intelligence (Phase 2) to autonomous operation (Phase 3) and platform scale (Phase 4). Each phase delivers standalone value while building toward the full vision.
