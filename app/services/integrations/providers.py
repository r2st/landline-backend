"""Concrete marketplace connectors (feature 4.2).

Priority integrations from the spec:

* **Slack** — post notifications to a channel via an incoming webhook.
* **Google Calendar** — create meetings via the Calendar API (OAuth).
* **HubSpot** / **Salesforce** — CRM contact sync (migration + ongoing).
* **Clearbit** / **ZoomInfo** — company/person enrichment.

Each connector performs a real HTTP call when credentials are present and
returns a :class:`ConnectorResult`. With missing credentials or an offline
provider it returns a failure result rather than raising, so callers (the
marketplace router and workflow actions) degrade gracefully.
"""

from __future__ import annotations

import logging

import httpx

from app.services.integrations.base import BaseConnector, ConnectorResult

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)


class SlackConnector(BaseConnector):
    provider = "slack"
    display_name = "Slack"
    description = "Post lead and deal notifications to a Slack channel."
    category = "communication"
    auth_type = "webhook"
    config_fields = ["default_channel"]
    credential_fields = ["webhook_url"]
    supported_actions = ["send_notification"]

    async def test_connection(self, config: dict, credentials: dict) -> ConnectorResult:
        if not credentials.get("webhook_url"):
            return ConnectorResult.failure("Missing Slack webhook_url")
        return ConnectorResult.success("Slack webhook configured")

    async def _action_send_notification(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        url = credentials.get("webhook_url")
        if not url:
            return ConnectorResult.failure("Missing Slack webhook_url")
        text = params.get("text") or params.get("message") or ""
        if not text:
            return ConnectorResult.failure("Nothing to send: empty text")
        payload = {"text": text}
        if params.get("channel") or config.get("default_channel"):
            payload["channel"] = params.get("channel") or config["default_channel"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return ConnectorResult.success("Sent to Slack")
        return ConnectorResult.failure(
            f"Slack returned {resp.status_code}", status_code=resp.status_code
        )


class GoogleCalendarConnector(BaseConnector):
    provider = "google_calendar"
    display_name = "Google Calendar"
    description = "Create meetings and hold slots on a connected Google Calendar."
    category = "productivity"
    auth_type = "oauth"
    config_fields = ["calendar_id"]
    credential_fields = ["access_token"]
    supported_actions = ["create_event"]

    async def _action_create_event(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        token = credentials.get("access_token")
        if not token:
            return ConnectorResult.failure("Missing Google OAuth access_token")
        calendar_id = params.get("calendar_id") or config.get("calendar_id") or "primary"
        body = {
            "summary": params.get("summary", "Landline meeting"),
            "description": params.get("description", ""),
            "start": {"dateTime": params["start"]},
            "end": {"dateTime": params["end"]},
        }
        if params.get("attendees"):
            body["attendees"] = [{"email": e} for e in params["attendees"]]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            return ConnectorResult.success(
                "Event created", event_id=data.get("id"), html_link=data.get("htmlLink")
            )
        return ConnectorResult.failure(
            f"Calendar API returned {resp.status_code}", status_code=resp.status_code
        )


class HubSpotConnector(BaseConnector):
    provider = "hubspot"
    display_name = "HubSpot"
    description = "Two-way contact sync and migration with HubSpot CRM."
    category = "crm"
    auth_type = "api_key"
    config_fields = ["sync_direction"]
    credential_fields = ["access_token"]
    supported_actions = ["sync_contact", "fetch_contacts"]

    # Properties asked for by name: HubSpot returns only ``createdate``,
    # ``lastmodifieddate`` and ``hs_object_id`` unless you say otherwise, so an
    # unqualified fetch comes back with no email on it at all.
    _PROPERTIES = (
        "email", "firstname", "lastname", "company", "phone", "jobtitle",
        "website", "city", "state", "country",
    )

    async def _action_fetch_contacts(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        """Read a page of contacts *out* of HubSpot.

        Returns provider-neutral rows (see ``app.services.crm_import``) plus the
        opaque ``cursor`` for the next page — paging is the caller's loop, not
        this method's, so one connector call is one HTTP request and a caller
        can stop at whatever budget it has.
        """
        token = credentials.get("access_token")
        if not token:
            return ConnectorResult.failure("Missing HubSpot access_token")
        query: dict = {
            "limit": max(1, min(int(params.get("limit") or 100), 100)),
            "properties": ",".join(self._PROPERTIES),
        }
        if params.get("cursor"):
            query["after"] = params["cursor"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {token}"},
                params=query,
            )
        if resp.status_code != 200:
            return ConnectorResult.failure(
                f"HubSpot returned {resp.status_code}", status_code=resp.status_code
            )
        payload = resp.json() or {}
        records = []
        for row in payload.get("results") or []:
            props = row.get("properties") or {}
            records.append({
                "external_id": row.get("id"),
                "email": props.get("email"),
                "first_name": props.get("firstname"),
                "last_name": props.get("lastname"),
                "title": props.get("jobtitle"),
                "phone": props.get("phone"),
                "company": props.get("company"),
                "website": props.get("website"),
                "city": props.get("city"),
                "state": props.get("state"),
                "country": props.get("country"),
            })
        cursor = ((payload.get("paging") or {}).get("next") or {}).get("after")
        return ConnectorResult.success(
            f"Fetched {len(records)} HubSpot contacts",
            records=records,
            cursor=cursor,
        )

    async def _action_sync_contact(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        token = credentials.get("access_token")
        if not token:
            return ConnectorResult.failure("Missing HubSpot access_token")
        email = params.get("email")
        if not email:
            return ConnectorResult.failure("Contact email required")
        props = {"email": email}
        for k in ("firstname", "lastname", "company", "phone", "jobtitle"):
            if params.get(k):
                props[k] = params[k]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {token}"},
                json={"properties": props},
            )
        if resp.status_code in (200, 201):
            return ConnectorResult.success("Contact synced to HubSpot", id=resp.json().get("id"))
        if resp.status_code == 409:
            return ConnectorResult.success("Contact already exists in HubSpot")
        return ConnectorResult.failure(
            f"HubSpot returned {resp.status_code}", status_code=resp.status_code
        )


class SalesforceConnector(BaseConnector):
    provider = "salesforce"
    display_name = "Salesforce"
    description = "Contact and lead sync/migration with Salesforce CRM."
    category = "crm"
    auth_type = "oauth"
    config_fields = ["instance_url", "sync_direction"]
    credential_fields = ["access_token"]
    supported_actions = ["sync_contact", "fetch_contacts"]

    _API = "v59.0"
    # Leads, not Contacts, to mirror the push side: ``sync_contact`` creates a
    # Lead, so pulling Leads makes the round trip idempotent instead of writing
    # to one object and reading from another.
    _SOQL = (
        "SELECT Id, FirstName, LastName, Email, Phone, Title, Company, Website, "
        "City, State, Country, NumberOfEmployees FROM Lead"
    )

    async def _action_fetch_contacts(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        """Read a page of Leads *out* of Salesforce.

        Salesforce hands back a whole path in ``nextRecordsUrl`` rather than an
        opaque token, so the cursor here is that path; the caller passes it back
        untouched, same as HubSpot's ``after``.
        """
        token = credentials.get("access_token")
        instance = config.get("instance_url")
        if not token or not instance:
            return ConnectorResult.failure("Missing Salesforce access_token / instance_url")
        base = instance.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        cursor = params.get("cursor")
        limit = max(1, min(int(params.get("limit") or 200), 2000))
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if cursor:
                resp = await client.get(f"{base}{cursor}", headers=headers)
            else:
                resp = await client.get(
                    f"{base}/services/data/{self._API}/query",
                    headers=headers,
                    params={"q": f"{self._SOQL} LIMIT {limit}"},
                )
        if resp.status_code != 200:
            return ConnectorResult.failure(
                f"Salesforce returned {resp.status_code}", status_code=resp.status_code
            )
        payload = resp.json() or {}
        records = []
        for row in payload.get("records") or []:
            records.append({
                "external_id": row.get("Id"),
                "email": row.get("Email"),
                "first_name": row.get("FirstName"),
                "last_name": row.get("LastName"),
                "title": row.get("Title"),
                "phone": row.get("Phone"),
                "company": row.get("Company"),
                "website": row.get("Website"),
                "city": row.get("City"),
                "state": row.get("State"),
                "country": row.get("Country"),
                "employee_count": row.get("NumberOfEmployees"),
            })
        return ConnectorResult.success(
            f"Fetched {len(records)} Salesforce leads",
            records=records,
            cursor=payload.get("nextRecordsUrl"),
        )

    async def _action_sync_contact(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        token = credentials.get("access_token")
        instance = config.get("instance_url")
        if not token or not instance:
            return ConnectorResult.failure("Missing Salesforce access_token / instance_url")
        body = {"Email": params.get("email")}
        if params.get("lastname"):
            body["LastName"] = params["lastname"]
        if params.get("company"):
            body["Company"] = params["company"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{instance.rstrip('/')}/services/data/v59.0/sobjects/Lead",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        if resp.status_code in (200, 201):
            return ConnectorResult.success("Lead synced to Salesforce", id=resp.json().get("id"))
        return ConnectorResult.failure(
            f"Salesforce returned {resp.status_code}", status_code=resp.status_code
        )


class ClearbitConnector(BaseConnector):
    provider = "clearbit"
    display_name = "Clearbit"
    description = "Enrich companies and people with Clearbit firmographic data."
    category = "enrichment"
    auth_type = "api_key"
    config_fields = []
    credential_fields = ["api_key"]
    supported_actions = ["enrich_company"]

    async def _action_enrich_company(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        api_key = credentials.get("api_key")
        domain = params.get("domain")
        if not api_key:
            return ConnectorResult.failure("Missing Clearbit api_key")
        if not domain:
            return ConnectorResult.failure("Company domain required")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://company.clearbit.com/v2/companies/find",
                params={"domain": domain},
                auth=(api_key, ""),
            )
        if resp.status_code == 200:
            return ConnectorResult.success("Company enriched", company=resp.json())
        return ConnectorResult.failure(
            f"Clearbit returned {resp.status_code}", status_code=resp.status_code
        )


class ZoomInfoConnector(BaseConnector):
    provider = "zoominfo"
    display_name = "ZoomInfo"
    description = "Enrich companies and contacts with ZoomInfo B2B data."
    category = "enrichment"
    auth_type = "api_key"
    config_fields = []
    credential_fields = ["access_token"]
    supported_actions = ["enrich_company"]

    async def _action_enrich_company(
        self, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        token = credentials.get("access_token")
        domain = params.get("domain")
        if not token:
            return ConnectorResult.failure("Missing ZoomInfo access_token")
        if not domain:
            return ConnectorResult.failure("Company domain required")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.zoominfo.com/enrich/company",
                headers={"Authorization": f"Bearer {token}"},
                json={"matchCompanyInput": [{"companyWebsite": domain}]},
            )
        if resp.status_code == 200:
            return ConnectorResult.success("Company enriched", company=resp.json())
        return ConnectorResult.failure(
            f"ZoomInfo returned {resp.status_code}", status_code=resp.status_code
        )
