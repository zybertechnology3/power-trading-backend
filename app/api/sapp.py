"""
SAPP MTP constrained area result endpoints.
"""

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import ValidationError

from app.db.database import get_db
from app.schemas.sapp import (
    BidStatus,
    SappBidMarket,
    SappBidCreate,
    SappBidList,
    SappBidResponse,
    SappBidTemplateCreate,
    SappBidTemplateList,
    SappBidTemplateResponse,
    SappBidTemplateUpdate,
    SappBidUpdate,
    SappConstrainedAreaResultList,
    SappConstrainedAreaResultResponse,
    SappParticipantPortfolioResultList,
    SappParticipantPortfolioResultResponse,
    SappScrapeRangeResponse,
    SappScrapeResponse,
    SappTradingInvoiceCreditNoteList,
    SappTradingInvoiceCreditNoteResponse,
    SappTradingInvoiceHourlyDetailList,
    SappTradingInvoiceHourlyDetailResponse,
)
from sapp_scraper import (
    SAPP_EXTRACTION_JOBS,
    get_extraction_job,
    run_extraction_job,
    run_extraction_job_for_date_range,
)

router = APIRouter(prefix="/sapp", tags=["sapp"])

Frequency = Literal["1h", "4h", "1d", "1w", "1mo", "1y"]
TRADING_INVOICE_MARKETS = ("fpm_m", "fpm_w", "dam", "idm", "bm_up", "bm_down")
TRADING_INVOICE_MARKET_ALIASES = {
    "fpm-m": "fpm_m",
    "fpm m": "fpm_m",
    "fpmm": "fpm_m",
    "fpm-w": "fpm_w",
    "fpm w": "fpm_w",
    "fpmw": "fpm_w",
    "bm up": "bm_up",
    "bm-up": "bm_up",
    "bm down": "bm_down",
    "bm-down": "bm_down",
}

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _name_key(name: str) -> str:
    return " ".join(name.lower().split())


def _normalize_quantities(quantities) -> list[dict]:
    normalized_quantities = []
    for quantity in quantities:
        quantity_data = (
            quantity.model_dump(exclude_none=True)
            if hasattr(quantity, "model_dump")
            else dict(quantity)
        )
        if quantity_data["energy_mwh"] > 0:
            normalized_quantities.append(quantity_data)
    return normalized_quantities


def _calculate_bid_totals(quantities: list[dict]) -> tuple[float, Optional[float]]:
    total_energy = sum(quantity["energy_mwh"] for quantity in quantities)
    if total_energy == 0:
        return 0, None

    total_value = sum(
        quantity["energy_mwh"] * quantity["price_usd_per_mwh"]
        for quantity in quantities
    )
    return total_energy, total_value / total_energy


def _bid_period_range(payload: SappBidCreate) -> tuple[date, date, int]:
    if payload.market == "dam":
        return payload.delivery_date, payload.delivery_date, 1
    if payload.market == "fpm_w":
        return payload.week_start_date, payload.week_start_date + timedelta(days=6), 7

    days_in_month = monthrange(
        payload.month_start_date.year,
        payload.month_start_date.month,
    )[1]
    return (
        payload.month_start_date,
        payload.month_start_date + timedelta(days=days_in_month - 1),
        days_in_month,
    )


def _bid_fields_from_payload(payload: SappBidCreate) -> dict:
    quantities = _normalize_quantities(payload.quantities)
    daily_energy, weighted_average_price = _calculate_bid_totals(quantities)
    period_start_date, period_end_date, delivery_days = _bid_period_range(payload)
    return {
        "market": payload.market,
        "delivery_date": payload.delivery_date.isoformat()
        if payload.delivery_date
        else None,
        "week_start_date": payload.week_start_date.isoformat()
        if payload.week_start_date
        else None,
        "month_start_date": payload.month_start_date.isoformat()
        if payload.month_start_date
        else None,
        "period_start_date": period_start_date.isoformat(),
        "period_end_date": period_end_date.isoformat(),
        "delivery_days": delivery_days,
        "price_columns": payload.price_columns,
        "quantities": quantities,
        "template_id": payload.template_id,
        "notes": payload.notes,
        "daily_energy_mwh": daily_energy,
        "total_energy_mwh": daily_energy * delivery_days,
        "weighted_average_price_usd_per_mwh": weighted_average_price,
    }


