"""
SAPP MTP constrained area result endpoints.
"""

from datetime import date, datetime, time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

from app.db.database import get_db
from app.schemas.sapp import (
    SappConstrainedAreaResultList,
    SappConstrainedAreaResultResponse,
    SappScrapeRangeResponse,
    SappScrapeResponse,
)
from sapp_scraper import (
    SAPP_EXTRACTION_JOBS,
    get_extraction_job,
    run_extraction_job,
    run_extraction_job_for_date_range,
)

router = APIRouter(prefix="/sapp", tags=["sapp"])

Frequency = Literal["1h", "4h", "1d", "1w", "1mo", "1y"]

FREQUENCY_BUCKETS = {
    "4h": {"unit": "hour", "binSize": 4},
    "1d": {"unit": "day", "binSize": 1},
    "1w": {"unit": "week", "binSize": 1},
    "1mo": {"unit": "month", "binSize": 1},
    "1y": {"unit": "year", "binSize": 1},
}


def _serialize_result(record: dict) -> dict:
    record["_id"] = str(record["_id"])
    return record


def _build_constrained_area_filter(
    delivery_date: Optional[date],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
) -> dict:
    query_filter = {}
    if delivery_date:
        query_filter["delivery_date"] = delivery_date.isoformat()
    if start_time or end_time:
        query_filter["timestamp"] = {}
        if start_time:
            query_filter["timestamp"]["$gte"] = start_time
        if end_time:
            query_filter["timestamp"]["$lte"] = end_time
    return query_filter


def _bucket_period_end_expression(frequency: str):
    if frequency == "4h":
        return {"$dateAdd": {"startDate": "$_id", "unit": "hour", "amount": 4}}
    if frequency == "1d":
        return {"$dateAdd": {"startDate": "$_id", "unit": "day", "amount": 1}}
    if frequency == "1w":
        return {"$dateAdd": {"startDate": "$_id", "unit": "week", "amount": 1}}
    if frequency == "1mo":
        return {"$dateAdd": {"startDate": "$_id", "unit": "month", "amount": 1}}
    if frequency == "1y":
        return {"$dateAdd": {"startDate": "$_id", "unit": "year", "amount": 1}}
    raise ValueError(f"Unsupported frequency: {frequency}")


def _aggregate_constrained_area_results(
    collection,
    query_filter: dict,
    frequency: str,
    skip: int,
    limit: int,
) -> tuple[list[dict], int]:
    bucket = FREQUENCY_BUCKETS[frequency]
    date_trunc = {
        "$dateTrunc": {
            "date": "$timestamp",
            "unit": bucket["unit"],
            "binSize": bucket["binSize"],
            "timezone": "UTC",
        }
    }
    if frequency == "1w":
        date_trunc["$dateTrunc"]["startOfWeek"] = "Monday"

    group_stage = {
        "$group": {
            "_id": date_trunc,
            "area_purchase_mw": {"$avg": "$area_purchase_mw"},
            "area_sales_mw": {"$avg": "$area_sales_mw"},
            "area_price_usd_per_mwh": {"$avg": "$area_price_usd_per_mwh"},
            "sample_count": {"$sum": 1},
        }
    }

    total_result = list(
        collection.aggregate(
            [
                {"$match": query_filter},
                group_stage,
                {"$count": "total"},
            ]
        )
    )
    total = total_result[0]["total"] if total_result else 0

    records = list(
        collection.aggregate(
            [
                {"$match": query_filter},
                group_stage,
                {"$sort": {"_id": -1}},
                {"$skip": skip},
                {"$limit": limit},
                {
                    "$project": {
                        "_id": {"$concat": [frequency, ":", {"$toString": "$_id"}]},
                        "timestamp": "$_id",
                        "delivery_date": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$_id",
                                "timezone": "UTC",
                            }
                        },
                        "hour": None,
                        "hour_label": frequency,
                        "frequency": frequency,
                        "period_start": "$_id",
                        "period_end": _bucket_period_end_expression(frequency),
                        "sample_count": "$sample_count",
                        "area_purchase_mw": "$area_purchase_mw",
                        "area_sales_mw": "$area_sales_mw",
                        "area_price_usd_per_mwh": "$area_price_usd_per_mwh",
                        "data_source": "SAPP_MTP_DAM_CONSTRAINED_AREA_RESULTS",
                    }
                },
            ]
        )
    )
    return records, total


