"""Census Bureau County Business Patterns (CBP) API scraper.

Uses the Census Bureau's CBP API to find manufacturing establishments
by NAICS code, state, and employee size class.

API docs: https://api.census.gov/data/2021/cbp.html
"""

from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.scrapers.base import BaseScraper, ScraperResult

settings = get_settings()

# NAICS manufacturing codes (31-33)
MANUFACTURING_NAICS = {
    "3111": "Animal food manufacturing",
    "3112": "Grain and oilseed milling",
    "3114": "Fruit and vegetable preserving",
    "3115": "Dairy product manufacturing",
    "3116": "Meat product manufacturing",
    "3118": "Bakeries and tortilla manufacturing",
    "3119": "Other food manufacturing",
    "3121": "Beverage manufacturing",
    "3131": "Fiber, yarn, and thread mills",
    "3132": "Fabric mills",
    "3133": "Textile and fabric finishing",
    "3141": "Textile furnishings mills",
    "3149": "Other textile product mills",
    "3151": "Apparel knitting mills",
    "3152": "Cut and sew apparel manufacturing",
    "3159": "Apparel accessories manufacturing",
    "3211": "Sawmills and wood preservation",
    "3212": "Veneer, plywood, and engineered wood",
    "3219": "Other wood product manufacturing",
    "3221": "Pulp, paper, and paperboard mills",
    "3222": "Converted paper product manufacturing",
    "3231": "Printing and related support",
    "3241": "Petroleum and coal products",
    "3251": "Basic chemical manufacturing",
    "3252": "Resin, rubber, and fiber manufacturing",
    "3253": "Pesticide, fertilizer manufacturing",
    "3254": "Pharmaceutical manufacturing",
    "3255": "Paint, coating, adhesive manufacturing",
    "3256": "Soap, cleaning compound manufacturing",
    "3259": "Other chemical manufacturing",
    "3261": "Plastics product manufacturing",
    "3262": "Rubber product manufacturing",
    "3271": "Clay and refractory manufacturing",
    "3272": "Glass and glass product manufacturing",
    "3273": "Cement and concrete manufacturing",
    "3274": "Lime and gypsum product manufacturing",
    "3279": "Other nonmetallic mineral products",
    "3311": "Iron and steel mills",
    "3312": "Steel product manufacturing",
    "3313": "Alumina and aluminum production",
    "3314": "Nonferrous metal production",
    "3315": "Foundries",
    "3321": "Forging and stamping",
    "3322": "Cutlery and handtool manufacturing",
    "3323": "Architectural and structural metals",
    "3324": "Boiler, tank, and shipping container",
    "3325": "Hardware manufacturing",
    "3326": "Spring and wire product manufacturing",
    "3327": "Machine shops and threaded products",
    "3328": "Coating, engraving, heat treating",
    "3329": "Other fabricated metal products",
    "3331": "Agriculture and construction machinery",
    "3332": "Industrial machinery manufacturing",
    "3333": "Commercial machinery manufacturing",
    "3334": "HVAC and refrigeration equipment",
    "3335": "Metalworking machinery manufacturing",
    "3336": "Engine and turbine manufacturing",
    "3339": "Other general purpose machinery",
    "3341": "Computer and peripheral equipment",
    "3342": "Communications equipment manufacturing",
    "3343": "Audio and video equipment",
    "3344": "Semiconductor and electronic components",
    "3345": "Navigational and measuring instruments",
    "3346": "Magnetic and optical media",
    "3351": "Electric lighting equipment",
    "3352": "Household appliance manufacturing",
    "3353": "Electrical equipment manufacturing",
    "3359": "Other electrical equipment",
    "3361": "Motor vehicle manufacturing",
    "3362": "Motor vehicle body and trailer",
    "3363": "Motor vehicle parts manufacturing",
    "3364": "Aerospace product manufacturing",
    "3365": "Railroad rolling stock manufacturing",
    "3366": "Ship and boat building",
    "3369": "Other transportation equipment",
    "3371": "Household and office furniture",
    "3372": "Office furniture manufacturing",
    "3379": "Other furniture manufacturing",
    "3391": "Medical equipment manufacturing",
    "3399": "Other miscellaneous manufacturing",
}

# Employee size class codes
SIZE_CLASSES = {
    "210": "10-19 employees",
    "220": "20-49 employees",
    "230": "50-99 employees",
    "241": "100-249 employees",
    "242": "250-499 employees",
    "251": "500-999 employees",
    "252": "1000-2499 employees",
}