def _template_fields_from_payload(payload: SappBidTemplateCreate) -> dict:
    return {
        "name": payload.name,
        "name_key": _name_key(payload.name),
        "market": payload.market,
        "price_columns": payload.price_columns,
        "default_quantities": _normalize_quantities(payload.default_quantities),
        "notes": payload.notes,
    }


def _validate_bid_candidate(candidate: dict) -> SappBidCreate:
    try:
        return SappBidCreate(**candidate)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _validate_template_candidate(candidate: dict) -> SappBidTemplateCreate:
    try:
        return SappBidTemplateCreate(**candidate)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _get_template_or_400(template_id: Optional[str]) -> Optional[dict]:
    if not template_id:
        return None

    template_oid = _parse_object_id(template_id, "template")
    db = get_db()
    template = db["sapp_bid_templates"].find_one({"_id": template_oid})
    if not template:
        raise HTTPException(status_code=400, detail="Template not found")
    return template


def _ensure_template_matches_market(template_id: Optional[str], market: str) -> None:
    template = _get_template_or_400(template_id)
    if template and template.get("market", "dam") != market:
        raise HTTPException(
            status_code=400,
            detail="Template market must match bid market",
        )


def _normalize_trading_invoice_market(market: Optional[str]) -> Optional[str]:
    if market is None:
        return None

    normalized_market = market.strip().lower().replace("-", "_").replace(" ", "_")
    normalized_market = TRADING_INVOICE_MARKET_ALIASES.get(
        market.strip().lower(),
        normalized_market,
    )
    if normalized_market not in TRADING_INVOICE_MARKETS:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid trading invoice market.",
                "invalid_market": market,
                "valid_markets": list(TRADING_INVOICE_MARKETS),
                "examples": [
                    "market=dam",
                    "market=fpm_w",
                    "market=bm_up",
                ],
            },
        )

    return normalized_market


