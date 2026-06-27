"""
SAPP MTP constrained area result endpoints.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional

import httpx
from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import ValidationError

from app.core.time_of_use import (
    build_time_of_use_schedule,
    count_time_of_use_hours,
    get_time_of_use_period,
)
from app.core.config import settings
from app.db.database import get_db
from app.schemas.sapp import (
    BidStatus,
    SappBidMarket,
    SappBidCreate,
    SappBidComparisonResponse,
    SappBidList,
    SappBidResponse,
    SappSubmittedBidSummaryList,
    SappBidTemplateCreate,
    SappBidTemplateList,
    SappBidTemplateResponse,
    SappBidTemplateUpdate,
    SappBidUpdate,
    SappConstrainedAreaDayResponse,
    SappConstrainedAreaResultList,
    SappConstrainedAreaResultResponse,
    SappMarketOverviewDayResponse,
    SappMarketOverviewResponse,
    SappParticipantPortfolioResultList,
    SappParticipantPortfolioResultResponse,
    SappPublicHolidayResponse,
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
    run_area_results_test,
    run_extraction_job,
    run_extraction_job_for_date_range,
)

router = APIRouter(prefix="/sapp", tags=["sapp"])

Frequency = Literal["1h", "4h", "1d", "1w", "1mo", "1y"]
BOTSWANA_COUNTRY_CODE = "BW"
MONDAY = 0
FRIDAY = 4
WEDNESDAY = 2
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

PUBLIC_HOLIDAY_TYPES = {"Public"}


def _serialize_result(record: dict) -> dict:
    record["_id"] = str(record["_id"])
    return record


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _public_holidays_api_url(year: int) -> str:
    return (
        f"{settings.PUBLIC_HOLIDAYS_API_BASE_URL.rstrip('/')}"
        f"/PublicHolidays/{year}/{BOTSWANA_COUNTRY_CODE}"
    )


def _normalize_public_holiday(record: dict) -> dict:
    return {
        "date": record["date"],
        "local_name": record.get("localName") or record.get("local_name") or record["name"],
        "name": record["name"],
        "country_code": record.get("countryCode") or record.get("country_code") or BOTSWANA_COUNTRY_CODE,
        "fixed": bool(record.get("fixed", False)),
        "global_holiday": bool(record.get("global", record.get("global_holiday", True))),
        "counties": record.get("counties"),
        "launch_year": record.get("launchYear") or record.get("launch_year"),
        "types": record.get("types") or [],
    }


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
        if quantity_data["energy_mwh"] != 0:
            normalized_quantities.append(quantity_data)
    return normalized_quantities


def _calculate_weighted_average_price(
    weighted_quantities: list[tuple[dict, int]],
) -> Optional[float]:
    total_volume = sum(
        abs(quantity["energy_mwh"]) * hour_count
        for quantity, hour_count in weighted_quantities
    )
    if total_volume == 0:
        return None

    total_value = sum(
        abs(quantity["energy_mwh"]) * hour_count * quantity["price_usd_per_mwh"]
        for quantity, hour_count in weighted_quantities
    )
    return total_value / total_volume


@router.get(
    "/public-holidays",
    response_model=list[SappPublicHolidayResponse],
)
def list_botswana_public_holidays(
    year: int = Query(..., ge=1900, le=2100),
    public_only: bool = Query(True),
):
    """List Botswana public holidays for SAPP market calendar use."""
    try:
        response = httpx.get(_public_holidays_api_url(year), timeout=10)
        response.raise_for_status()
        records = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Public holidays provider returned "
                f"{exc.response.status_code}"
            ),
        )
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Public holidays provider is unavailable",
        )

    holidays = [_normalize_public_holiday(record) for record in records]
    if public_only:
        holidays = [
            holiday
            for holiday in holidays
            if not holiday["types"] or PUBLIC_HOLIDAY_TYPES.intersection(holiday["types"])
        ]
    return holidays


def _add_one_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    next_month_first = _add_one_month(date(year, month, 1))
    current_date = next_month_first - timedelta(days=1)
    while current_date.weekday() != weekday:
        current_date -= timedelta(days=1)
    return current_date


def _fpm_week_start_from_placement(reference_date: date) -> date:
    days_until_next_monday = (7 - reference_date.weekday()) % 7
    if days_until_next_monday == 0:
        days_until_next_monday = 7
    next_monday = reference_date + timedelta(days=days_until_next_monday)

    if reference_date.weekday() >= FRIDAY:
        return next_monday + timedelta(days=7)
    return next_monday


def _fpm_week_start_from_period_date(reference_date: date) -> date:
    days_since_monday = (reference_date.weekday() - MONDAY) % 7
    return reference_date - timedelta(days=days_since_monday)


def _fpm_week_bid_deadline(week_start_date: date) -> date:
    return week_start_date - timedelta(days=3)


def _fpm_month_bid_deadline(month_start_date: date) -> date:
    previous_month_last_day = month_start_date - timedelta(days=1)
    last_wednesday = _last_weekday_of_month(
        previous_month_last_day.year,
        previous_month_last_day.month,
        WEDNESDAY,
    )
    if (month_start_date - last_wednesday).days <= 5:
        return last_wednesday - timedelta(days=7)
    return last_wednesday


def _fpm_month_start_from_placement(reference_date: date) -> date:
    next_month_start = _add_one_month(date(reference_date.year, reference_date.month, 1))
    deadline = _fpm_month_bid_deadline(next_month_start)
    if reference_date <= deadline:
        return next_month_start
    return _add_one_month(next_month_start)


def _fpm_month_start_from_period_date(reference_date: date) -> date:
    return date(reference_date.year, reference_date.month, 1)


def _fpm_month_end(month_start_date: date) -> date:
    return _add_one_month(month_start_date) - timedelta(days=1)


def _bid_period_range(payload: SappBidCreate) -> tuple[date, date, int]:
    if payload.market == "dam":
        return payload.delivery_date, payload.delivery_date, 1
    if payload.market == "fpm_w":
        return payload.week_start_date, payload.week_start_date + timedelta(days=6), 7

    period_end_date = _fpm_month_end(payload.month_start_date)
    delivery_days = (period_end_date - payload.month_start_date).days + 1
    return payload.month_start_date, period_end_date, delivery_days


def _bid_fields_from_payload(payload: SappBidCreate) -> dict:
    quantities = _normalize_quantities(payload.quantities)
    period_start_date, period_end_date, delivery_days = _bid_period_range(payload)
    period_hour_counts = count_time_of_use_hours(period_start_date, period_end_date)

    if payload.market == "dam":
        weighted_quantities = [(quantity, 1) for quantity in quantities]
        daily_energy = sum(quantity["energy_mwh"] for quantity in quantities)
        total_energy = daily_energy
        period_energy = {"off_peak": 0, "standard": 0, "peak": 0}
        for quantity in quantities:
            product = get_time_of_use_period(payload.delivery_date, quantity["hour"])
            period_energy[product] += quantity["energy_mwh"]
    else:
        weighted_quantities = [
            (quantity, period_hour_counts[quantity["product"]])
            for quantity in quantities
        ]
        total_energy = sum(
            quantity["energy_mwh"] * hour_count
            for quantity, hour_count in weighted_quantities
        )
        daily_energy = total_energy / delivery_days if delivery_days else 0
        period_energy = {"off_peak": 0, "standard": 0, "peak": 0}
        for quantity, hour_count in weighted_quantities:
            period_energy[quantity["product"]] += quantity["energy_mwh"] * hour_count

    weighted_average_price = _calculate_weighted_average_price(weighted_quantities)
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
        "period_hour_counts": period_hour_counts,
        "period_energy_mwh": period_energy,
        "price_columns": payload.price_columns,
        "quantities": quantities,
        "template_id": payload.template_id,
        "notes": payload.notes,
        "daily_energy_mwh": daily_energy,
        "total_energy_mwh": total_energy,
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


def _serialize_bid_result(record: dict) -> dict:
    serialized_record = _serialize_result(record)
    serialized_record.setdefault("market", "dam")
    if "delivery_date" in serialized_record:
        serialized_record.setdefault("period_start_date", serialized_record["delivery_date"])
        serialized_record.setdefault("period_end_date", serialized_record["delivery_date"])
    serialized_record.setdefault("delivery_days", 1)
    serialized_record.setdefault(
        "period_hour_counts",
        {"off_peak": 0, "standard": 0, "peak": 0},
    )
    serialized_record.setdefault(
        "period_energy_mwh",
        {"off_peak": 0, "standard": 0, "peak": 0},
    )
    serialized_record.setdefault(
        "daily_energy_mwh",
        serialized_record.get("total_energy_mwh", 0),
    )
    return serialized_record


def _price_key(price: float) -> str:
    if float(price).is_integer():
        return str(int(price))
    return str(price)


def _hour_label(hour: int) -> str:
    return f"{hour - 1:02d}-{hour:02d}" if hour < 24 else "23-24"


def _to_float(value) -> float:
    return float(value) if value is not None else 0


def _date_from_bid_field(bid: dict, field_name: str) -> Optional[date]:
    value = bid.get(field_name)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _bid_date_range(bid: dict) -> tuple[date, date]:
    start_date = _date_from_bid_field(bid, "period_start_date")
    end_date = _date_from_bid_field(bid, "period_end_date")
    if start_date and end_date:
        return start_date, end_date

    delivery_date = _date_from_bid_field(bid, "delivery_date")
    if delivery_date:
        return delivery_date, delivery_date

    raise HTTPException(status_code=400, detail="Bid does not have a usable date range")


def _invoice_hourly_records(db, market: str, start_date: date, end_date: date) -> list[dict]:
    query_filter = {
        "market": market,
        "delivery_date": {
            "$gte": start_date.isoformat(),
            "$lte": end_date.isoformat(),
        },
    }
    return list(
        db["sapp_trading_invoice_hourly_details"]
        .find(query_filter)
        .sort([("delivery_date", 1), ("hour", 1)])
    )


def _portfolio_hourly_records(db, delivery_date: date) -> list[dict]:
    return list(
        db["sapp_participant_portfolio_results"]
        .find({"delivery_date": delivery_date.isoformat()})
        .sort("hour", 1)
    )


def _summarize_invoice_results(records: list[dict]) -> dict:
    purchase_mwh = sum(_to_float(record.get("traded_purchases_mwh")) for record in records)
    sale_mwh = sum(_to_float(record.get("traded_sales_mwh")) for record in records)
    purchase_amount = sum(_to_float(record.get("purchase_turnover_usd")) for record in records)
    sale_amount = sum(_to_float(record.get("sale_turnover_usd")) for record in records)
    return {
        "result_purchase_mwh": purchase_mwh,
        "result_sale_mwh": sale_mwh,
        "result_purchase_amount_usd": purchase_amount,
        "result_sale_amount_usd": sale_amount,
        "result_net_mwh": purchase_mwh - sale_mwh,
        "result_net_amount_usd": purchase_amount - sale_amount,
        "results_available": bool(records),
    }


def _submitted_bid_summary(bid: dict, db) -> dict:
    start_date, end_date = _bid_date_range(bid)
    invoice_summary = _summarize_invoice_results(
        _invoice_hourly_records(db, bid.get("market", "dam"), start_date, end_date)
    )
    return {
        "id": str(bid["_id"]),
        "market": bid.get("market", "dam"),
        "delivery_date": bid.get("delivery_date"),
        "week_start_date": bid.get("week_start_date"),
        "month_start_date": bid.get("month_start_date"),
        "period_start_date": bid.get("period_start_date") or bid.get("delivery_date"),
        "period_end_date": bid.get("period_end_date") or bid.get("delivery_date"),
        "delivery_days": bid.get("delivery_days", 1),
        "total_bid_energy_mwh": bid.get("total_energy_mwh", 0),
        "weighted_average_bid_price_usd_per_mwh": bid.get(
            "weighted_average_price_usd_per_mwh"
        ),
        "submitted_at": bid.get("submitted_at"),
        "created_at": bid["created_at"],
        "updated_at": bid["updated_at"],
        **invoice_summary,
    }


def _build_bid_hour_rows(bid: dict, delivery_date: date) -> list[dict]:
    quantities = bid.get("quantities", [])
    price_columns = bid.get("price_columns", [])
    market = bid.get("market", "dam")
    rows = []

    for hour in range(1, 25):
        product = get_time_of_use_period(delivery_date, hour)
        quantities_by_price = {_price_key(price): 0 for price in price_columns}

        for quantity in quantities:
            if market == "dam" and quantity.get("hour") != hour:
                continue
            if market != "dam" and quantity.get("product") != product:
                continue
            price_key = _price_key(quantity["price_usd_per_mwh"])
            quantities_by_price[price_key] = (
                quantities_by_price.get(price_key, 0) + quantity["energy_mwh"]
            )

        rows.append(
            {
                "delivery_date": delivery_date.isoformat(),
                "hour": hour,
                "hour_label": _hour_label(hour),
                "product": product,
                "quantities_by_price": quantities_by_price,
                "total_energy_mwh": sum(quantities_by_price.values()),
            }
        )

    return rows


def _build_result_hour_rows(db, market: str, delivery_date: date) -> list[dict]:
    invoice_records = {
        record.get("hour"): record
        for record in _invoice_hourly_records(db, market, delivery_date, delivery_date)
    }
    portfolio_records = {
        record.get("hour"): record for record in _portfolio_hourly_records(db, delivery_date)
    }
    rows = []

    for hour in range(1, 25):
        product = get_time_of_use_period(delivery_date, hour)
        invoice_record = invoice_records.get(hour, {})
        portfolio_record = portfolio_records.get(hour, {})
        purchase_mwh = invoice_record.get("traded_purchases_mwh")
        sale_mwh = invoice_record.get("traded_sales_mwh")
        area_price = portfolio_record.get("area_price_usd_per_mwh")
        participant_schedule = portfolio_record.get("participant_total_area_schedule_mwh")

        rows.append(
            {
                "delivery_date": delivery_date.isoformat(),
                "hour": hour,
                "hour_label": _hour_label(hour),
                "product": product,
                "market": market,
                "purchase_mwh": purchase_mwh,
                "sale_mwh": sale_mwh,
                "net_mwh": (
                    _to_float(purchase_mwh) - _to_float(sale_mwh)
                    if purchase_mwh is not None or sale_mwh is not None
                    else None
                ),
                "purchase_amount_usd": invoice_record.get("purchase_turnover_usd"),
                "sale_amount_usd": invoice_record.get("sale_turnover_usd"),
                "area_price_usd_per_mwh": area_price,
                "unconstrained_market_price_usd_per_mwh": portfolio_record.get(
                    "unconstrained_market_price_usd_per_mwh"
                ),
                "participant_total_area_schedule_mwh": participant_schedule,
                "admin_fees_usd": invoice_record.get("admin_fees_usd"),
                "wheeling_cost_usd": invoice_record.get("wheeling_cost_usd"),
            }
        )

    return rows


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
    start_date: Optional[date],
    end_date: Optional[date],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
) -> dict:
    query_filter = {}
    if delivery_date:
        if start_date or end_date:
            raise HTTPException(
                status_code=400,
                detail="Use either delivery_date or start_date/end_date, not both",
            )
        query_filter["delivery_date"] = delivery_date.isoformat()
    elif start_date or end_date:
        query_filter["delivery_date"] = {}
        if start_date:
            query_filter["delivery_date"]["$gte"] = start_date.isoformat()
        if end_date:
            query_filter["delivery_date"]["$lte"] = end_date.isoformat()
    if start_time or end_time:
        query_filter["timestamp"] = {}
        if start_time:
            query_filter["timestamp"]["$gte"] = start_time
        if end_time:
            query_filter["timestamp"]["$lte"] = end_time
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="end_date must be greater than or equal to start_date",
        )
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


def _aggregate_unconstrained_area_results(
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
            "total_purchase_volume_mw": {"$avg": "$total_purchase_volume_mw"},
            "total_sales_volume_mw": {"$avg": "$total_sales_volume_mw"},
            "price_usd_per_mwh": {"$avg": "$price_usd_per_mwh"},
            "price_zar_per_mwh": {"$avg": "$price_zar_per_mwh"},
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
                        "total_purchase_volume_mw": "$total_purchase_volume_mw",
                        "total_sales_volume_mw": "$total_sales_volume_mw",
                        "price_usd_per_mwh": "$price_usd_per_mwh",
                        "price_zar_per_mwh": "$price_zar_per_mwh",
                        "data_source": "SAPP_MTP_DAM_UNCONSTRAINED_RESULTS",
                    }
                },
            ]
        )
    )
    return records, total


def _scrape_job_names_for_request(job_name: str) -> list[str]:
    if job_name == "constrained_area_results":
        return ["constrained_area_results", "unconstrained_area_results"]
    return [job_name]


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
    return _serialize_bid_result(record)


@router.get("/time-of-use-periods")
def get_time_of_use_periods(
    delivery_date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    """Get time-of-use period rules for one date or an inclusive date range."""
    if delivery_date:
        if start_date or end_date:
            raise HTTPException(
                status_code=400,
                detail="Use either delivery_date or start_date/end_date, not both",
            )
        counts = count_time_of_use_hours(delivery_date, delivery_date)
        return {
            "delivery_date": delivery_date.isoformat(),
            "period_hour_counts": counts,
            "hours": build_time_of_use_schedule(delivery_date),
        }

    if not start_date or not end_date:
        raise HTTPException(
            status_code=400,
            detail="Provide delivery_date or both start_date and end_date",
        )
    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="end_date must be greater than or equal to start_date",
        )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "period_hour_counts": count_time_of_use_hours(start_date, end_date),
    }


@router.get("/bid-trading-period")
def get_bid_trading_period(
    market: SappBidMarket = Query(...),
    reference_date: date = Query(...),
    mode: Literal["placement", "period"] = Query(
        "placement",
        description=(
            "placement applies bid deadline rules; period resolves the trading "
            "period containing reference_date for reconstruction."
        ),
    ),
):
    """Resolve the bid trading period from a bid placement/reference date."""
    if market == "dam":
        period_start_date = reference_date
        period_end_date = reference_date
        request_date_field = "delivery_date"
        bid_deadline_date = reference_date
    elif market == "fpm_w":
        if mode == "period":
            period_start_date = _fpm_week_start_from_period_date(reference_date)
        else:
            period_start_date = _fpm_week_start_from_placement(reference_date)
        period_end_date = period_start_date + timedelta(days=6)
        request_date_field = "week_start_date"
        bid_deadline_date = _fpm_week_bid_deadline(period_start_date)
    else:
        if mode == "period":
            period_start_date = _fpm_month_start_from_period_date(reference_date)
        else:
            period_start_date = _fpm_month_start_from_placement(reference_date)
        period_end_date = _fpm_month_end(period_start_date)
        request_date_field = "month_start_date"
        bid_deadline_date = _fpm_month_bid_deadline(period_start_date)

    return {
        "market": market,
        "mode": mode,
        "reference_date": reference_date.isoformat(),
        "bid_deadline_date": bid_deadline_date.isoformat(),
        "period_start_date": period_start_date.isoformat(),
        "period_end_date": period_end_date.isoformat(),
        "delivery_days": (period_end_date - period_start_date).days + 1,
        "request_date_field": request_date_field,
        "request_date_value": period_start_date.isoformat(),
        "period_hour_counts": count_time_of_use_hours(
            period_start_date,
            period_end_date,
        ),
    }


@router.get("/submitted-bids/summary", response_model=SappSubmittedBidSummaryList)
def list_submitted_bid_summaries(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    market: Optional[SappBidMarket] = Query(None),
    delivery_date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    """List submitted bid summaries without hourly bid construction data."""
    db = get_db()
    collection = db["sapp_bids"]

    query_filter = {"status": "submitted"}
    if market:
        query_filter["market"] = market
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
        .sort([("period_start_date", -1), ("submitted_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(limit)
    )

    page = (skip // limit) + 1
    return SappSubmittedBidSummaryList(
        records=[_submitted_bid_summary(record, db) for record in records],
        total=total,
        page=page,
        page_size=limit,
    )


@router.get(
    "/submitted-bids/{bid_id}/comparison",
    response_model=SappBidComparisonResponse,
)
def get_submitted_bid_comparison(
    bid_id: str,
    delivery_date: Optional[date] = Query(
        None,
        description="Required for a specific FPM-W/FPM-M day; defaults to bid date for DAM.",
    ),
):
    """Get hourly bid construction and matching SAPP purchase/sale results."""
    db = get_db()
    collection = db["sapp_bids"]
    bid = collection.find_one(
        {"_id": _parse_object_id(bid_id, "bid"), "status": "submitted"}
    )
    if not bid:
        raise HTTPException(status_code=404, detail="Submitted bid not found")

    start_date, end_date = _bid_date_range(bid)
    comparison_date = delivery_date
    if comparison_date is None:
        comparison_date = start_date
    if comparison_date < start_date or comparison_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="delivery_date must fall inside the submitted bid period",
        )

    return {
        "bid": _serialize_bid_result(dict(bid)),
        "summary": _submitted_bid_summary(bid, db),
        "delivery_date": comparison_date.isoformat(),
        "bid_hours": _build_bid_hour_rows(bid, comparison_date),
        "result_hours": _build_result_hour_rows(
            db,
            bid.get("market", "dam"),
            comparison_date,
        ),
    }


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
    records = [_serialize_bid_result(record) for record in records]

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

    return _serialize_bid_result(record)


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
    return _serialize_bid_result(record)


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
    if not existing.get("quantities"):
        raise HTTPException(
            status_code=400,
            detail="Cannot submit a bid with no non-zero energy quantities",
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
    return _serialize_bid_result(record)


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
    "/area-results-test",
    summary="Test the new SAPP area results navigation flow locally",
    description=(
        "Runs Selenium locally, logs into SAPP, navigates directly to the "
        "area-results test URL, fills Delivery Day, sets Category to Price in USD, "
        "searches unconstrained and constrained area results, extracts the visible "
        "Handsontable data, and returns the resulting browser URL and table payloads. "
        "This is a test route only and does not download or store data."
    ),
)
def area_results_test(
    delivery_date: Optional[date] = Query(
        None,
        description="Delivery day to enter into the page. Defaults to today's date.",
    ),
    target_text: Optional[str] = Query(
        None,
        description=(
            "Optional exact visible text for the menu span. If omitted, the first "
            "visible span matching lpx-menu-item-text hidden-in-hover-trigger is clicked."
        ),
    ),
    timeout: int = Query(
        20,
        ge=1,
        le=120,
        description="Seconds to wait for the menu span and page load.",
    ),
):
    """Login and navigate directly to the area results test page. Run locally."""
    try:
        return run_area_results_test(
            target_text=target_text,
            timeout=timeout,
            delivery_date=delivery_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
    ),
):
    """Download, parse, and upsert one SAPP document. Run locally."""
    try:
        requested_job_names = _scrape_job_names_for_request(job_name)
        results = [
            run_extraction_job(
                get_extraction_job(requested_job_name),
                delivery_date=delivery_date,
                page_start=page_start,
            )
            for requested_job_name in requested_job_names
        ]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    primary_result = dict(results[0])
    primary_result.update(
        {
            "job": job_name,
            "requested_jobs": requested_job_names,
            "successful_jobs": len(results),
            "failed_jobs": 0,
            "imported": sum(result.get("imported", 0) for result in results),
            "updated": sum(result.get("updated", 0) for result in results),
            "related_results": results,
        }
    )
    return primary_result


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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
    ),
):
    """Download, parse, and upsert SAPP documents for a date range. Run locally."""
    try:
        requested_job_names = _scrape_job_names_for_request(job_name)
        range_results = [
            run_extraction_job_for_date_range(
                get_extraction_job(requested_job_name),
                start_date=start_date,
                end_date=end_date,
                continue_on_error=continue_on_error,
                page_start=page_start,
            )
            for requested_job_name in requested_job_names
        ]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    merged_results = [
        result
        for range_result in range_results
        for result in range_result.get("results", [])
    ]
    successful_results = [
        result for result in merged_results if result.get("status") == "success"
    ]
    failed_results = [
        result for result in merged_results if result.get("status") == "failed"
    ]
    return {
        "job": job_name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "page_start": page_start,
        "requested_dates": len(merged_results),
        "successful_dates": len(successful_results),
        "failed_dates": len(failed_results),
        "imported": sum(result.get("imported", 0) for result in successful_results),
        "updated": sum(result.get("updated", 0) for result in successful_results),
        "results": merged_results,
        "requested_jobs": requested_job_names,
        "successful_jobs": len(
            [result for result in range_results if result.get("failed_dates", 0) == 0]
        ),
        "failed_jobs": len(
            [result for result in range_results if result.get("failed_dates", 0) > 0]
        ),
        "related_results": range_results,
    }


@router.get("/constrained-area-results", response_model=SappConstrainedAreaResultList)
def list_constrained_area_results(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
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
    unconstrained_collection = db["sapp_unconstrained_area_results"]

    query_filter = _build_sapp_time_filter(
        delivery_date,
        start_date,
        end_date,
        start_time,
        end_time,
    )

    if frequency == "1h":
        total = collection.count_documents(query_filter)
        records = list(
            collection.find(query_filter)
            .sort("timestamp", 1)
            .skip(skip)
            .limit(limit)
        )
        records = [_serialize_result(record) for record in records]
        unconstrained_total = unconstrained_collection.count_documents(query_filter)
        unconstrained_records = list(
            unconstrained_collection.find(query_filter)
            .sort("timestamp", 1)
            .skip(skip)
            .limit(limit)
        )
        unconstrained_records = [
            _serialize_result(record) for record in unconstrained_records
        ]
    else:
        records, total = _aggregate_constrained_area_results(
            collection,
            query_filter,
            frequency,
            skip,
            limit,
        )
        unconstrained_records, unconstrained_total = _aggregate_unconstrained_area_results(
            unconstrained_collection,
            query_filter,
            frequency,
            skip,
            limit,
        )

    page = (skip // limit) + 1
    return SappConstrainedAreaResultList(
        records=records,
        unconstrained_records=unconstrained_records,
        total=total,
        unconstrained_total=unconstrained_total,
        page=page,
        page_size=limit,
    )


@router.get("/market-overview", response_model=SappMarketOverviewResponse)
def get_market_overview(
    delivery_date: Optional[date] = Query(
        None,
        description="Single delivery date to return. Use this or start_date/end_date.",
    ),
    start_date: Optional[date] = Query(
        None,
        description="First delivery date in the requested range.",
    ),
    end_date: Optional[date] = Query(
        None,
        description="Last delivery date in the requested range, inclusive.",
    ),
):
    """Return constrained and unconstrained area results grouped by delivery date."""
    if delivery_date:
        if start_date or end_date:
            raise HTTPException(
                status_code=400,
                detail="Use either delivery_date or start_date/end_date, not both",
            )
        normalized_start_date = delivery_date
        normalized_end_date = delivery_date
    else:
        if not start_date and not end_date:
            raise HTTPException(
                status_code=400,
                detail="Provide delivery_date or start_date/end_date",
            )
        normalized_start_date = start_date or end_date
        normalized_end_date = end_date or start_date
        if normalized_start_date is None or normalized_end_date is None:
            raise HTTPException(
                status_code=400,
                detail="Provide delivery_date or start_date/end_date",
            )
        if normalized_end_date < normalized_start_date:
            raise HTTPException(
                status_code=400,
                detail="end_date must be greater than or equal to start_date",
            )

    db = get_db()
    constrained_collection = db["sapp_constrained_area_results"]
    unconstrained_collection = db["sapp_unconstrained_area_results"]

    query_filter = _build_sapp_time_filter(
        None,
        normalized_start_date,
        normalized_end_date,
        None,
        None,
    )

    constrained_records = [
        _serialize_result(record)
        for record in constrained_collection.find(query_filter).sort(
            [("delivery_date", 1), ("timestamp", 1), ("hour", 1)]
        )
    ]
    unconstrained_records = [
        _serialize_result(record)
        for record in unconstrained_collection.find(query_filter).sort(
            [("delivery_date", 1), ("timestamp", 1), ("hour", 1)]
        )
    ]

    days_by_date: dict[str, dict] = {}
    current_date = normalized_start_date
    while current_date <= normalized_end_date:
        days_by_date[current_date.isoformat()] = {
            "delivery_date": current_date,
            "constrained_records": [],
            "unconstrained_records": [],
        }
        current_date += timedelta(days=1)

    for record in constrained_records:
        record_date = record["delivery_date"]
        days_by_date.setdefault(
            record_date,
            {
                "delivery_date": date.fromisoformat(record_date),
                "constrained_records": [],
                "unconstrained_records": [],
            },
        )["constrained_records"].append(record)

    for record in unconstrained_records:
        record_date = record["delivery_date"]
        days_by_date.setdefault(
            record_date,
            {
                "delivery_date": date.fromisoformat(record_date),
                "constrained_records": [],
                "unconstrained_records": [],
            },
        )["unconstrained_records"].append(record)

    days = []
    for delivery_date_key in sorted(days_by_date.keys()):
        day = days_by_date[delivery_date_key]
        days.append(
            SappMarketOverviewDayResponse(
                delivery_date=day["delivery_date"],
                constrained_records=day["constrained_records"],
                unconstrained_records=day["unconstrained_records"],
                constrained_count=len(day["constrained_records"]),
                unconstrained_count=len(day["unconstrained_records"]),
            )
        )

    return SappMarketOverviewResponse(
        delivery_date=delivery_date,
        start_date=normalized_start_date,
        end_date=normalized_end_date,
        day_count=len(days),
        constrained_total=len(constrained_records),
        unconstrained_total=len(unconstrained_records),
        days=days,
    )


@router.get(
    "/constrained-area-results/{delivery_date}",
    response_model=SappConstrainedAreaDayResponse,
)
def get_constrained_area_results_for_day(delivery_date: date):
    """Get hourly SAPP constrained and unconstrained area results for one date."""
    db = get_db()
    collection = db["sapp_constrained_area_results"]
    unconstrained_collection = db["sapp_unconstrained_area_results"]

    start = datetime.combine(delivery_date, time.min)
    end = datetime.combine(delivery_date, time.max)
    records = list(
        collection.find({"timestamp": {"$gte": start, "$lte": end}})
        .sort("timestamp", 1)
    )
    unconstrained_records = list(
        unconstrained_collection.find({"timestamp": {"$gte": start, "$lte": end}})
        .sort("timestamp", 1)
    )
    if not records and not unconstrained_records:
        raise HTTPException(status_code=404, detail="No SAPP results found for this date")

    return {
        "delivery_date": delivery_date.isoformat(),
        "constrained_records": [_serialize_result(record) for record in records],
        "unconstrained_records": [
            _serialize_result(record) for record in unconstrained_records
        ],
    }


@router.get(
    "/participant-portfolio-results",
    response_model=SappParticipantPortfolioResultList,
)
def list_participant_portfolio_results(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    delivery_date: Optional[date] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List SAPP participant portfolio results with optional date filtering.
    """
    db = get_db()
    collection = db["sapp_participant_portfolio_results"]

    query_filter = _build_sapp_time_filter(
        delivery_date,
        start_date,
        end_date,
        start_time,
        end_time,
    )
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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
    ),
):
    """Download, parse, and upsert one SAPP participant portfolio document."""
    try:
        job = get_extraction_job("participant_portfolio_results")
        result = run_extraction_job(
            job,
            delivery_date=delivery_date,
            page_start=page_start,
        )
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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
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
            page_start=page_start,
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
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List SAPP trading invoice / credit note summaries with optional date filtering.
    """
    db = get_db()
    collection = db["sapp_trading_invoice_credit_notes"]

    query_filter = _build_sapp_time_filter(
        delivery_date,
        start_date,
        end_date,
        start_time,
        end_time,
    )
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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
    ),
):
    """Download, parse, and upsert one SAPP trading invoice / credit note workbook."""
    try:
        job = get_extraction_job("trading_invoice_credit_note")
        result = run_extraction_job(
            job,
            delivery_date=delivery_date,
            page_start=page_start,
        )
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
    page_start: int = Query(
        1,
        ge=1,
        description="Inbox page number to start searching from.",
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
            page_start=page_start,
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
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    market: Optional[str] = Query(None),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
):
    """
    List hourly SAPP trading invoice detail rows with optional date and market filtering.
    """
    db = get_db()
    collection = db["sapp_trading_invoice_hourly_details"]

    query_filter = _build_sapp_time_filter(
        delivery_date,
        start_date,
        end_date,
        start_time,
        end_time,
    )
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
