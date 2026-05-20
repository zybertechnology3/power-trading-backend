"""
Resource forecasting endpoints.
"""

import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.dam_calculations import (
    calculate_dam_projection,
    list_dam_calculation_configs,
)
from app.db.database import get_db
from app.schemas.resource_forecasting import (
    DamCalculationConfigResponse,
    DamCalculationRequest,
    DamCalculationResponse,
    LevelMonitoringAggregationRecord,
    LevelMonitoringAggregationResponse,
    LevelMonitoringFieldCreate,
    LevelMonitoringFieldResponse,
    LevelMonitoringFieldUpdate,
    LevelMonitoringListResponse,
    LevelMonitoringRecordCreate,
    LevelMonitoringRecordResponse,
    LevelMonitoringRecordUpdate,
    ReservoirCode,
    ReservoirInfo,
    ResourceAggregationGroup,
)

router = APIRouter(prefix="/resource-forecasting", tags=["resource-forecasting"])

FT_TO_M3_FACTOR = 2393.89

RESERVOIRS: dict[ReservoirCode, dict[str, float | str]] = {
    "mps": {"name": "MPS", "min_level_ft": 570.0, "max_level_ft": 641.5},
    "lps": {"name": "LPS", "min_level_ft": 140.0, "max_level_ft": 233.0},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _records_collection():
    return get_db()["resource_level_monitoring_records"]


def _fields_collection():
    return get_db()["resource_level_monitoring_fields"]


def _active_filter() -> dict:
    return {"deleted_at": {"$exists": False}}


def _encode_cursor(record: dict) -> str:
    cursor_payload = {
        "record_date": record["record_date"],
        "_id": str(record["_id"]),
    }
    raw_cursor = json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_cursor).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    try:
        decoded_cursor = base64.urlsafe_b64decode(cursor.encode("ascii"))
        cursor_payload = json.loads(decoded_cursor.decode("utf-8"))
        record_date = date.fromisoformat(cursor_payload["record_date"]).isoformat()
        record_id = _parse_object_id(cursor_payload["_id"], "cursor")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")

    return {"record_date": record_date, "_id": record_id}


