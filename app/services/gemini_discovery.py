"""Company discovery search on the org's Gemini key, grounded on Google Search.

The alternative to Apollo's company search (which the Apollo Free plan refuses
with a 403). Gemini searches the web for companies matching the operator's
criteria and answers with JSON; this module turns that answer into the same
:class:`~app.services.ingestion.CandidateRecord` shape the Apollo path feeds the
ingestion service, so both sources dedup and persist under one rule.

**Emails are the field to distrust.** A language model asked for a contact's
email will happily produce a plausible ``first.last@domain`` it never saw. An
invented address that bounces costs the sending domain reputation, so an email
is kept only when it sits on the company's own domain — and the prompt asks for
published addresses only. Names, titles and LinkedIn URLs are kept as given;
they cost nothing if wrong until a human acts on them.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import org_gemini
from app.services.ingestion import CandidateRecord, ContactRecord
from app.services.normalize import normalize_domain

logger = logging.getLogger(__name__)

SOURCE = "gemini"

SYSTEM = (
    "You are a B2B market researcher. Use Google Search to find real, currently "
    "operating companies that match the criteria. Never invent a company, a "
    "domain, a person or an email address: if you cannot find a value, use null. "
    "Only include an email when it is published on the web for that person or "
    "company. Answer with a single JSON object and nothing else."
)

_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


class GeminiContact(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    linkedin: Optional[str] = None


class GeminiCompany(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    industry: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    location: Optional[str] = None
    employee_count: Optional[Union[int, float, str]] = None
    description: Optional[str] = None
    contacts: list[GeminiContact] = Field(default_factory=list)

    @field_validator("contacts", mode="before")
    @classmethod
    def _contacts_list(cls, v: Any) -> Any:
        return v if isinstance(v, list) else []


class GeminiSearchAnswer(BaseModel):
    companies: list[GeminiCompany] = Field(default_factory=list)

    @field_validator("companies", mode="before")
    @classmethod
    def _companies_list(cls, v: Any) -> Any:
        return v if isinstance(v, list) else []


def build_prompt(
    *,
    query: str = "",
    industry: str,
    location: str,
    keywords: list[str],
    min_employees: Optional[int],
    max_employees: Optional[int],
    limit: int,
) -> str:
    criteria = []
    if industry.strip():
        criteria.append(f"- Industry: {industry.strip()}")
    if location.strip():
        criteria.append(f"- Location: {location.strip()}")
    kw = [k.strip() for k in keywords if k and k.strip()]
    if kw:
        criteria.append(f"- Keywords: {', '.join(kw)}")
    if min_employees is not None or max_employees is not None:
        lo = min_employees if min_employees is not None else 1
        hi = max_employees if max_employees is not None else "any"
        criteria.append(f"- Employees: {lo} to {hi}")
    query = (query or "").strip()
    if query:
        # The operator's own description is the primary input; the structured
        # filters only narrow it.
        head = f"Find up to {limit} companies matching this description:\n{query}\n"
        if criteria:
            head += "\nThey must also satisfy these filters:\n" + "\n".join(criteria) + "\n"
    else:
        if not criteria:
            criteria.append("- Any industry and location")
        head = f"Find up to {limit} companies matching:\n" + "\n".join(criteria) + "\n"
    return (
        head
        + "\nFor each, list up to 3 key decision-makers (founder, CEO, owner, "
        "or heads of operations, sales or purchasing).\n\n"
        "Return exactly this JSON shape:\n"
        '{"companies": [{"name": str, "domain": str (bare website domain, e.g. '
        '"acme.com"), "industry": str, "city": str, "state": str, "country": str, '
        '"employee_count": int (best estimate), "description": str (one sentence), '
        '"contacts": [{"name": str, "title": str, "email": str|null, '
        '"linkedin": str|null}]}]}'
    )


def parse_employee_count(raw: Any) -> Optional[int]:
    """``250``, ``"250"``, ``"1,200"``, ``"51-200"`` (upper bound), ``"500+"``."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(raw))]
    nums = [n for n in nums if n > 0]
    return max(nums) if nums else None


