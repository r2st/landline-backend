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
        """Parse JSON from Claude response, handling markdown code blocks."""
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)

    async def generate_email(
        self,
        company_name: str,
        contact_name: str,
        contact_title: str,
        industry: str,
        purpose: str = "initial outreach",
        tone: str = "professional but warm",
        additional_context: str = "",
    ) -> dict:
        """Generate a personalized email using Claude."""
        try:
            ctx_line = ""
            if additional_context:
                ctx_line = f"Additional Context: {additional_context}"

            prompt = f"""You are writing an email AS Suman from Apprend Technologies. Apprend builds custom manufacturing software — quoting tools, ERP integrations, shop floor dashboards, quality tracking systems.

Write a cold outreach email to this person:

Name: {contact_name}
Title: {contact_title}  
Company: {company_name}
Industry: {industry}
{ctx_line}

CRITICAL RULES — your email MUST follow these:
1. SHORT. Under 100 words for the body. 4-5 sentences max.
2. Sound like a real person texting a colleague, not a marketing team.
3. Start with something SPECIFIC about their company or role — show you did homework.
4. NO buzzwords: no "leverage", "synergy", "cutting-edge", "revolutionary", "transform".
5. NO generic openers: no "I hope this finds you well", "I came across your company", "I noticed that".
6. Use conversational language — contractions, casual tone, like you'd talk to someone at a trade show.
7. One clear ask at the end — suggest a quick 15-min call.
8. Subject line: short, lowercase-ish, looks like a real person wrote it (not a marketing campaign).

GOOD example:
Subject: quick question about your quoting process
Body: Hey {{{{first_name}}}}, saw that {{{{company}}}} does [specific thing]. Curious — are you still using spreadsheets or a manual process for quoting? We built a tool for [similar company] that cut their quote turnaround from 2 days to 20 minutes. Would a 15-min call make sense to see if something similar could work for you guys?

BAD example (DO NOT write like this):
Subject: Transform Your Manufacturing Operations with AI-Powered Solutions  
Body: Dear {{{{first_name}}}}, I hope this email finds you well. I came across {{{{company}}}} and was impressed by your operations. At Apprend Technologies, we leverage cutting-edge technology to help manufacturers like you streamline their processes...

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

            prompt = f"""Improve this B2B outreach email. Make it more engaging, concise, and likely to get a response.

Current Subject: {subject}
Current Body:
{body}

{instr_line}

Requirements:
- Keep it under 100 words
- Make the subject line short, lowercase, like a real person wrote it
- Sound human and genuine — contractions, casual tone
- No buzzwords, no corporate speak
- One clear CTA

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

            prompt = f"""Write follow-up #{step_number} for a cold email I sent.

Original subject: {original_subject}
Contact: {contact_name}
Days since last email: {days_since_last}

RULES:
1. Under 50 words. Yes, fifty.
2. Follow-up #1: Bump the thread. "Hey, just floating this back up" energy. Add ONE new thing of value.
3. Follow-up #2: Different angle entirely. Share something useful (industry trend, case study result, a relevant number).
4. Follow-up #3: Breakup email. "No worries if the timing isn't right" — keep it classy, leave the door open.
5. After #3: STOP. Set should_stop_sequence to true.
6. DO NOT say "just checking in", "circling back", "following up", "touching base".
7. Sound like a real person, not a drip campaign.
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

            prompt = f"""You're Suman from Apprend Technologies responding to a lead's reply. Your goal is to get a 15-20 minute call scheduled.

CONTEXT:
- You sent them a cold email about manufacturing software
- They just replied
- You want to schedule a quick call to understand their process and show relevant demos

ORIGINAL EMAIL WE SENT:
Subject: {original_subject}

THEIR REPLY:
{reply_body[:1000]}

Contact: {contact_name}
Company: {company_name}

{scheduling_instruction}

CLASSIFY their intent: "interested", "not_now", "not_interested", "question", "referral", "auto_reply"

RESPONSE RULES:
1. MAX 60 words. Seriously — short replies get more responses.
2. Match their energy. If they're casual, be casual. If formal, be slightly formal.
3. Sound like you typed this on your phone between meetings.
4. NO corporate speak. Write like a human.
5. For interested/question: propose 2-3 specific time slots or ask when works for them.
6. For not_now: "totally understand" + leave door open. ONE sentence.
7. For not_interested: "appreciate you letting me know" + done. TWO sentences max.
8. For auto_reply: do NOT respond. Set should_auto_send to false.
9. NEVER say "I appreciate your prompt response" or "Thank you for getting back to me" — just get to the point.
10. Sign off casually: "- Suman" or just "Suman"

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

Auto-send rules:
- true ONLY for "interested" and "question"
- false for everything else
- NEVER true for "auto_reply"
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

    async def extract_business_card(self, image_base64: str, media_type: str = "image/jpeg") -> dict:
        """Extract contact information from a business card image using Claude vision."""
        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
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
                            "text": """Extract all contact information from this business card image.

Return ONLY valid JSON with these fields (use empty string if not found):
{
  "first_name": "",
  "last_name": "",
  "title": "",
  "company_name": "",
  "email": "",
  "phone": "",
  "website": "",
  "address": "",
  "city": "",
  "state": "",
  "country": "",
  "linkedin_url": "",
  "notes": ""
}

Be thorough -- check for all text on the card including small print.""",
                        },
                    ],
                }],
            )

            response_text = message.content[0].text.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
                response_text = response_text.strip()

            return json.loads(response_text)
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
