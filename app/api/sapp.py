"""
SAPP MTP constrained area result endpoints.
"""

from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.db.database import get_db
from app.schemas.sapp import (
    SappConstrainedAreaResultList,
    SappConstrainedAreaResultResponse,
    SappScrapeResponse,
)
from sapp_scraper import run_scraper

router = APIRouter(prefix="/sapp", tags=["sapp"])


def _serialize_result(record: dict) -> dict:
    record["_id"] = str(record["_id"])
    return record


@router.post("/scrape", response_model=SappScrapeResponse)
def scrape_sapp_results(
    delivery_date: Optional[date] = Query(
        None,
        description="Delivery date to fetch. Defaults to today's date if omitted.",
    )
):
    """
    Download the SAPP MTP DAM constrained area document for a delivery date,
    parse hourly data, and upsert it into MongoDB.
    """
    try:
        result = run_scraper(delivery_date=delivery_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


@router.get("/constrained-area-results", response_model=SappConstrainedAreaResultList)
def list_constrained_area_results(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List SAPP hourly constrained area results with optional delivery-date or
    timestamp range filtering.
    """
    db = get_db()
    collection = db["sapp_constrained_area_results"]

    query_filter = {}
    if delivery_date:
        query_filter["delivery_date"] = delivery_date.isoformat()
    if start_time or end_time:
        query_filter["timestamp"] = {}
        if start_time:
            query_filter["timestamp"]["$gte"] = start_time
        if end_time:
            query_filter["timestamp"]["$lte"] = end_time

    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort("timestamp", -1)
        .skip(skip)
        .limit(limit)
    )

    page = (skip // limit) + 1
    return SappConstrainedAreaResultList(
        records=[_serialize_result(record) for record in records],
        total=total,
        page=page,
        page_size=limit,
    )


@router.get(
    "/constrained-area-results/{delivery_date}",
    response_model=list[SappConstrainedAreaResultResponse],
)
def get_constrained_area_results_for_day(delivery_date: date):
    """Get all hourly SAPP constrained area results for one delivery date."""
    db = get_db()
    collection = db["sapp_constrained_area_results"]

    start = datetime.combine(delivery_date, time.min)
    end = datetime.combine(delivery_date, time.max)
    records = list(
        collection.find({"timestamp": {"$gte": start, "$lte": end}})
        .sort("timestamp", 1)
    )
    if not records:
        raise HTTPException(status_code=404, detail="No SAPP results found for this date")

    return [_serialize_result(record) for record in records]