def _clean(v: Optional[str], limit: int = 255) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return v[:limit]


def _split_name(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    name = _clean(name)
    if not name:
        return None, None
    parts = name.split()
    return parts[0], (" ".join(parts[1:]) or None)


def _email_on_domain(email: Optional[str], domain: Optional[str]) -> Optional[str]:
    """Keep an email only when it is on the company's own domain (see module doc)."""
    email = _clean(email)
    if not email or not domain:
        return None
    m = _EMAIL.match(email.lower())
    if not m:
        return None
    host = m.group(1)
    return email.lower() if host == domain or host.endswith("." + domain) else None


def _linkedin(url: Optional[str]) -> Optional[str]:
    url = _clean(url, 500)
    if not url or "linkedin.com/" not in url.lower():
        return None
    return url if url.startswith("http") else f"https://{url.lstrip('/')}"


def to_records(
    answer: GeminiSearchAnswer,
    *,
    min_employees: Optional[int],
    max_employees: Optional[int],
    limit: int,
) -> tuple[list[CandidateRecord], dict[int, dict]]:
    """Validated records, plus display-only extras keyed by record ``id()``.

    Drops nameless entries, in-answer duplicates, and companies whose *known*
    employee count falls outside the requested range (an unknown count stays —
    a missing estimate is not evidence of a mismatch).
    """
    records: list[CandidateRecord] = []
    extras: dict[int, dict] = {}
    seen: set[str] = set()
    for company in answer.companies:
        name = _clean(company.name)
        if not name:
            continue
        domain = normalize_domain(_clean(company.domain))
        key = domain or name.lower()
        if key in seen:
            continue
        employees = parse_employee_count(company.employee_count)
        if employees is not None:
            if min_employees is not None and employees < min_employees:
                continue
            if max_employees is not None and employees > max_employees:
                continue
        seen.add(key)

        contacts: list[ContactRecord] = []
        for person in company.contacts[:5]:
            first, last = _split_name(person.name)
            email = _email_on_domain(person.email, domain)
            linkedin = _linkedin(person.linkedin)
            if not (first or email):
                continue
            contacts.append(ContactRecord(
                first_name=first,
                last_name=last,
                email=email,
                title=_clean(person.title),
                linkedin_url=linkedin,
                source=SOURCE,
                raw=person.model_dump(),
            ))

        city, state, country = _clean(company.city), _clean(company.state), _clean(company.country)
        location = _clean(company.location) or ", ".join(p for p in (city, state, country) if p) or None
        record = CandidateRecord(
            name=name,
            domain=domain,
            industry=_clean(company.industry),
            employee_count=employees,
            country=country,
            city=city,
            state=state,
            website=f"https://{domain}" if domain else None,
            contacts=contacts,
            source=SOURCE,
            raw=company.model_dump(),
        )
        records.append(record)
        extras[id(record)] = {
            "description": _clean(company.description, 1000),
            "location": location,
        }
        if len(records) >= limit:
            break
    return records, extras


async def search_companies(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    query: str = "",
    industry: str = "",
    location: str = "",
    keywords: Optional[list[str]] = None,
    min_employees: Optional[int] = None,
    max_employees: Optional[int] = None,
    limit: int = 10,
) -> tuple[list[CandidateRecord], dict[int, dict], list[dict]]:
    """Run the grounded search. Raises ``org_gemini.GeminiKeyMissing`` / ``GeminiError``."""
    prompt = build_prompt(
        query=query,
        industry=industry,
        location=location,
        keywords=keywords or [],
        min_employees=min_employees,
        max_employees=max_employees,
        limit=limit,
    )
    answer = await org_gemini.generate_grounded_json(
        db, org_id, system=SYSTEM, prompt=prompt, schema=GeminiSearchAnswer,
    )
    records, extras = to_records(
        answer.data, min_employees=min_employees, max_employees=max_employees, limit=limit,
    )
    logger.info(
        "Gemini discovery for org %s: %d returned, %d kept",
        org_id, len(answer.data.companies), len(records),
    )
    return records, extras, answer.sources
