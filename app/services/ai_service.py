"""AI service using Anthropic Claude for intelligent email and lead decisions."""

import logging
import json
from typing import Optional

import anthropic
from app.config import get_settings

logger = logging.getLogger(__name__)


class AIService:
    """Uses Claude to make intelligent decisions about outreach and leads.

    Uses AsyncAnthropic so that calls do not block the FastAPI event loop.
    Celery tasks call these via _run_async() wrappers which create a new
    event loop, so the await is resolved there too.
    """

    def __init__(self):
        settings = get_settings()
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def _parse_json_response(self, response_text: str) -> dict:
        """Parse JSON from Claude response, handling markdown code blocks and
        any stray prose the model puts before/after the JSON object."""
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Fall back to the first {...} / [...] span, so a stray preamble like
            # "Here is the JSON:" doesn't blow up an otherwise-valid response.
            start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise

    async def complete_json(self, prompt: str, max_tokens: int = 1024) -> dict:
        """Public single-shot JSON completion.

        Thin wrapper over :meth:`_complete_json` for other services (deal
        forecasting, smart suggestions) that need a structured Claude reply and
        want the same fence-tolerant parsing + never-raises contract.
        """
        return await self._complete_json(prompt, max_tokens=max_tokens)

    async def _complete_json(self, prompt: str, max_tokens: int = 1024) -> dict:
        """Send a single-shot prompt to Claude and parse the JSON reply.

        Centralizes the messages.create + markdown-fence-tolerant JSON parse used
        by the playbook/tactics helpers. Returns ``{"error": ...}`` on any
        failure so callers never raise into the request handler.
        """
        response_text = ""
        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse AI JSON response: %s", str(e))
            return {"error": "Failed to parse AI response", "raw": response_text}
        except Exception as e:
            logger.error("AI completion failed: %s", str(e))
            return {"error": str(e)}

    async def complete_text(self, prompt: str, max_tokens: int = 1024, temperature: float | None = None) -> str:
        """Public single-shot free-text completion.

        Used by features that need prose rather than JSON (RAG natural-language
        answers, voice-call summaries). Never raises — returns an empty string
        on failure so callers can degrade gracefully.
        """
        try:
            kwargs = {"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]}
            if temperature is not None:
                kwargs["temperature"] = temperature
            message = await self.client.messages.create(**kwargs)
            return message.content[0].text
        except Exception as e:
            logger.error("AI text completion failed: %s", str(e))
            return ""

    async def generate_email(
        self,
        company_name: str,
        contact_name: str,
        contact_title: str,
        industry: str,
        purpose: str = "initial outreach",
        tone: str = "professional but warm",
        additional_context: str = "",
        rag_context: str = "",
    ) -> dict:
        """Generate a personalized email using Claude.

        ``rag_context`` (feature 3.4) lets a caller inject retrieved
        institutional knowledge — e.g. an email that worked for a similar shop —
        which the model may draw on. It is advisory context, appended after the
        per-lead ``additional_context``.
        """
        try:
            ctx_line = ""
            if additional_context:
                ctx_line = f"Additional Context: {additional_context}"
            if rag_context:
                ctx_line += (
                    f"\n\nRelevant past context (draw on what worked before, do NOT copy verbatim):\n{rag_context}"
                )

            prompt = f"""You're Suman from Apprend Technologies. You build manufacturing software — shop floor monitoring, quoting tools, ERP integrations, quality tracking.

Write a cold email to:

Name: {contact_name}
Title: {contact_title}
Company: {company_name}
Industry: {industry}
{ctx_line}

HOW TO WRITE THIS:
- You're a person, not a department. Write like you'd talk to someone at a trade show
- 4-5 sentences. Under 100 words. That's it
- Open with something about THEIR world — their industry, their role, a real challenge they probably face. Not about you
- Use contractions. Short sentences. The occasional dash
- One ask at the end: a 15-min call. Frame it as a question, not a demand
- Subject line: lowercase, short, like you'd text a colleague

BANNED:
- "I hope this finds you well", "I came across", "I noticed that", "I wanted to reach out"
- "leverage", "synergy", "cutting-edge", "revolutionary", "transform", "streamline", "innovative"
- Exclamation marks
- Bullet points or lists in the body

GOOD example:
Subject: quick question about your quoting process
Body: Hey {contact_name.split()[0] if contact_name else 'there'}, saw that {company_name} does [specific thing]. Curious — are you still running quotes through spreadsheets? We built a tool for a shop like yours that got their turnaround from 2 days to 20 minutes. Would a 15-min call make sense to see if something similar could work for you guys?

Return ONLY valid JSON: {{"subject": "...", "body": "...", "follow_up_suggestion": "..."}}"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            result = self._parse_json_response(response_text)
            return {
                "subject": result.get("subject", ""),
                "body": result.get("body", ""),
                "follow_up_suggestion": result.get("follow_up_suggestion", ""),
                "model": self.model,
            }
        except json.JSONDecodeError as e:
            logger.error("Failed to parse AI response as JSON: %s", str(e))
            return {"error": "Failed to parse AI response", "raw": response_text}
        except Exception as e:
            logger.error("AI email generation failed: %s", str(e))
            return {"error": str(e)}

    async def improve_email(self, subject: str, body: str, instruction: str = "") -> dict:
        """Improve an existing email draft using Claude."""
        try:
            instr_line = "General improvement: better subject line, tighter copy, stronger CTA."
            if instruction:
                instr_line = f"Specific instructions: {instruction}"

            prompt = f"""Tighten up this outreach email. Make it sound like a real person wrote it quickly, not like a committee drafted it.

Current Subject: {subject}
Current Body:
{body}

{instr_line}

What to do:
- Cut it to under 100 words. Kill any sentence that doesn't earn its spot
- Subject line: short, lowercase, something you'd actually type — not a headline
- Read it out loud — if you wouldn't say it to someone at a coffee shop, rewrite it
- Strip out anything corporate-sounding. Contractions are good. Dashes are fine
- One clear ask at the end
- Keep the core message, just make it sound human

Return ONLY valid JSON: {{"subject": "...", "body": "...", "changes_made": "..."}}"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("AI email improvement failed: %s", str(e))
            return {"error": str(e)}

    async def score_lead(
        self,
        company_name: str,
        industry: str,
        employee_count: Optional[int],
        location: str,
        engagement_history: str = "",
        notes: str = "",
    ) -> dict:
        """Use Claude to intelligently score a lead."""
        try:
            emp_str = str(employee_count) if employee_count else "Unknown"
            eng_str = engagement_history if engagement_history else "None yet"
            notes_str = notes if notes else "None"

            prompt = f"""Score this B2B lead on a scale of 1-100 and explain why.

Company: {company_name}
Industry: {industry}
Employee Count: {emp_str}
Location: {location}
Engagement History: {eng_str}
Notes: {notes_str}

Context: We are Apprend Technologies, a custom manufacturing software company. Our sweet spot:
- Mid-size manufacturers (50-500 employees)
- Industries: plastics, CNC machining, injection molding, metal stamping, aerospace parts, marine parts
- US and Canada
- Companies still using spreadsheets, paper, or outdated ERP systems
- Decision makers: VPs of Operations, Plant Managers, CTOs, Owners

Scoring criteria:
- Industry relevance (is this a manufacturer we would want to work with?)
- Company size (sweet spot is 50-500 employees)
- Location (US/Canada preferred)
- Engagement signals (opens, clicks, replies are positive)
- Title/role fit of contacts

Return ONLY valid JSON: {{"score": 75, "grade": "A/B/C/D/F", "reasoning": "...", "recommended_action": "...", "priority": "high/medium/low"}}"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("AI lead scoring failed: %s", str(e))
            return {"error": str(e)}

    async def analyze_company(self, company_data: dict) -> dict:
        """Analyze a company and suggest outreach strategy."""
        try:
            company_json = json.dumps(company_data, indent=2, default=str)
            prompt = f"""Analyze this company for B2B outreach and suggest a strategy.

Company Data:
{company_json}

Context: We are Apprend Technologies, a custom manufacturing software company. We build quoting tools, ERP integrations, shop floor dashboards, and quality tracking systems for mid-size manufacturers. Analyze:
1. How good a fit is this company for our services?
2. What is the best approach to reach out?
3. What pain points might they have that we could address?
4. Who should we target (which roles)?
5. What messaging angle would resonate?

Return ONLY valid JSON:
{{
  "fit_score": 1,
  "fit_reasoning": "...",
  "recommended_approach": "...",
  "likely_pain_points": ["..."],
  "target_roles": ["..."],
  "messaging_angle": "...",
  "suggested_subject_line": "...",
  "red_flags": ["..."]
}}"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("AI company analysis failed: %s", str(e))
            return {"error": str(e)}

    async def generate_followup(
        self,
        original_subject: str,
        original_body: str,
        step_number: int,
        contact_name: str,
        days_since_last: int,
    ) -> dict:
        """Generate an intelligent follow-up email that does not feel spammy."""
        try:
            is_final = step_number >= 3
            final_note = "This is the final follow-up. Make it a graceful breakup email — keep it classy, leave the door open." if is_final else ""

            prompt = f"""Write follow-up #{step_number} for a cold email I sent {days_since_last} days ago.

Original subject: {original_subject}
Contact: {contact_name}

THE GOAL: Get a reply. Not annoy them.

RULES:
1. Under 50 words. Seriously — the shorter the follow-up, the higher the reply rate
2. Follow-up #1: Float the thread back up. Add one new thing — a quick stat, a thought about their industry, or the demo link. Keep it casual. "Hey {contact_name.split()[0] if contact_name else 'there'}, wanted to bump this" energy
3. Follow-up #2: Completely different angle. Don't reference the old email. Try a specific pain point, a result from a similar shop, or a question about their process
4. Follow-up #3: Breakup. "Totally fine if the timing's off — just didn't want to keep pinging you without checking." Two sentences. Leave the door open. Done
5. After #3: Set should_stop_sequence to true
6. NEVER say: "just checking in", "circling back", "following up on my previous email", "touching base", "wanted to circle back"
7. Sound like you dashed this off in 30 seconds, because you did
{final_note}

Return ONLY valid JSON: {{"subject": "...", "body": "...", "is_final": {str(is_final).lower()}, "should_stop_sequence": {str(is_final).lower()}}}"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("AI follow-up generation failed: %s", str(e))
            return {"error": str(e)}

    async def analyze_reply(self, reply_body: str, original_subject: str, original_body: str, contact_name: str, company_name: str) -> dict:
        """Analyze a lead's reply to understand intent and draft a response focused on scheduling appointments."""
        try:
            settings = get_settings()
            scheduling_link = getattr(settings, "scheduling_link", "")
            scheduling_instruction = ""
            if scheduling_link:
                scheduling_instruction = f"""
IMPORTANT: We have a scheduling link available: {scheduling_link}
Include this link naturally in your response when suggesting a call or meeting."""

            prompt = f"""You're Suman. Someone replied to your cold email. Read what they said and write back.

THEIR REPLY:
{reply_body[:1000]}

YOUR ORIGINAL EMAIL:
Subject: {original_subject}

Who they are: {contact_name} at {company_name}

{scheduling_instruction}

FIRST: Figure out what they mean:
- "interested" = they want to learn more or talk
- "question" = they asked something specific
- "not_now" = bad timing but not a no
- "not_interested" = clear no
- "referral" = pointing you to someone else
- "auto_reply" = out of office / automated

THEN write your reply:

- MAX 60 words. You're replying from your phone
- Match their vibe. Casual reply to a casual email. Slightly more polished if they're formal
- Get to the point immediately. No "thanks for getting back to me" throat-clearing
- Interested/question: suggest hopping on a quick call. Ask what times work for them, or propose 2-3 slots
- Not now: "totally get it" + when should you check back? One sentence
- Not interested: "appreciate you letting me know" and done. Don't grovel
- Auto-reply: don't respond at all. Set should_auto_send to false
- Sign off: "- Suman" or just "Suman"

Return ONLY valid JSON:
{{
  "intent": "interested|not_now|not_interested|question|referral|auto_reply",
  "confidence": 0.0,
  "summary": "one line summary",
  "draft_reply": {{
    "subject": "Re: ...",
    "body": "the reply"
  }},
  "should_send": true,
  "should_auto_send": true,
  "reasoning": "why",
  "suggested_action": "schedule_meeting|follow_up_later|close_lead|ask_for_referral_intro|no_action",
  "follow_up_days": null
}}

Auto-send: true ONLY for "interested" and "question". false for everything else. NEVER for "auto_reply".
"""

            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("AI reply analysis failed: %s", str(e))
            return {"error": str(e), "should_send": False, "should_auto_send": False}

    # ------------------------------------------------------------------
    # Sales tactics / playbook generators
    #
    # These power the frontend "Tactics" tab — multi-channel outreach assets
    # for a single lead. All return plain dicts (never raise) and share the
    # same Apprend voice rules as the cold-email generator above.
    # ------------------------------------------------------------------

    _VOICE_RULES = (
        "VOICE: Write like a real person, not a marketing team. Short, concrete, "
        "conversational (contractions OK). NO buzzwords (leverage, synergy, "
        "cutting-edge, revolutionary, transform, streamline). NO generic openers "
        '("I hope this finds you well", "I came across"). One clear ask.'
    )

    async def linkedin_messages(
        self,
        company_name: str,
        contact_name: str,
        contact_title: str = "",
        company_industry: str = "",
        company_domain: str = "",
        topic: str = "",
    ) -> dict:
        """Draft LinkedIn connection note, follow-up, and a post-comment."""
        topic_line = f"Angle/topic to lead with: {topic}" if topic else ""
        prompt = f"""You are Suman from Apprend Technologies (custom manufacturing software —
quoting tools, ERP integrations, shop-floor dashboards, quality tracking).

Write LinkedIn outreach to:
Name: {contact_name}
Title: {contact_title}
Company: {company_name}
Industry: {company_industry}
{topic_line}

{self._VOICE_RULES}

Produce three assets:
1. connection_note: a connection request note, HARD MAX 300 characters, no link.
2. follow_up_message: a short message to send after they accept (2-3 sentences, one soft ask).
3. comment_draft: a thoughtful comment to leave on one of their recent posts (1-2 sentences, adds value, not salesy).

Return ONLY valid JSON: {{"connection_note": "...", "follow_up_message": "...", "comment_draft": "..."}}"""
        return await self._complete_json(prompt, max_tokens=768)

    async def phone_script(
        self,
        company_name: str,
        contact_name: str,
        contact_title: str = "",
        company_industry: str = "",
        product: str = "",
    ) -> dict:
        """Build a cold-call script: opener, value prop, discovery, objections, close, voicemail."""
        product_line = f"Product/service to pitch: {product}" if product else "Pitch our custom manufacturing software broadly."
        prompt = f"""You are coaching Suman from Apprend Technologies for a cold call.

Prospect: {contact_name}, {contact_title} at {company_name} ({company_industry}).
{product_line}

{self._VOICE_RULES}
Keep every spoken line natural and brief — this is talked, not read aloud.

Return ONLY valid JSON with these fields:
{{
  "opener": "first 2 sentences when they pick up — pattern-interrupt, not 'how are you'",
  "value_proposition": "one tight sentence on the outcome we deliver for shops like theirs",
  "discovery_questions": ["3-5 open questions about their current process/pain"],
  "objection_responses": {{"we already have a system": "...", "no budget": "...", "send me an email": "..."}},
  "close": "the specific ask — a 15-min call/demo",
  "voicemail_script": "a 15-second voicemail if they don't answer"
}}"""
        return await self._complete_json(prompt, max_tokens=1024)

    async def referral_request(
        self,
        company_name: str,
        referrer_name: str,
        target_name: str,
        target_title: str = "",
        company_industry: str = "",
    ) -> dict:
        """Draft a warm-intro request to a mutual connection + a forwardable blurb."""
        prompt = f"""You are Suman from Apprend Technologies (custom manufacturing software).

You want an introduction to {target_name} ({target_title}) at {company_name} ({company_industry}).
You'll ask {referrer_name}, a mutual connection, to make the intro.

{self._VOICE_RULES}

Produce:
1. referral_email: a short note to {referrer_name} asking for the intro — make it EASY to say yes, give context, no pressure.
2. forwardable_blurb: a 2-3 sentence blurb {referrer_name} can paste/forward straight to {target_name} describing who you are and why it's worth 15 minutes.

Return ONLY valid JSON: {{"referral_email": "...", "forwardable_blurb": "..."}}"""
        return await self._complete_json(prompt, max_tokens=768)

    async def event_outreach(
        self,
        company_name: str,
        contact_name: str,
        event_name: str,
        event_type: str = "conference",
        company_industry: str = "",
    ) -> dict:
        """Draft a 3-touch event sequence: pre-event, at-event, post-event."""
        prompt = f"""You are Suman from Apprend Technologies (custom manufacturing software).

You and {contact_name} from {company_name} ({company_industry}) will both be at
{event_name} (a {event_type}). Write a 3-touch sequence around the event.

{self._VOICE_RULES}

Produce:
1. pre_event_email: before the event — suggest meeting up / grabbing coffee there.
2. at_event_message: a short text/LinkedIn message to send during the event to connect in person.
3. post_event_followup: after the event — reference the event, propose next step.

Return ONLY valid JSON: {{"pre_event_email": "...", "at_event_message": "...", "post_event_followup": "..."}}"""
        return await self._complete_json(prompt, max_tokens=1024)

    async def multi_touch_plan(
        self,
        company_name: str,
        company_industry: str = "",
        employee_count: Optional[int] = None,
        location: str = "",
        contacts_summary: str = "",
        fit_score: Optional[int] = None,
    ) -> dict:
        """Generate a sequenced multi-channel campaign plan for one company."""
        emp = str(employee_count) if employee_count else "Unknown"
        score_line = f"Current fit score: {fit_score}/100." if fit_score is not None else ""
        prompt = f"""You are a B2B sales strategist for Apprend Technologies (custom manufacturing
software — quoting, ERP integrations, shop-floor dashboards, quality tracking).

Design a realistic multi-channel outreach campaign for this account:
Company: {company_name}
Industry: {company_industry}
Employees: {emp}
Location: {location}
Known contacts: {contacts_summary or 'none yet — discovery needed'}
{score_line}

Mix channels (email, linkedin, phone, referral) across ~2-4 weeks. Be specific and
sequenced — each step builds on the last. {self._VOICE_RULES}

Return ONLY valid JSON:
{{
  "summary": "one-paragraph strategy overview tailored to this account",
  "steps": [
    {{"channel": "email|linkedin|phone|referral", "timing": "e.g. Day 1", "action": "exactly what to do/say"}}
  ]
}}
Provide 5-8 steps."""
        return await self._complete_json(prompt, max_tokens=1500)

    async def extract_business_card(self, image_base64: str, media_type: str = "image/jpeg") -> dict:
        """Extract contact information from a business card image using Claude vision.

        Returns the extracted fields plus a ``_confidence`` map (field -> 0..1)
        and an ``_overall_confidence`` score so the UI can flag fields that need
        a human check. Handles international/multilingual cards and multiple
        phone numbers in mixed formats. Never raises — returns ``{"error": ...}``
        on failure so the request handler can surface it cleanly.
        """
        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1536,
                temperature=0,  # deterministic transcription, not creative writing
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {
                            "type": "text",
                            "text": """You are a precise OCR engine for business cards. Extract every piece of contact information from this card image.

Return ONLY valid JSON with these fields (use an empty string "" if a field is not present on the card):
{
  "first_name": "",
  "last_name": "",
  "title": "",
  "department": "",
  "company_name": "",
  "email": "",
  "phone": "",
  "mobile": "",
  "fax": "",
  "phones": [{"label": "", "number": ""}],
  "website": "",
  "address": "",
  "city": "",
  "state": "",
  "postal_code": "",
  "country": "",
  "linkedin_url": "",
  "notes": "",
  "_confidence": {},
  "_overall_confidence": 0.0
}

Extraction rules:
- PHONES: Capture EVERY phone number. Put the main/office number in "phone", a
  cell/mobile number in "mobile", and a fax number in "fax". List ALL numbers
  (including those three) in the "phones" array, each with a short label such as
  "Office", "Mobile", "Fax", "Direct", "Toll-free", "Tel", "WhatsApp" based on
  the card's own labels/icons. If a number has no label, use "Phone". Preserve
  international formats verbatim, INCLUDING the country code and "+" (e.g.
  "+49 30 1234567", "+81-3-1234-5678"). Do not reformat or strip digits.
- NAMES: Split the person's name into first/last. For names written
  family-name-first (common on East-Asian cards), still map the personal/given
  name to "first_name" and the family name to "last_name". If the card shows the
  name in a non-Latin script with a Latin transliteration, use the Latin form.
- MULTILINGUAL: Cards may be in any language. Translate the FIELD MEANINGS, not
  the values — keep proper nouns (names, company, street) as printed; you may
  normalize the country to its common English name.
- ADDRESS: "address" is the street line(s) only; put city, state/province,
  ZIP/postal code and country in their own fields. Map regions sensibly
  (e.g. a German "PLZ" is the postal_code).
- EMAIL/WEB: Fix obvious OCR slips in emails/URLs only when unambiguous (e.g.
  a clearly-misread "@"); otherwise transcribe exactly. Do not invent domains.
- Be thorough — read ALL text including small print, taglines and back-of-card
  details. Put anything useful that has no field (slogan, hours, second person)
  into "notes".

Confidence rules:
- "_confidence" maps EACH field you populated (non-empty) to a number from 0 to
  1 reflecting how sure you are it is correct and legible (1 = crystal clear,
  0.5 = partly obscured/ambiguous, <0.3 = a guess). Omit fields you left empty.
- "_overall_confidence" is your overall read quality for the whole card (0..1),
  factoring in image clarity, glare, blur and how much you had to guess.""",
                        },
                    ],
                }],
            )

            result = self._parse_json_response(message.content[0].text)
            if not isinstance(result, dict):
                return {"error": "Failed to parse extracted data"}
            return result
        except json.JSONDecodeError as e:
            logger.error("Failed to parse card extraction: %s", str(e))
            return {"error": "Failed to parse extracted data"}
        except Exception as e:
            logger.error("Business card extraction failed: %s", str(e))
            return {"error": str(e)}


_ai_service = None


def get_ai_service() -> AIService:
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService()
    return _ai_service
