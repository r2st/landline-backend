"""Company news monitoring endpoints."""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.services.news_monitor import get_news_monitor

router = APIRouter(prefix="/news", tags=["news"])


@router.get("")
async def list_news(
    limit: int = Query(20, ge=1, le=200),
    actionable_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List recent company news, newest first."""
    # Join the company name in a single query (avoids an N+1 per article).
    query = (
        select(CompanyNews, Company.name)
        .outerjoin(Company, Company.id == CompanyNews.company_id)
        .order_by(CompanyNews.discovered_at.desc())
        .limit(limit)
    )
    if actionable_only:
        query = query.where(CompanyNews.is_actionable == True)
    result = await db.execute(query)
    rows = result.all()

    items = []
    for n, company_name in rows:
        company_name = company_name or "Unknown"

        items.append({
            "id": str(n.id),
            "company_id": str(n.company_id),
            "company_name": company_name,
            "title": n.title,
            "summary": n.summary,
            "url": n.url,
            "source": n.source,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            "discovered_at": n.discovered_at.isoformat() if n.discovered_at else None,
            "category": n.category,
            "relevance_score": n.relevance_score,
            "is_actionable": n.is_actionable,
            "actioned": n.actioned,
            "ai_analysis": n.ai_analysis,
        })
    return items


@router.post("/check/{company_id}")
async def check_company_news(company_id: UUID, db: AsyncSession = Depends(get_db)):
    """Check for news about a specific company."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    monitor = get_news_monitor()
    news = await monitor.check_company_news(db, company)
    return {"company": company.name, "articles_found": len(news), "articles": news}


@router.post("/check-all")
async def check_all_news(
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Check news for multiple companies (batch).

    ``limit`` is bounded because each company in the batch costs an outbound
    news fetch and an LLM call, run one after another inside this request. An
    unbounded value therefore let one caller hold a worker and a database
    connection for as long as the company table is long, and spend the org's
    model quota doing it — a `?limit=100000` that reads as a harmless default
    override. It also rejects ``0``/negative rather than passing them to
    ``LIMIT``, which Postgres refuses outright.
    """
    monitor = get_news_monitor()
    checked, results = await monitor.check_all_companies_counted(db, limit=limit)
    total_articles = sum(r["news_found"] for r in results)
    return {
        # ``results`` holds only the companies that *had* news — the batch drops
        # the empty ones — so ``len(results)`` is not the number checked. It was
        # reported as one, which meant a sweep that examined ten companies and
        # found nothing said "companies_checked: 0": indistinguishable from a
        # sweep that never ran, on the endpoint an operator uses to confirm it
        # did.
        "companies_checked": checked,
        "companies_with_news": len(results),
        "total_articles": total_articles,
        "results": results,
    }


@router.patch("/{news_id}/action")
async def mark_actioned(news_id: UUID, db: AsyncSession = Depends(get_db)):
    """Mark a news item as actioned."""
    result = await db.execute(select(CompanyNews).where(CompanyNews.id == news_id))
    news = result.scalar_one_or_none()
    if not news:
        raise HTTPException(status_code=404, detail="News item not found")
    news.actioned = True
    # get_db commits on success; flush keeps this consistent with the rest of
    # the app and lets the surrounding transaction own the commit.
    await db.flush()
    return {"status": "actioned", "id": str(news.id), "actioned": True}