@router.get("/scrape-jobs")
def list_scrape_jobs():
    """List configured SAPP inbox extraction jobs."""
    return {
        "jobs": [
            {
                "name": job.name,
                "subject_template": job.subject_template,
                "collection_name": job.collection_name,
                "attachment_extension": job.attachment_extension,
            }
            for job in SAPP_EXTRACTION_JOBS.values()
        ]
    }


@router.post(
    "/scrape",
    response_model=SappScrapeResponse,
    summary="Scrape one SAPP document locally",
    description=(
        "Runs Selenium, Firefox, and geckodriver to download and import one SAPP "
        "document. This should be run locally, not on the deployed server. The "
        "current hosted server does not have enough memory to reliably run the "
        "browser stack, and server-side scraping may trigger memory limit restarts. "
        "Use the deployed API to read already-imported MongoDB data."
    ),
)
def scrape_sapp_results(
    delivery_date: Optional[date] = Query(
        None,
        description="Delivery date to fetch. Defaults to today's date if omitted.",
    ),
    job_name: str = Query(
        "constrained_area_results",
        description="SAPP extraction job to run.",
    ),
):
    """Download, parse, and upsert one SAPP document. Run locally."""
    try:
        job = get_extraction_job(job_name)
        result = run_extraction_job(job, delivery_date=delivery_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


@router.post(
    "/scrape-range",
    response_model=SappScrapeRangeResponse,
    summary="Scrape multiple SAPP documents locally",
    description=(
        "Runs Selenium, Firefox, and geckodriver to download and import SAPP "
        "documents for an inclusive delivery date range. This should be run "
        "locally, not on the deployed server. The current hosted server does not "
        "have enough memory to reliably run the browser stack, and server-side "
        "scraping may trigger memory limit restarts. Use the deployed API to read "
        "already-imported MongoDB data."
    ),
)
def scrape_sapp_results_for_date_range(
    start_date: date = Query(..., description="First delivery date to fetch."),
    end_date: date = Query(..., description="Last delivery date to fetch, inclusive."),
    job_name: str = Query(
        "constrained_area_results",
        description="SAPP extraction job to run.",
    ),
    continue_on_error: bool = Query(
        True,
        description="Continue with later dates if one date fails.",
    ),
):
    """Download, parse, and upsert SAPP documents for a date range. Run locally."""
    try:
        job = get_extraction_job(job_name)
        result = run_extraction_job_for_date_range(
            job,
            start_date=start_date,
            end_date=end_date,
            continue_on_error=continue_on_error,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    frequency: Frequency = Query(
        "1h",
        description="Time bucket frequency: 1h, 4h, 1d, 1w, 1mo, or 1y.",
    ),
):
    """
    List SAPP constrained area results with optional date filtering and frequency bucketing.
    """
    db = get_db()
    collection = db["sapp_constrained_area_results"]

    query_filter = _build_constrained_area_filter(delivery_date, start_time, end_time)

    if frequency == "1h":
        total = collection.count_documents(query_filter)
        records = list(
            collection.find(query_filter)
            .sort("timestamp", -1)
            .skip(skip)
            .limit(limit)
        )
        records = [_serialize_result(record) for record in records]
    else:
        records, total = _aggregate_constrained_area_results(
            collection,
            query_filter,
            frequency,
            skip,
            limit,
        )

    page = (skip // limit) + 1
    return SappConstrainedAreaResultList(
        records=records,
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