def _build_sapp_time_filter(
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
                {"$sort": {"_id": 1}},
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


@router.post(
    "/bids",
    response_model=SappBidResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_bid(payload: SappBidCreate):
    """Create and save a draft SAPP bid construction grid."""
    _ensure_template_matches_market(payload.template_id, payload.market)

    db = get_db()
    collection = db["sapp_bids"]
    now = _utcnow()
    document = {
        **_bid_fields_from_payload(payload),
        "status": "draft",
        "submitted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    result = collection.insert_one(document)
    record = collection.find_one({"_id": result.inserted_id})
    return _serialize_result(record)


@router.get("/bids/history", response_model=SappBidList)
def list_bid_history(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    market: Optional[SappBidMarket] = Query(None),
    status_filter: Optional[BidStatus] = Query(None, alias="status"),
    delivery_date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    """List saved bids and submitted bids for the bid history screen."""
    db = get_db()
    collection = db["sapp_bids"]

    query_filter = {}
    if market:
        query_filter["market"] = market
    if status_filter:
        query_filter["status"] = status_filter
    if delivery_date:
        query_filter["period_start_date"] = delivery_date.isoformat()
    elif start_date or end_date:
        query_filter["period_start_date"] = {}
        if start_date:
            query_filter["period_start_date"]["$gte"] = start_date.isoformat()
        if end_date:
            query_filter["period_start_date"]["$lte"] = end_date.isoformat()

    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort([("updated_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(limit)
    )
    records = [_serialize_result(record) for record in records]

    page = (skip // limit) + 1
    return SappBidList(
        records=records,
        total=total,
        page=page,
        page_size=limit,
    )


@router.get("/bids/{bid_id}", response_model=SappBidResponse)
def get_bid(bid_id: str):
    """Get one saved bid construction grid."""
    db = get_db()
    collection = db["sapp_bids"]
    record = collection.find_one({"_id": _parse_object_id(bid_id, "bid")})
    if not record:
        raise HTTPException(status_code=404, detail="Bid not found")

    return _serialize_result(record)


@router.patch("/bids/{bid_id}", response_model=SappBidResponse)
def update_bid(bid_id: str, payload: SappBidUpdate):
    """Edit a draft bid. Submitted bids are immutable."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No bid fields provided")

    db = get_db()
    collection = db["sapp_bids"]
    bid_oid = _parse_object_id(bid_id, "bid")
    existing = collection.find_one({"_id": bid_oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Bid not found")
    if existing["status"] != "draft":
        raise HTTPException(status_code=409, detail="Submitted bids cannot be edited")

    candidate = {
        "market": existing.get("market", "dam"),
        "delivery_date": existing.get("delivery_date"),
        "week_start_date": existing.get("week_start_date"),
        "month_start_date": existing.get("month_start_date"),
        "price_columns": existing["price_columns"],
        "quantities": existing.get("quantities", []),
        "template_id": existing.get("template_id"),
        "notes": existing.get("notes"),
    }
    candidate.update(update_data)

    if "market" in update_data and "quantities" not in update_data:
        candidate["quantities"] = []

    if (
        "price_columns" in update_data
        and "market" not in update_data
        and "quantities" not in update_data
    ):
        valid_prices = set(update_data["price_columns"])
        candidate["quantities"] = [
            quantity
            for quantity in existing.get("quantities", [])
            if quantity["price_usd_per_mwh"] in valid_prices
        ]

    validated_bid = _validate_bid_candidate(candidate)
    _ensure_template_matches_market(validated_bid.template_id, validated_bid.market)
    update_fields = _bid_fields_from_payload(validated_bid)
    update_fields["updated_at"] = _utcnow()

    record = collection.find_one_and_update(
        {"_id": bid_oid},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_result(record)


@router.post("/bids/{bid_id}/submit", response_model=SappBidResponse)
def submit_bid(bid_id: str):
    """Submit a draft bid and make it immutable."""
    db = get_db()
    collection = db["sapp_bids"]
    bid_oid = _parse_object_id(bid_id, "bid")
    existing = collection.find_one({"_id": bid_oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Bid not found")
    if existing["status"] != "draft":
        raise HTTPException(status_code=409, detail="Bid has already been submitted")
    if existing.get("total_energy_mwh", 0) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot submit a bid with no positive energy quantities",
        )

    now = _utcnow()
    record = collection.find_one_and_update(
        {"_id": bid_oid, "status": "draft"},
        {
            "$set": {
                "status": "submitted",
                "submitted_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_result(record)


@router.post(
    "/bid-templates",
    response_model=SappBidTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_bid_template(payload: SappBidTemplateCreate):
    """Create a reusable bid grid template."""
    db = get_db()
    collection = db["sapp_bid_templates"]
    now = _utcnow()
    document = {
        **_template_fields_from_payload(payload),
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = collection.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Template name already exists")

    record = collection.find_one({"_id": result.inserted_id})
    return _serialize_result(record)


@router.get("/bid-templates", response_model=SappBidTemplateList)
def list_bid_templates(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    market: Optional[SappBidMarket] = Query(None),
):
    """List reusable bid templates."""
    db = get_db()
    collection = db["sapp_bid_templates"]
    query_filter = {}
    if market:
        query_filter["market"] = market

    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort([("updated_at", -1), ("name", 1)])
        .skip(skip)
        .limit(limit)
    )
    records = [_serialize_result(record) for record in records]
    page = (skip // limit) + 1
    return SappBidTemplateList(
        records=records,
        total=total,
        page=page,
        page_size=limit,
    )


@router.get("/bid-templates/{template_id}", response_model=SappBidTemplateResponse)
def get_bid_template(template_id: str):
    """Get one reusable bid template."""
    db = get_db()
    collection = db["sapp_bid_templates"]
    record = collection.find_one({"_id": _parse_object_id(template_id, "template")})
    if not record:
        raise HTTPException(status_code=404, detail="Template not found")

    return _serialize_result(record)


@router.patch("/bid-templates/{template_id}", response_model=SappBidTemplateResponse)
def update_bid_template(template_id: str, payload: SappBidTemplateUpdate):
    """Update a reusable bid template."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No template fields provided")

    db = get_db()
    collection = db["sapp_bid_templates"]
    template_oid = _parse_object_id(template_id, "template")
    existing = collection.find_one({"_id": template_oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")

    candidate = {
        "name": existing["name"],
        "market": existing.get("market", "dam"),
        "price_columns": existing["price_columns"],
        "default_quantities": existing.get("default_quantities", []),
        "notes": existing.get("notes"),
    }
    candidate.update(update_data)

    if "market" in update_data and "default_quantities" not in update_data:
        candidate["default_quantities"] = []

    if (
        "price_columns" in update_data
        and "market" not in update_data
        and "default_quantities" not in update_data
    ):
        valid_prices = set(update_data["price_columns"])
        candidate["default_quantities"] = [
            quantity
            for quantity in existing.get("default_quantities", [])
            if quantity["price_usd_per_mwh"] in valid_prices
        ]

    validated_template = _validate_template_candidate(candidate)
    update_fields = _template_fields_from_payload(validated_template)
    update_fields["updated_at"] = _utcnow()

    try:
        record = collection.find_one_and_update(
            {"_id": template_oid},
            {"$set": update_fields},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Template name already exists")

    return _serialize_result(record)


@router.delete("/bid-templates/{template_id}")
def delete_bid_template(template_id: str):
    """Delete a reusable bid template. Existing bids keep their copied grid values."""
    db = get_db()
    collection = db["sapp_bid_templates"]
    result = collection.delete_one({"_id": _parse_object_id(template_id, "template")})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")

    return {"deleted": True}


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
    limit: int = Query(1000, ge=1, le=1000),
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

    query_filter = _build_sapp_time_filter(delivery_date, start_time, end_time)

    if frequency == "1h":
        total = collection.count_documents(query_filter)
        records = list(
            collection.find(query_filter)
            .sort("timestamp", 1)
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


@router.get(
    "/participant-portfolio-results",
    response_model=SappParticipantPortfolioResultList,
)
def list_participant_portfolio_results(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List SAPP participant portfolio results with optional date filtering.
    """
    db = get_db()
    collection = db["sapp_participant_portfolio_results"]

    query_filter = _build_sapp_time_filter(delivery_date, start_time, end_time)
    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort("timestamp", 1)
        .skip(skip)
        .limit(limit)
    )
    records = [_serialize_result(record) for record in records]

    page = (skip // limit) + 1
    return SappParticipantPortfolioResultList(
        records=records,
        total=total,
        page=page,
        page_size=limit,
    )


@router.post(
    "/participant-portfolio-results/scrape",
    response_model=SappScrapeResponse,
    summary="Scrape one SAPP participant portfolio document locally",
)
def scrape_participant_portfolio_results(
    delivery_date: Optional[date] = Query(
        None,
        description="Delivery date to fetch. Defaults to today's date if omitted.",
    ),
):
    """Download, parse, and upsert one SAPP participant portfolio document."""
    try:
        job = get_extraction_job("participant_portfolio_results")
        result = run_extraction_job(job, delivery_date=delivery_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


@router.post(
    "/participant-portfolio-results/scrape-range",
    response_model=SappScrapeRangeResponse,
    summary="Scrape multiple SAPP participant portfolio documents locally",
)
def scrape_participant_portfolio_results_for_date_range(
    start_date: date = Query(..., description="First delivery date to fetch."),
    end_date: date = Query(..., description="Last delivery date to fetch, inclusive."),
    continue_on_error: bool = Query(
        True,
        description="Continue with later dates if one date fails.",
    ),
):
    """Download, parse, and upsert SAPP participant portfolio documents for a date range."""
    try:
        job = get_extraction_job("participant_portfolio_results")
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


@router.get(
    "/participant-portfolio-results/{delivery_date}",
    response_model=list[SappParticipantPortfolioResultResponse],
)
def get_participant_portfolio_results_for_day(delivery_date: date):
    """Get all hourly SAPP participant portfolio results for one delivery date."""
    db = get_db()
    collection = db["sapp_participant_portfolio_results"]

    start = datetime.combine(delivery_date, time.min)
    end = datetime.combine(delivery_date, time.max)
    records = list(
        collection.find({"timestamp": {"$gte": start, "$lte": end}})
        .sort("timestamp", 1)
    )
    if not records:
        raise HTTPException(
            status_code=404,
            detail="No SAPP participant portfolio results found for this date",
        )

    return [_serialize_result(record) for record in records]


@router.get(
    "/trading-invoice-credit-notes",
    response_model=SappTradingInvoiceCreditNoteList,
)
def list_trading_invoice_credit_notes(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List SAPP trading invoice / credit note summaries with optional date filtering.
    """
    db = get_db()
    collection = db["sapp_trading_invoice_credit_notes"]

    query_filter = _build_sapp_time_filter(delivery_date, start_time, end_time)
    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort("timestamp", 1)
        .skip(skip)
        .limit(limit)
    )
    records = [_serialize_result(record) for record in records]

    page = (skip // limit) + 1
    return SappTradingInvoiceCreditNoteList(
        records=records,
        total=total,
        page=page,
        page_size=limit,
    )


@router.post(
    "/trading-invoice-credit-notes/scrape",
    response_model=SappScrapeResponse,
    summary="Scrape one SAPP trading invoice / credit note Excel document locally",
)
def scrape_trading_invoice_credit_note(
    delivery_date: Optional[date] = Query(
        None,
        description="Delivery date to fetch. Defaults to today's date if omitted.",
    ),
):
    """Download, parse, and upsert one SAPP trading invoice / credit note workbook."""
    try:
        job = get_extraction_job("trading_invoice_credit_note")
        result = run_extraction_job(job, delivery_date=delivery_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


@router.post(
    "/trading-invoice-credit-notes/scrape-range",
    response_model=SappScrapeRangeResponse,
    summary="Scrape multiple SAPP trading invoice / credit note Excel documents locally",
)
def scrape_trading_invoice_credit_notes_for_date_range(
    start_date: date = Query(..., description="First delivery date to fetch."),
    end_date: date = Query(..., description="Last delivery date to fetch, inclusive."),
    continue_on_error: bool = Query(
        True,
        description="Continue with later dates if one date fails.",
    ),
):
    """Download, parse, and upsert SAPP trading invoice / credit note workbooks."""
    try:
        job = get_extraction_job("trading_invoice_credit_note")
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


@router.get(
    "/trading-invoice-credit-notes/hourly-details",
    response_model=SappTradingInvoiceHourlyDetailList,
)
def list_trading_invoice_hourly_details(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    market: Optional[str] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List hourly SAPP trading invoice detail rows with optional date and market filtering.
    """
    db = get_db()
    collection = db["sapp_trading_invoice_hourly_details"]

    query_filter = _build_sapp_time_filter(delivery_date, start_time, end_time)
    normalized_market = _normalize_trading_invoice_market(market)
    if normalized_market:
        query_filter["market"] = normalized_market

    total = collection.count_documents(query_filter)
    records = list(
        collection.find(query_filter)
        .sort([("timestamp", 1), ("market", 1)])
        .skip(skip)
        .limit(limit)
    )
    records = [_serialize_result(record) for record in records]

    page = (skip // limit) + 1
    return SappTradingInvoiceHourlyDetailList(
        records=records,
        total=total,
        page=page,
        page_size=limit,
    )


@router.get(
    "/trading-invoice-credit-notes/{delivery_date}/hourly-details",
    response_model=list[SappTradingInvoiceHourlyDetailResponse],
)
def get_trading_invoice_hourly_details_for_day(
    delivery_date: date,
    market: Optional[str] = Query(None),
):
    """Get hourly SAPP trading invoice detail rows for one delivery date."""
    db = get_db()
    collection = db["sapp_trading_invoice_hourly_details"]

    query_filter = {"delivery_date": delivery_date.isoformat()}
    normalized_market = _normalize_trading_invoice_market(market)
    if normalized_market:
        query_filter["market"] = normalized_market

    records = list(
        collection.find(query_filter)
        .sort([("timestamp", 1), ("market", 1)])
    )
    if not records:
        detail = {
            "message": "No SAPP trading invoice hourly details found for this date.",
            "delivery_date": delivery_date.isoformat(),
        }
        if normalized_market:
            detail["market"] = normalized_market
            detail["valid_markets"] = list(TRADING_INVOICE_MARKETS)
        raise HTTPException(
            status_code=404,
            detail=detail,
        )

    return [_serialize_result(record) for record in records]


@router.get(
    "/trading-invoice-credit-notes/{delivery_date}",
    response_model=SappTradingInvoiceCreditNoteResponse,
)
def get_trading_invoice_credit_note_for_day(delivery_date: date):
    """Get one SAPP trading invoice / credit note summary for a delivery date."""
    db = get_db()
    collection = db["sapp_trading_invoice_credit_notes"]

    record = collection.find_one({"delivery_date": delivery_date.isoformat()})
    if not record:
        raise HTTPException(
            status_code=404,
            detail="No SAPP trading invoice / credit note found for this date",
        )

    return _serialize_result(record)
