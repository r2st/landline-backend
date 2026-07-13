"""Voice intelligence: call recording transcription + analysis (feature 3.5).

Pipeline for a recorded cold call:

    recording (url or provided transcript)
        → transcribe (OpenAI Whisper, if configured — else use a provided transcript)
        → analyze (Claude → structured coaching + deal signals)
        → persist analysis on the CallRecording row

Transcription is optional infrastructure (VoIP/Whisper); analysis works from any
transcript, and ``analyze_transcript`` falls back to a deterministic heuristic
when Claude is unavailable so the feature degrades gracefully offline.
"""

import logging
import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.call_recording import CallRecording

logger = logging.getLogger(__name__)

_OBJECTION_CUES = (
    "too expensive", "no budget", "not interested", "already have", "call me back",
    "send me an email", "not a good time", "need to think", "talk to my", "not the right person",
)
_COMPETITOR_HINTS = ("competitor", "currently use", "we use", "switched from", "vendor")
_SPEAKER_RE = re.compile(r"^\s*(rep|agent|sales|caller|me|prospect|customer|client|buyer|them)\s*:", re.IGNORECASE)
_REP_LABELS = {"rep", "agent", "sales", "caller", "me"}


class VoiceIntelligence:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------
    async def transcribe(self, recording_url: str) -> Optional[str]:
        """Transcribe an audio recording via OpenAI Whisper, if configured.

        Returns None when no OpenAI key is set or the SDK/audio isn't available;
        callers should treat transcription as best-effort.
        """
        if not self.settings.openai_api_key or not recording_url:
            return None
        try:  # pragma: no cover - external network / SDK path
            import httpx
            from openai import AsyncOpenAI

            async with httpx.AsyncClient(timeout=60.0) as http:
                resp = await http.get(recording_url)
                resp.raise_for_status()
                audio_bytes = resp.content

            client = AsyncOpenAI(api_key=self.settings.openai_api_key)
            result = await client.audio.transcriptions.create(
                model=self.settings.whisper_model,
                file=("recording.mp3", audio_bytes),
            )
            return getattr(result, "text", None)
        except Exception as e:  # pragma: no cover
            logger.warning("Whisper transcription failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Talk / listen ratio
    # ------------------------------------------------------------------
    def talk_listen_ratio(self, transcript: str) -> Optional[float]:
        """Estimate rep-talk : prospect-talk from speaker-labeled lines.

        Returns rep_words / prospect_words (so >1 means the rep dominated), or
        None when the transcript has no usable speaker labels.
        """
        rep_words = prospect_words = 0
        current = None
        for line in (transcript or "").splitlines():
            m = _SPEAKER_RE.match(line)
            if m:
                label = m.group(1).lower()
                current = "rep" if label in _REP_LABELS else "prospect"
                line = line[m.end():]
            words = len(_tokenize(line))
            if current == "rep":
                rep_words += words
            elif current == "prospect":
                prospect_words += words
        if rep_words == 0 and prospect_words == 0:
            return None
        if prospect_words == 0:
            return round(float(rep_words), 2) if rep_words else None
        return round(rep_words / prospect_words, 2)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    async def analyze_transcript(self, transcript: str, context: str = "", use_ai: bool = True) -> dict:
        """Return structured analysis of a call transcript.

        Tries Claude first; falls back to a heuristic analysis so the result is
        always well-formed even offline.
        """
        transcript = (transcript or "").strip()
        ratio = self.talk_listen_ratio(transcript)
        if use_ai and transcript:
            ai = await self._ai_analyze(transcript, context)
            if ai:
                ai.setdefault("talk_listen_ratio", ratio)
                if ai.get("talk_listen_ratio") is None:
                    ai["talk_listen_ratio"] = ratio
                ai["_source"] = "ai"
                return ai
        result = self._heuristic_analyze(transcript)
        result["talk_listen_ratio"] = ratio
        result["_source"] = "heuristic"
        return result

    async def _ai_analyze(self, transcript: str, context: str) -> Optional[dict]:
        try:
            from app.services.ai_service import get_ai_service

            ctx = f"\nCall context: {context}\n" if context else ""
            prompt = (
                "You are a sales-call coach for Apprend Technologies (manufacturing software). "
                "Analyze this cold-call transcript and return ONLY JSON with this exact shape:\n"
                "{\n"
                '  "summary": "2-sentence summary",\n'
                '  "key_moments": ["..."],\n'
                '  "objections": [{"objection": "...", "how_handled": "...", "effectiveness": "poor|ok|good"}],\n'
                '  "questions_asked": ["..."],\n'
                '  "commitments": ["..."],\n'
                '  "competitors_mentioned": ["..."],\n'
                '  "action_items": [{"task": "...", "due_hint": "..."}],\n'
                '  "next_meeting": "ISO date or null",\n'
                '  "stakeholders": ["names/roles mentioned"],\n'
                '  "coaching": {"talk_listen_ratio": null, "question_quality": "1-10 + note", '
                '"objection_handling": "1-10 + note", "close_technique": "1-10 + note", "top_tip": "..."},\n'
                '  "deal_signals": {"sentiment": "positive|neutral|negative", "interest_level": "high|medium|low", "next_step": "..."}\n'
                "}\n\n"
                f"{ctx}TRANSCRIPT:\n{transcript[:6000]}"
            )
            data = await get_ai_service().complete_json(prompt, max_tokens=1500)
            if isinstance(data, dict) and "error" not in data:
                return data
            return None
        except Exception as e:  # pragma: no cover
            logger.warning("Voice AI analysis failed: %s", e)
            return None

    def _heuristic_analyze(self, transcript: str) -> dict:
        lower = transcript.lower()
        objections = [
            {"objection": cue, "how_handled": "", "effectiveness": "unknown"}
            for cue in _OBJECTION_CUES if cue in lower
        ]
        competitors = sorted({h for h in _COMPETITOR_HINTS if h in lower})
        questions = [
            s.strip() for s in re.split(r"(?<=[?])\s+", transcript) if s.strip().endswith("?")
        ][:10]
        sentiment = "positive" if any(w in lower for w in ("interested", "sounds good", "let's", "yes")) \
            else "negative" if any(w in lower for w in ("not interested", "no thanks", "stop")) else "neutral"
        return {
            "summary": "Heuristic analysis (AI unavailable). Review the transcript for detail.",
            "key_moments": [],
            "objections": objections,
            "questions_asked": questions,
            "commitments": [],
            "competitors_mentioned": competitors,
            "action_items": [],
            "next_meeting": None,
            "stakeholders": [],
            "coaching": {
                "question_quality": f"{len(questions)} questions detected",
                "objection_handling": f"{len(objections)} objection(s) surfaced",
                "close_technique": "n/a",
                "top_tip": "Enable AI analysis for detailed coaching.",
            },
            "deal_signals": {"sentiment": sentiment, "interest_level": "unknown", "next_step": ""},
        }

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def process_recording(
        self, db: AsyncSession, recording: CallRecording, context: str = "", use_ai: bool = True
    ) -> CallRecording:
        """Transcribe (if needed) + analyze a recording, updating it in place."""
        try:
            transcript = recording.transcription
            if not transcript and recording.recording_url:
                recording.status = "transcribing"
                await db.flush()
                transcript = await self.transcribe(recording.recording_url)
                if transcript:
                    recording.transcription = transcript
                    recording.transcription_source = "whisper"
            if not transcript:
                recording.status = "failed"
                recording.error = "no transcript available (provide transcription or configure Whisper)"
                await db.flush()
                return recording

            recording.status = "analyzing"
            await db.flush()
            analysis = await self.analyze_transcript(transcript, context=context, use_ai=use_ai)
            recording.analysis = analysis
            recording.talk_listen_ratio = analysis.get("talk_listen_ratio")
            recording.model_used = "claude-sonnet-4-6" if analysis.get("_source") == "ai" else "heuristic"
            recording.status = "analyzed"
            await db.flush()
            return recording
        except Exception as e:
            logger.exception("Recording processing failed")
            recording.status = "failed"
            recording.error = str(e)
            await db.flush()
            return recording


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text or "")


_voice: Optional[VoiceIntelligence] = None


def get_voice_intelligence() -> VoiceIntelligence:
    global _voice
    if _voice is None:
        _voice = VoiceIntelligence()
    return _voice
