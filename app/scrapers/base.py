"""Abstract base class for all scrapers."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class ScraperResult:
    """Standardized result from any scraper."""

    def __init__(self):
        self.companies: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.total_found: int = 0

    def add_company(self, data: dict[str, Any]) -> None:
        self.companies.append(data)
        self.total_found += 1

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def to_dict(self) -> dict:
        return {
            "total_found": self.total_found,
            "companies_count": len(self.companies),
            "errors_count": len(self.errors),
            "errors": self.errors[:20],
        }


class BaseScraper(ABC):
    """Base class for all data scrapers.

    Subclasses must implement `scrape()` which populates a ScraperResult
    and `save_results()` which persists data to the database.
    """

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"scraper.{name}")

    @abstractmethod
    async def scrape(self, **kwargs) -> ScraperResult:
        """Run the scraper with given parameters. Returns ScraperResult."""
        ...

    @abstractmethod
    async def save_results(
        self, db: AsyncSession, result: ScraperResult
    ) -> dict:
        """Persist scraped data to the database. Returns summary stats."""
        ...

    async def run(self, db: AsyncSession, **kwargs) -> dict:
        """Execute full scrape-and-save pipeline."""
        self.logger.info("Starting %s scraper with params: %s", self.name, kwargs)

        result = await self.scrape(**kwargs)
        self.logger.info(
            "%s scraper found %d companies (%d errors)",
            self.name, result.total_found, len(result.errors),
        )

        if result.companies:
            stats = await self.save_results(db, result)
        else:
            stats = {"saved": 0, "skipped": 0}

        return {
            "scraper": self.name,
            "scraped": result.to_dict(),
            "saved": stats,
        }
