"""Frontend-facing status and manual controls for backend SAPP scraping."""

from fastapi import APIRouter, HTTPException, Query

from app.services.sapp_auto_scraper import sapp_auto_scraper

router = APIRouter(prefix="/sapp/auto-scraper", tags=["sapp-auto-scraper"])


@router.get("/status")
def get_auto_scraper_status():
    """Return scheduler configuration and the latest run for every dataset."""
    return sapp_auto_scraper.status()


@router.get("/runs")
def get_auto_scraper_runs(limit: int = Query(50, ge=1, le=200)):
    """Return recent scheduled and manually triggered scraper runs."""
    return {"runs": sapp_auto_scraper.runs(limit)}


@router.post("/trigger/{job_name}", status_code=202)
async def trigger_auto_scraper(job_name: str):
    """Manually queue one configured scraper using the same scheduler lock."""
    try:
        return await sapp_auto_scraper.trigger(job_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
