"""
Power outage request endpoints.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument

from app.db.database import get_db
from app.schemas.outage_requests import (
    GeneratingUnitResponse,
    OutageReason,
    OutageRequestCreate,
    OutageRequestListResponse,
    OutageRequestReplace,
    OutageRequestResponse,
)

router = APIRouter(prefix="/outage-requests", tags=["contracts"])

GENERATING_UNITS: dict[str, str] = {
    "MPS UNIT 1": "MPS Unit 1",
    "MPS UNIT 2": "MPS Unit 2",
    "MPS UNIT 3": "MPS Unit 3",
    "MPS UNIT 4": "MPS Unit 4",
    "LPS UNIT 1": "LPS Unit 1",
    "LPS UNIT 2": "LPS Unit 2",
    "LPS UNIT 3": "LPS Unit 3",
    "LPS UNIT 4": "LPS Unit 4",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _outage_collection():
    return get_db()["outage_requests"]


def _active_filter() -> dict:
    return {"deleted_at": {"$exists": False}, "unit_code": {"$exists": True}}


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datetime_key(value: datetime) -> str:
    return _parse_datetime(value).isoformat()


def _duration_hrs(start_at: datetime, restore_at: datetime) -> float:
    return round((restore_at - start_at).total_seconds() / 3600, 4)


def _unit_name(unit_code: str) -> str:
    normalized_code = unit_code.strip().upper()
    if normalized_code not in GENERATING_UNITS:
        raise HTTPException(status_code=400, detail=f"Unknown generating unit: {unit_code}")
    return GENERATING_UNITS[normalized_code]


def _outage_document(payload: OutageRequestCreate | OutageRequestReplace) -> dict:
    unit_code = payload.unit_code.strip().upper()
    start_at = _parse_datetime(payload.start_at)
    restore_at = _parse_datetime(payload.restore_at)
    if restore_at <= start_at:
        raise HTTPException(status_code=400, detail="restore_at must be after start_at")

    return {
        "unit_code": unit_code,
        "unit_name": _unit_name(unit_code),
        "reason": payload.reason,
        "start_at": _datetime_key(start_at),
        "restore_at": _datetime_key(restore_at),
        "duration_hrs": _duration_hrs(start_at, restore_at),
        "expected_mw_reduction": payload.expected_mw_reduction,
        "description": payload.description,
    }


def _serialize_request(record: dict) -> dict:
    return {
        "id": str(record["_id"]),
        "unit_code": record["unit_code"],
        "unit_name": record["unit_name"],
        "reason": record["reason"],
        "start_at": _parse_datetime(record["start_at"]),
        "restore_at": _parse_datetime(record["restore_at"]),
        "duration_hrs": record["duration_hrs"],
        "expected_mw_reduction": record.get("expected_mw_reduction"),
        "description": record.get("description"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _overlap_query(
    document: dict,
    exclude_id: Optional[ObjectId] = None,
) -> dict:
    query_filter = {
        "unit_code": document["unit_code"],
        "start_at": {"$lt": document["restore_at"]},
        "restore_at": {"$gt": document["start_at"]},
        **_active_filter(),
    }
    if exclude_id is not None:
        query_filter["_id"] = {"$ne": exclude_id}
    return query_filter


def _validate_no_overlaps(
    document: dict,
    exclude_id: Optional[ObjectId] = None,
) -> None:
    overlapping = _outage_collection().find_one(_overlap_query(document, exclude_id))
    if overlapping:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Outage overlap for {document['unit_code']} between "
                f"{document['start_at']} and {document['restore_at']}"
            ),
        )


def _list_query_filter(
    unit: Optional[str],
    reason: Optional[OutageReason],
    from_datetime: Optional[datetime],
    to_datetime: Optional[datetime],
) -> dict:
    query_filter = _active_filter()
    if unit:
        query_filter["unit_code"] = unit.strip().upper()
    if reason:
        query_filter["reason"] = reason
    if from_datetime:
        query_filter["restore_at"] = {"$gte": _datetime_key(from_datetime)}
    if to_datetime:
        query_filter.setdefault("start_at", {})
        query_filter["start_at"]["$lte"] = _datetime_key(to_datetime)
    return query_filter


@router.get("/generating-units", response_model=list[GeneratingUnitResponse])
def list_generating_units():
    """List generating units available for outage records."""
    return [
        GeneratingUnitResponse(unit_code=unit_code, unit_name=unit_name)
        for unit_code, unit_name in GENERATING_UNITS.items()
    ]


@router.get("", response_model=OutageRequestListResponse)
def list_outage_requests(
    unit: Optional[str] = Query(None),
    reason: Optional[OutageReason] = Query(None),
    from_datetime: Optional[datetime] = Query(None, alias="from"),
    to_datetime: Optional[datetime] = Query(None, alias="to"),
):
    """List outage records with optional unit, reason, and time-window filters."""
    if from_datetime and to_datetime and _parse_datetime(to_datetime) < _parse_datetime(from_datetime):
        raise HTTPException(status_code=400, detail="to must be on or after from")

    records = list(
        _outage_collection()
        .find(_list_query_filter(unit, reason, from_datetime, to_datetime))
        .sort([("start_at", -1), ("_id", -1)])
    )
    return OutageRequestListResponse(
        records=[_serialize_request(record) for record in records]
    )


@router.get("/{request_id}", response_model=OutageRequestResponse)
def get_outage_request(request_id: str):
    """Fetch one outage record."""
    record = _outage_collection().find_one(
        {"_id": _parse_object_id(request_id, "outage request"), **_active_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Outage request not found")
    return _serialize_request(record)


@router.post(
    "",
    response_model=OutageRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_outage_request(payload: OutageRequestCreate):
    """Create one outage record."""
    collection = _outage_collection()
    document = _outage_document(payload)
    _validate_no_overlaps(document)

    now = _utcnow()
    document["created_at"] = now
    document["updated_at"] = now

    result = collection.insert_one(document)
    record = collection.find_one({"_id": result.inserted_id})
    return _serialize_request(record)


@router.put("/{request_id}", response_model=OutageRequestResponse)
def replace_outage_request(request_id: str, payload: OutageRequestReplace):
    """Replace one outage record."""
    collection = _outage_collection()
    request_oid = _parse_object_id(request_id, "outage request")
    existing = collection.find_one({"_id": request_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Outage request not found")

    document = _outage_document(payload)
    _validate_no_overlaps(document, exclude_id=request_oid)
    document["created_at"] = existing["created_at"]
    document["updated_at"] = _utcnow()

    record = collection.find_one_and_replace(
        {"_id": request_oid, **_active_filter()},
        {**document, "_id": request_oid},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_request(record)


@router.delete("/{request_id}")
def delete_outage_request(request_id: str):
    """Soft delete one outage record."""
    result = _outage_collection().update_one(
        {"_id": _parse_object_id(request_id, "outage request"), **_active_filter()},
        {"$set": {"deleted_at": _utcnow(), "updated_at": _utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Outage request not found")
    return {"deleted": True}
