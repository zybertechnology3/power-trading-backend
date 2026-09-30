"""Frontend-facing status and manual controls for backend SAPP scraping."""

from datetime import datetime, timezone
from typing import Optional

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


@router.get("/runs/finished")
def get_finished_sapp_runs(
    since: Optional[datetime] = Query(
        None,
        description="Return completed runs after this timestamp. Store next_since from the response.",
    ),
    dataset_id: Optional[str] = Query(None),
    status: str = Query("success", pattern="^(success|failed|all)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return durable completion events for frontend catch-up syncing."""
    return sapp_auto_scraper.finished_runs(
        since=since,
        dataset_id=dataset_id,
        status=status,
        limit=limit,
    )


@router.post("/trigger/{job_name}", status_code=202)
async def trigger_auto_scraper(job_name: str):
    """Manually queue one configured scraper using the same scheduler lock."""
    try:
        return await sapp_auto_scraper.trigger(job_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
