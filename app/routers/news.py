"""Company news monitoring endpoints."""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
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
    limit: int = 20,
    actionable_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List recent company news, newest first."""
    query = select(CompanyNews).order_by(CompanyNews.discovered_at.desc()).limit(limit)
    if actionable_only:
        query = query.where(CompanyNews.is_actionable == True)
    result = await db.execute(query)
    news = result.scalars().all()
    
    items = []
    for n in news:
        # Get company name
        cr = await db.execute(select(Company.name).where(Company.id == n.company_id))
        company_name = cr.scalar_one_or_none() or "Unknown"
        
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
async def check_company_news(company_id: str, db: AsyncSession = Depends(get_db)):
    """Check for news about a specific company."""
    result = await db.execute(select(Company).where(Company.id == UUID(company_id)))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    monitor = get_news_monitor()
    news = await monitor.check_company_news(db, company)
    return {"company": company.name, "articles_found": len(news), "articles": news}


@router.post("/check-all")
async def check_all_news(limit: int = 10, db: AsyncSession = Depends(get_db)):
    """Check news for multiple companies (batch)."""
    monitor = get_news_monitor()
    results = await monitor.check_all_companies(db, limit=limit)
    total_articles = sum(r["news_found"] for r in results)
    return {
        "companies_checked": len(results),
        "total_articles": total_articles,
        "results": results,
    }


@router.patch("/{news_id}/action")
async def mark_actioned(news_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a news item as actioned."""
    result = await db.execute(select(CompanyNews).where(CompanyNews.id == UUID(news_id)))
    news = result.scalar_one_or_none()
    if not news:
        raise HTTPException(status_code=404, detail="News item not found")
    news.actioned = True
    await db.commit()
    return {"status": "actioned"}
