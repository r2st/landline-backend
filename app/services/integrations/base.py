"""Standardized integration connector interface (feature 4.2).

Every marketplace provider is implemented as a :class:`BaseConnector` subclass.
A connector abstracts one external system behind three concerns:

* **auth** — one of ``oauth`` / ``api_key`` / ``webhook`` (``auth_type``);
* **connection test** — :meth:`test_connection` verifies stored credentials;
* **actions** — :meth:`execute` runs a named capability (send a Slack message,
  create a calendar event, sync a contact, enrich a company, …).

Connectors receive the integration's non-secret ``config`` and its decrypted
``credentials`` (the router decrypts before calling). They must never raise:
every path returns a :class:`ConnectorResult` so a misconfigured or offline
provider degrades cleanly instead of breaking the request or a workflow step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConnectorResult:
    """Outcome of a connector operation."""

    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, message: str = "", **data: Any) -> "ConnectorResult":
        return cls(ok=True, message=message, data=dict(data))

    @classmethod
    def failure(cls, message: str, **data: Any) -> "ConnectorResult":
        return cls(ok=False, message=message, data=dict(data))


class BaseConnector:
    """Base class for all marketplace connectors.

    Subclasses set ``provider``, ``auth_type``, ``category``, ``display_name``,
    ``description``, ``config_fields`` (non-secret settings the UI collects),
    ``credential_fields`` (secret fields, encrypted at rest), and
    ``supported_actions`` (the action keys :meth:`execute` accepts).
    """

    provider: str = ""
    display_name: str = ""
    description: str = ""
    category: str = "other"
    auth_type: str = "api_key"
    config_fields: list[str] = []
    credential_fields: list[str] = []
    supported_actions: list[str] = []

    # --- lifecycle -------------------------------------------------------

    async def test_connection(
        self, config: dict, credentials: dict
    ) -> ConnectorResult:
        """Verify the stored credentials reach the provider. Override me."""
        if self.credential_fields and not any(
            credentials.get(f) for f in self.credential_fields
        ):
            return ConnectorResult.failure("Missing credentials")
        return ConnectorResult.success("Configured")

    async def execute(
        self, action: str, params: dict, config: dict, credentials: dict
    ) -> ConnectorResult:
        """Dispatch a named action to its handler method ``_action_<name>``."""
        if action not in self.supported_actions:
            return ConnectorResult.failure(
                f"{self.provider} does not support action {action!r}"
            )
        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            return ConnectorResult.failure(f"Action {action!r} not implemented")
        try:
            return await handler(params, config, credentials)
        except Exception as exc:  # noqa: BLE001 — connectors never raise
            logger.exception("%s.%s failed", self.provider, action)
            return ConnectorResult.failure(f"{self.provider}.{action} error: {exc}")

    # --- catalog metadata ------------------------------------------------

    @classmethod
    def catalog_entry(cls) -> dict:
        """Marketplace listing describing what the UI needs to configure it."""
        return {
            "provider": cls.provider,
            "display_name": cls.display_name or cls.provider.title(),
            "description": cls.description,
            "category": cls.category,
            "auth_type": cls.auth_type,
            "config_fields": list(cls.config_fields),
            "credential_fields": list(cls.credential_fields),
            "supported_actions": list(cls.supported_actions),
        }
