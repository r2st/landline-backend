"""SQLAlchemy models package."""

from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.models.consent import ConsentRecord
from app.models.sequence import OutreachSequence, SequenceStep
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.interaction import Interaction
from app.models.email_template import EmailTemplate, EmailSend
from app.models.follow_up import FollowUp
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.models.company_news import CompanyNews
from app.models.deal import Deal
from app.models.task import Task
from app.models.client_health import ClientHealthSnapshot
from app.models.email_signature import EmailSignature

__all__ = [
    "Company",
    "Contact",
    "LeadScore",
    "ConsentRecord",
    "OutreachSequence",
    "SequenceStep",
    "OutreachEnrollment",
    "OutreachEvent",
    "Interaction",
    "EmailTemplate",
    "EmailSend",
    "FollowUp",
    "GmailAccount",
    "SmtpAccount",
    "CompanyNews",
    "Deal",
    "Task",
    "ClientHealthSnapshot",
    "EmailSignature",
]
