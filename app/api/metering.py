"""
Metering endpoints.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.database import get_db
from app.schemas.metering import (
    MeterCaptureListResponse,
    MeterCaptureReadingCreate,
    MeterCaptureReadingResponse,
    MeterCaptureReadingUpdate,
    MeterResponse,
    MeterUpdate,
    MeteringSiteCode,
    MeteringSiteInfo,
)

router = APIRouter(prefix="/metering", tags=["metering"])

METERING_SITES: dict[MeteringSiteCode, str] = {
    "mps": "MPS",
    "lps": "LPS",
}

METER_COUNT_PER_SITE = 4
METER_INTERVAL_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _active_filter() -> dict:
    return {"deleted_at": {"$exists": False}}


def _meters_collection():
    return get_db()["metering_meters"]


def _readings_collection():
    return get_db()["metering_interval_readings"]


def _site_name(site: MeteringSiteCode) -> str:
    return METERING_SITES[site]


def _parse_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datetime_key(value: datetime) -> str:
    return _parse_datetime(value).isoformat()


def _validate_interval(value: datetime) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.minute not in (0, 30) or parsed.second != 0 or parsed.microsecond != 0:
        raise HTTPException(
            status_code=400,
            detail="interval_start must be aligned to a 30-minute boundary",
        )
    return parsed


def _encode_cursor(record: dict) -> str:
    cursor_payload = {
        "interval_start": record["interval_start"],
        "_id": str(record["_id"]),
    }
    raw_cursor = json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_cursor).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    try:
        decoded_cursor = base64.urlsafe_b64decode(cursor.encode("ascii"))
        cursor_payload = json.loads(decoded_cursor.decode("utf-8"))
        interval_start = _datetime_key(_parse_datetime(cursor_payload["interval_start"]))
        record_id = _parse_object_id(cursor_payload["_id"], "cursor")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")

    return {"interval_start": interval_start, "_id": record_id}


def _meter_response(record: dict) -> dict:
    return {
        "id": record["meter_id"],
        "site": record["site"],
        "name": record["name"],
        "column_key": record["column_key"],
        "entry_mode": record["entry_mode"],
        "unit": record.get("unit", "MWh"),
        "sort_order": record["sort_order"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _meters_for_site(site: MeteringSiteCode) -> list[dict]:
    records = list(
        _meters_collection()
        .find({"site": site, **_active_filter()})
        .sort([("sort_order", 1), ("meter_id", 1)])
    )
    return [_meter_response(record) for record in records]


def _validate_reading_meter_ids(
    site: MeteringSiteCode,
    readings: dict[str, float],
) -> None:
    valid_meter_ids = {meter["id"] for meter in _meters_for_site(site)}
    unknown_meter_ids = sorted(set(readings) - valid_meter_ids)
    if unknown_meter_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown meter ID(s) for {site}: {', '.join(unknown_meter_ids)}",
        )


def _readings_with_meter_defaults(
    site: MeteringSiteCode,
    readings: Optional[dict[str, float]],
) -> dict[str, Optional[float]]:
    values = dict(readings or {})
    for meter in _meters_for_site(site):
        values.setdefault(meter["id"], None)
    return values


def _capture_response(record: dict) -> dict:
    interval_start = _parse_datetime(record["interval_start"])
    site = record["site"]
    return {
        "id": str(record["_id"]),
        "site": site,
        "site_name": _site_name(site),
        "interval_start": interval_start,
        "interval_end": interval_start + timedelta(minutes=METER_INTERVAL_MINUTES),
        "readings": _readings_with_meter_defaults(site, record.get("readings")),
        "source": record.get("source", "manual"),
        "notes": record.get("notes"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


@router.get("/sites", response_model=list[MeteringSiteInfo])
def list_metering_sites():
    """List sites available for metering."""
    return [
        MeteringSiteInfo(code=site_code, name=site_name)
        for site_code, site_name in METERING_SITES.items()
    ]


@router.get("/meters", response_model=list[MeterResponse])
def list_meters(site: Optional[MeteringSiteCode] = Query(None)):
    """List meter definitions and entry modes."""
    query_filter = _active_filter()
    if site:
        query_filter["site"] = site
    records = list(
        _meters_collection()
        .find(query_filter)
        .sort([("site", 1), ("sort_order", 1), ("meter_id", 1)])
    )
    return [_meter_response(record) for record in records]


@router.patch("/meters/{meter_id}", response_model=MeterResponse)
def update_meter(meter_id: str, payload: MeterUpdate):
    """Set a meter to manual or automatic entry mode."""
    record = _meters_collection().find_one_and_update(
        {"meter_id": meter_id, **_active_filter()},
        {"$set": {"entry_mode": payload.entry_mode, "updated_at": _utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Meter not found")
    return _meter_response(record)


@router.post(
    "/meter-capture/readings",
    response_model=MeterCaptureReadingResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_meter_capture_reading(payload: MeterCaptureReadingCreate):
    """Create a 30-minute meter capture row."""
    interval_start = _validate_interval(payload.interval_start)
    _validate_reading_meter_ids(payload.site, payload.readings)

    now = _utcnow()
    document = payload.model_dump()
    document["interval_start"] = _datetime_key(interval_start)
    document["created_at"] = now
    document["updated_at"] = now

    try:
        result = _readings_collection().insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A meter capture row already exists for this site and interval",
        )

    record = _readings_collection().find_one({"_id": result.inserted_id})
    return _capture_response(record)


@router.get("/meter-capture/readings", response_model=MeterCaptureListResponse)
def list_meter_capture_readings(
    site: MeteringSiteCode = Query(...),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
):
    """List 30-minute meter capture rows for the table."""
    if start_time and end_time and _parse_datetime(end_time) < _parse_datetime(start_time):
        raise HTTPException(status_code=400, detail="end_time must be on or after start_time")

    query_filter = {"site": site, **_active_filter()}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = _datetime_key(start_time)
        if end_time:
            time_filter["$lte"] = _datetime_key(end_time)
        query_filter["interval_start"] = time_filter
    if cursor:
        decoded_cursor = _decode_cursor(cursor)
        query_filter["$or"] = [
            {"interval_start": {"$lt": decoded_cursor["interval_start"]}},
            {
                "interval_start": decoded_cursor["interval_start"],
                "_id": {"$lt": decoded_cursor["_id"]},
            },
        ]

    skip = ((page - 1) * limit) if page and not cursor else 0
    records = list(
        _readings_collection()
        .find(query_filter)
        .sort([("interval_start", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit + 1)
    )

    next_cursor = None
    if len(records) > limit:
        next_cursor = _encode_cursor(records[limit - 1])
        records = records[:limit]

    return MeterCaptureListResponse(
        site=site,
        site_name=_site_name(site),
        meters=_meters_for_site(site),
        records=[_capture_response(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get(
    "/meter-capture/readings/{record_id}",
    response_model=MeterCaptureReadingResponse,
)
def get_meter_capture_reading(record_id: str):
    """Fetch one meter capture row."""
    record = _readings_collection().find_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return _capture_response(record)


@router.patch(
    "/meter-capture/readings/{record_id}",
    response_model=MeterCaptureReadingResponse,
)
def update_meter_capture_reading(
    record_id: str,
    payload: MeterCaptureReadingUpdate,
):
    """Partially update a meter capture row."""
    record_oid = _parse_object_id(record_id, "record")
    existing = _readings_collection().find_one({"_id": record_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Record not found")

    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No record values provided")

    site = existing["site"]
    if "readings" in update_data:
        _validate_reading_meter_ids(site, update_data["readings"])
    if "interval_start" in update_data:
        update_data["interval_start"] = _datetime_key(
            _validate_interval(update_data["interval_start"])
        )
    update_data["updated_at"] = _utcnow()

    try:
        record = _readings_collection().find_one_and_update(
            {"_id": record_oid, **_active_filter()},
            {"$set": update_data},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A meter capture row already exists for this site and interval",
        )
    return _capture_response(record)


@router.delete("/meter-capture/readings/{record_id}")
def delete_meter_capture_reading(record_id: str):
    """Soft delete a meter capture row."""
    result = _readings_collection().update_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()},
        {"$set": {"deleted_at": _utcnow(), "updated_at": _utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"deleted": True}