def _slugify_field_key(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", label.strip().lower())
    return normalized.strip("_")


def _field_response(record: dict) -> dict:
    return {
        "id": str(record["_id"]),
        "reservoir": record["reservoir"],
        "key": record["key"],
        "label": record["label"],
        "field_type": record["field_type"],
        "unit": record.get("unit"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _field_definitions(reservoir: ReservoirCode) -> list[dict]:
    records = list(
        _fields_collection()
        .find({"reservoir": reservoir, **_active_filter()})
        .sort([("created_at", 1), ("_id", 1)])
    )
    return [_field_response(record) for record in records]


def _custom_fields_with_defaults(
    record_custom_fields: Optional[dict[str, Any]],
    field_definitions: list[dict],
) -> dict[str, Any]:
    values = dict(record_custom_fields or {})
    for field_definition in field_definitions:
        values.setdefault(field_definition["key"], None)
    return values


def _parse_record_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _ft_to_m3(value: float) -> float:
    return value * FT_TO_M3_FACTOR


def _m3_to_ft(value: float) -> float:
    return value / FT_TO_M3_FACTOR


def _reservoir_max_level_ft(reservoir: ReservoirCode) -> float:
    return float(RESERVOIRS[reservoir]["max_level_ft"])


def _reservoir_min_level_ft(reservoir: ReservoirCode) -> float:
    return float(RESERVOIRS[reservoir]["min_level_ft"])


def _reservoir_max_level_m3(reservoir: ReservoirCode) -> float:
    return _ft_to_m3(_reservoir_max_level_ft(reservoir))


def _reservoir_min_level_m3(reservoir: ReservoirCode) -> float:
    return _ft_to_m3(_reservoir_min_level_ft(reservoir))


def _level_to_ft(value: float, unit: str) -> float:
    return value if unit == "ft" else _m3_to_ft(value)


def _level_to_m3(value: float, unit: str) -> float:
    return value if unit == "m3" else _ft_to_m3(value)


def _validate_reservoir_level_limit(
    reservoir: ReservoirCode,
    value: float,
    unit: str,
) -> None:
    if value < 0:
        raise HTTPException(status_code=400, detail="reservoir_level_value must be non-negative")

    level_ft = _level_to_ft(value, unit)
    min_level_ft = _reservoir_min_level_ft(reservoir)
    max_level_ft = _reservoir_max_level_ft(reservoir)
    if level_ft < min_level_ft or level_ft > max_level_ft:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{reservoir.upper()} reservoir level must be between "
                f"{min_level_ft:g} ft and {max_level_ft:g} ft"
            ),
        )


def _record_response(record: dict, field_definitions: Optional[list[dict]] = None) -> dict:
    if field_definitions is None:
        field_definitions = _field_definitions(record["reservoir"])

    level_value = record["reservoir_level_value"]
    level_unit = record["reservoir_level_unit"]
    reservoir = record["reservoir"]
    return {
        "id": str(record["_id"]),
        "reservoir": reservoir,
        "record_date": record["record_date"],
        "daily_inflow": record["daily_inflow"],
        "unaccounted_inflow": record["unaccounted_inflow"],
        "total_daily_inflow": record.get(
            "total_daily_inflow",
            record["daily_inflow"] + record["unaccounted_inflow"],
        ),
        "reservoir_level_value": level_value,
        "reservoir_level_unit": level_unit,
        "reservoir_level_ft": _level_to_ft(level_value, level_unit),
        "reservoir_level_m3": _level_to_m3(level_value, level_unit),
        "min_level_ft": _reservoir_min_level_ft(reservoir),
        "min_level_m3": _reservoir_min_level_m3(reservoir),
        "max_level_ft": _reservoir_max_level_ft(reservoir),
        "max_level_m3": _reservoir_max_level_m3(reservoir),
        "custom_fields": _custom_fields_with_defaults(
            record.get("custom_fields"),
            field_definitions,
        ),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _validate_custom_field_keys(reservoir: ReservoirCode, custom_fields: dict[str, Any]) -> None:
    if not custom_fields:
        return

    configured_keys = {
        field["key"]
        for field in _fields_collection().find(
            {"reservoir": reservoir, **_active_filter()},
            {"key": 1},
        )
    }
    unknown_keys = sorted(set(custom_fields) - configured_keys)
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown custom field key(s) for {reservoir}: {', '.join(unknown_keys)}",
        )


def _period_bounds(record_date: date, group_by: ResourceAggregationGroup) -> tuple[date, date]:
    if group_by == "week":
        start = record_date - timedelta(days=record_date.weekday())
        return start, start + timedelta(days=6)
    if group_by == "month":
        start = record_date.replace(day=1)
        next_month = (
            date(record_date.year + 1, 1, 1)
            if record_date.month == 12
            else date(record_date.year, record_date.month + 1, 1)
        )
        return start, next_month - timedelta(days=1)
    return date(record_date.year, 1, 1), date(record_date.year, 12, 31)


@router.get("/reservoirs", response_model=list[ReservoirInfo])
def list_reservoirs():
    """List reservoirs available for resource forecasting."""
    return [
        ReservoirInfo(
            code=reservoir_code,
            name=str(reservoir["name"]),
            min_level_ft=float(reservoir["min_level_ft"]),
            min_level_m3=_reservoir_min_level_m3(reservoir_code),
            max_level_ft=float(reservoir["max_level_ft"]),
            max_level_m3=_reservoir_max_level_m3(reservoir_code),
        )
        for reservoir_code, reservoir in RESERVOIRS.items()
    ]


@router.post(
    "/level-monitoring/fields",
    response_model=LevelMonitoringFieldResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_level_monitoring_field(payload: LevelMonitoringFieldCreate):
    """Create a persistent extra field for a reservoir."""
    now = _utcnow()
    key = _slugify_field_key(payload.key or payload.label)
    if not key:
        raise HTTPException(status_code=400, detail="Field key must not be blank")

    document = {
        "reservoir": payload.reservoir,
        "key": key,
        "label": payload.label,
        "field_type": payload.field_type,
        "unit": payload.unit,
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = _fields_collection().insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A field with this key already exists for this reservoir",
        )

    record = _fields_collection().find_one({"_id": result.inserted_id})
    return _field_response(record)


@router.get(
    "/level-monitoring/fields",
    response_model=list[LevelMonitoringFieldResponse],
)
def list_level_monitoring_fields(
    reservoir: Optional[ReservoirCode] = Query(None),
):
    """List persistent extra fields."""
    query_filter = _active_filter()
    if reservoir:
        query_filter["reservoir"] = reservoir

    records = list(
        _fields_collection()
        .find(query_filter)
        .sort([("reservoir", 1), ("created_at", 1), ("_id", 1)])
    )
    return [_field_response(record) for record in records]


@router.patch(
    "/level-monitoring/fields/{field_id}",
    response_model=LevelMonitoringFieldResponse,
)
def update_level_monitoring_field(field_id: str, payload: LevelMonitoringFieldUpdate):
    """Update an extra field definition."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No field values provided")

    update_data["updated_at"] = _utcnow()
    record = _fields_collection().find_one_and_update(
        {"_id": _parse_object_id(field_id, "field"), **_active_filter()},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Field not found")
    return _field_response(record)


@router.post(
    "/level-monitoring/records",
    response_model=LevelMonitoringRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_level_monitoring_record(payload: LevelMonitoringRecordCreate):
    """Create a daily level monitoring record."""
    _validate_custom_field_keys(payload.reservoir, payload.custom_fields)
    _validate_reservoir_level_limit(
        payload.reservoir,
        payload.reservoir_level_value,
        payload.reservoir_level_unit,
    )
    now = _utcnow()
    document = payload.model_dump()
    document["record_date"] = payload.record_date.isoformat()
    document["total_daily_inflow"] = payload.daily_inflow + payload.unaccounted_inflow
    document["created_at"] = now
    document["updated_at"] = now

    try:
        result = _records_collection().insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A level monitoring record already exists for this reservoir and date",
        )

    record = _records_collection().find_one({"_id": result.inserted_id})
    return _record_response(record)


@router.get(
    "/level-monitoring/records",
    response_model=LevelMonitoringListResponse,
)
def list_level_monitoring_records(
    reservoir: ReservoirCode = Query(...),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
):
    """List reservoir level records by date range."""
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    query_filter = {"reservoir": reservoir, **_active_filter()}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date.isoformat()
        if end_date:
            date_filter["$lte"] = end_date.isoformat()
        query_filter["record_date"] = date_filter
    if cursor:
        decoded_cursor = _decode_cursor(cursor)
        query_filter["$or"] = [
            {"record_date": {"$lt": decoded_cursor["record_date"]}},
            {
                "record_date": decoded_cursor["record_date"],
                "_id": {"$lt": decoded_cursor["_id"]},
            },
        ]

    skip = ((page - 1) * limit) if page and not cursor else 0
    records = list(
        _records_collection()
        .find(query_filter)
        .sort([("record_date", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit + 1)
    )

    next_cursor = None
    if len(records) > limit:
        next_cursor = _encode_cursor(records[limit - 1])
        records = records[:limit]

    fields = _field_definitions(reservoir)
    return LevelMonitoringListResponse(
        records=[_record_response(record, fields) for record in records],
        fields=fields,
        next_cursor=next_cursor,
    )


@router.get(
    "/level-monitoring/records/{record_id}",
    response_model=LevelMonitoringRecordResponse,
)
def get_level_monitoring_record(record_id: str):
    """Fetch one level monitoring record."""
    record = _records_collection().find_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return _record_response(record)


@router.patch(
    "/level-monitoring/records/{record_id}",
    response_model=LevelMonitoringRecordResponse,
)
def update_level_monitoring_record(record_id: str, payload: LevelMonitoringRecordUpdate):
    """Partially update a level monitoring record."""
    record_oid = _parse_object_id(record_id, "record")
    existing = _records_collection().find_one({"_id": record_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Record not found")

    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No record values provided")

    reservoir = existing["reservoir"]
    if "custom_fields" in update_data:
        _validate_custom_field_keys(reservoir, update_data["custom_fields"])

    reservoir_level_value = update_data.get(
        "reservoir_level_value",
        existing["reservoir_level_value"],
    )
    reservoir_level_unit = update_data.get(
        "reservoir_level_unit",
        existing["reservoir_level_unit"],
    )
    _validate_reservoir_level_limit(
        reservoir,
        reservoir_level_value,
        reservoir_level_unit,
    )

    daily_inflow = update_data.get("daily_inflow", existing["daily_inflow"])
    unaccounted_inflow = update_data.get(
        "unaccounted_inflow",
        existing["unaccounted_inflow"],
    )
    update_data["total_daily_inflow"] = daily_inflow + unaccounted_inflow
    if "record_date" in update_data:
        update_data["record_date"] = update_data["record_date"].isoformat()
    update_data["updated_at"] = _utcnow()

    try:
        record = _records_collection().find_one_and_update(
            {"_id": record_oid, **_active_filter()},
            {"$set": update_data},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A level monitoring record already exists for this reservoir and date",
        )
    return _record_response(record)


@router.delete("/level-monitoring/records/{record_id}")
def delete_level_monitoring_record(record_id: str):
    """Soft delete a level monitoring record."""
    result = _records_collection().update_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()},
        {"$set": {"deleted_at": _utcnow(), "updated_at": _utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"deleted": True}


@router.get(
    "/level-monitoring/aggregate",
    response_model=LevelMonitoringAggregationResponse,
)
def aggregate_level_monitoring_records(
    reservoir: ReservoirCode = Query(...),
    group_by: ResourceAggregationGroup = Query(...),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    """Aggregate level monitoring data to weeks, months, or years."""
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    query_filter = {"reservoir": reservoir, **_active_filter()}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date.isoformat()
        if end_date:
            date_filter["$lte"] = end_date.isoformat()
        query_filter["record_date"] = date_filter

    records = list(_records_collection().find(query_filter).sort([("record_date", 1)]))
    grouped: dict[tuple[date, date], dict[str, Any]] = {}

    for record in records:
        record_date = _parse_record_date(record["record_date"])
        period_start, period_end = _period_bounds(record_date, group_by)
        group = grouped.setdefault(
            (period_start, period_end),
            {
                "record_count": 0,
                "daily_inflow": 0.0,
                "unaccounted_inflow": 0.0,
                "total_daily_inflow": 0.0,
                "level_ft_total": 0.0,
                "level_ft_count": 0,
                "level_m3_total": 0.0,
                "level_m3_count": 0,
            },
        )
        group["record_count"] += 1
        group["daily_inflow"] += record["daily_inflow"]
        group["unaccounted_inflow"] += record["unaccounted_inflow"]
        group["total_daily_inflow"] += record.get(
            "total_daily_inflow",
            record["daily_inflow"] + record["unaccounted_inflow"],
        )
        if record["reservoir_level_unit"] == "ft":
            level_ft = record["reservoir_level_value"]
            level_m3 = _ft_to_m3(level_ft)
        else:
            level_m3 = record["reservoir_level_value"]
            level_ft = _m3_to_ft(level_m3)
        group["level_ft_total"] += level_ft
        group["level_ft_count"] += 1
        group["level_m3_total"] += level_m3
        group["level_m3_count"] += 1

    response_records = []
    for (period_start, period_end), group in grouped.items():
        response_records.append(
            LevelMonitoringAggregationRecord(
                reservoir=reservoir,
                group_by=group_by,
                period_start_date=period_start,
                period_end_date=period_end,
                record_count=group["record_count"],
                daily_inflow=group["daily_inflow"],
                unaccounted_inflow=group["unaccounted_inflow"],
                total_daily_inflow=group["total_daily_inflow"],
                avg_reservoir_level_ft=(
                    group["level_ft_total"] / group["level_ft_count"]
                    if group["level_ft_count"]
                    else None
                ),
                avg_reservoir_level_m3=(
                    group["level_m3_total"] / group["level_m3_count"]
                    if group["level_m3_count"]
                    else None
                ),
            )
        )

    return LevelMonitoringAggregationResponse(records=response_records)


@router.get(
    "/dam-calculation/configs",
    response_model=list[DamCalculationConfigResponse],
)
def list_dam_calculation_tool_configs(
    include_lookup: bool = Query(False),
):
    """List available dam calculation tool configurations and defaults."""
    return list_dam_calculation_configs(include_lookup=include_lookup)


@router.post(
    "/dam-calculation/calculate",
    response_model=DamCalculationResponse,
)
def calculate_dam_calculation_tool(payload: DamCalculationRequest):
    """Calculate dam volume, useful volume, and projected generation duration."""
    return calculate_dam_projection(
        dam=payload.dam,
        current_level_ft=payload.current_level_ft,
        evaporation_rate=payload.evaporation_rate,
        production_rate_mw=payload.production_rate_mw,
    )