class CensusBPScraper(BaseScraper):
    """Queries Census Bureau County Business Patterns API for manufacturing firms."""

    API_URL = "https://api.census.gov/data/2021/cbp"

    def __init__(self):
        super().__init__("census_cbp")

    async def scrape(
        self,
        naics_codes: Optional[list[str]] = None,
        states: Optional[list[str]] = None,
        min_employees: int = 50,
        **kwargs,
    ) -> ScraperResult:
        """Query the CBP API for manufacturing establishment counts by area.

        Args:
            naics_codes: Specific 4-digit NAICS codes. Defaults to key manufacturing codes.
            states: FIPS state codes (e.g., ["39"] for Ohio). None = all states.
            min_employees: Minimum employee threshold for size class filter.
        """
        result = ScraperResult()

        if naics_codes is None:
            # Focus on metal/machinery manufacturing — most relevant to Apprend
            naics_codes = [
                "3321", "3322", "3323", "3324", "3325", "3326", "3327", "3328", "3329",
                "3331", "3332", "3335", "3339",
            ]

        # Map min_employees to size class filter
        size_filter = self._get_size_classes(min_employees)

        api_key = settings.census_api_key

        async with httpx.AsyncClient(timeout=30.0) as client:
            for naics in naics_codes:
                try:
                    params = {
                        "get": "NAME,NAICS2017,NAICS2017_LABEL,ESTAB,EMPSZES,EMPSZES_LABEL,GEO_ID",
                        "for": "state:*" if not states else f"state:{','.join(states)}",
                        "NAICS2017": naics,
                    }
                    if api_key:
                        params["key"] = api_key
                    if size_filter:
                        params["EMPSZES"] = size_filter

                    response = await client.get(self.API_URL, params=params)
                    response.raise_for_status()
                    data = response.json()

                    if len(data) < 2:
                        continue

                    headers = data[0]
                    for row in data[1:]:
                        record = dict(zip(headers, row))
                        estab_count = int(record.get("ESTAB", "0"))
                        if estab_count > 0:
                            result.add_company({
                                "state_name": record.get("NAME", ""),
                                "naics_code": record.get("NAICS2017", naics),
                                "naics_description": record.get("NAICS2017_LABEL", MANUFACTURING_NAICS.get(naics, "")),
                                "establishment_count": estab_count,
                                "size_class": record.get("EMPSZES_LABEL", ""),
                                "state_fips": record.get("state", ""),
                                "geo_id": record.get("GEO_ID", ""),
                                "source": "census_cbp",
                            })

                except httpx.HTTPStatusError as e:
                    result.add_error(f"Census API error for NAICS {naics}: HTTP {e.response.status_code}")
                except Exception as e:
                    result.add_error(f"Error querying NAICS {naics}: {str(e)}")

        return result

    def _get_size_classes(self, min_employees: int) -> str:
        """Map minimum employee count to CBP size class codes."""
        applicable = []
        thresholds = [
            (10, "210"), (20, "220"), (50, "230"),
            (100, "241"), (250, "242"), (500, "251"),
            (1000, "252"),
        ]
        for threshold, code in thresholds:
            if threshold >= min_employees:
                applicable.append(code)
        return ",".join(applicable) if applicable else ""

    async def save_results(
        self, db: AsyncSession, result: ScraperResult
    ) -> dict:
        """Save Census CBP data as aggregate market intelligence records.

        Census data gives aggregate counts per state/NAICS, not individual companies.
        We create placeholder company records representing market segments.
        """
        saved = 0
        skipped = 0

        for data in result.companies:
            state_name = data.get("state_name", "Unknown")
            naics = data.get("naics_code", "")
            naics_desc = data.get("naics_description", "")
            estab_count = data.get("establishment_count", 0)

            segment_name = f"{naics_desc} — {state_name} ({estab_count} establishments)"

            # Check duplicate
            existing = await db.execute(
                select(Company).where(Company.name == segment_name).limit(1)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            company = Company(
                name=segment_name,
                state_province=state_name,
                country="US",
                naics_code=naics,
                naics_description=naics_desc,
                employee_count=estab_count,
                source="census_cbp",
                industry_segment=naics_desc,
                tags=["census_aggregate", f"naics_{naics}"],
                notes=f"Census CBP aggregate: {estab_count} establishments in {state_name} for {naics_desc}. Size class: {data.get('size_class', 'N/A')}",
            )
            db.add(company)
            saved += 1

        await db.flush()
        self.logger.info("Saved %d census records, skipped %d", saved, skipped)
        return {"saved": saved, "skipped": skipped}
