"""Integration connector registry (feature 4.2).

Maps a provider key → connector instance and exposes the marketplace catalog.
The router and workflow actions resolve connectors through :func:`get_connector`
so adding a provider is a one-line change here.
"""

from typing import Optional

from app.services.integrations.base import BaseConnector, ConnectorResult
from app.services.integrations.providers import (
    ClearbitConnector,
    GoogleCalendarConnector,
    HubSpotConnector,
    SalesforceConnector,
    SlackConnector,
    ZoomInfoConnector,
)

# Provider key → connector instance. Instances are stateless (config and
# credentials are passed per call), so a module-level singleton each is fine.
_CONNECTORS: dict[str, BaseConnector] = {
    c.provider: c
    for c in (
        SlackConnector(),
        GoogleCalendarConnector(),
        HubSpotConnector(),
        SalesforceConnector(),
        ClearbitConnector(),
        ZoomInfoConnector(),
    )
}


def get_connector(provider: str) -> Optional[BaseConnector]:
    """Return the connector for a provider key, or ``None`` if unknown."""
    return _CONNECTORS.get(provider)


def marketplace_catalog() -> list[dict]:
    """All available integrations as marketplace listings."""
    return [c.catalog_entry() for c in _CONNECTORS.values()]


def known_providers() -> list[str]:
    return list(_CONNECTORS.keys())


__all__ = [
    "BaseConnector",
    "ConnectorResult",
    "get_connector",
    "marketplace_catalog",
    "known_providers",
]
