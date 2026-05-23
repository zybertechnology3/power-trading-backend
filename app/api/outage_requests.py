"""
Power outage request endpoints.
"""

from datetime import date, datetime, timezone
from typing import Any, Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.database import get_db
from app.schemas.outage_requests import (
    GeneratingUnitResponse,
    OutageItemInput,
    OutageRequestCreate,
    OutageRequestListResponse,
    OutageRequestReplace,
    OutageRequestResponse,
    OutageRequestStatus,
    OutageStatusUpdate,
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

STATUS_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"submitted"},
    "submitted": {"approved", "rejected"},
    "approved": {"completed"},
    "rejected": set(),
    "completed": set(),
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
    return {"deleted_at": {"$exists": False}}


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


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _duration_hrs(start_at: datetime, restore_at: datetime) -> float:
    return round((restore_at - start_at).total_seconds() / 3600, 4)


def _unit_name(unit_code: str) -> str:
    normalized_code = unit_code.strip().upper()
    if normalized_code not in GENERATING_UNITS:
        raise HTTPException(status_code=400, detail=f"Unknown generating unit: {unit_code}")
    return GENERATING_UNITS[normalized_code]


def _item_document(item: OutageItemInput, outage_no: int) -> dict:
    unit_code = item.unit_code.strip().upper()
    start_at = _parse_datetime(item.start_at)
    restore_at = _parse_datetime(item.restore_at)
    if restore_at <= start_at:
        raise HTTPException(status_code=400, detail="restore_at must be after start_at")

    return {
        "outage_no": outage_no,
        "unit_code": unit_code,
        "unit_name": _unit_name(unit_code),
        "reason": item.reason,
        "start_at": _datetime_key(start_at),
        "restore_at": _datetime_key(restore_at),
        "duration_hrs": _duration_hrs(start_at, restore_at),
        "expected_mw_reduction": item.expected_mw_reduction,
        "description": item.description,
    }


def _items_document(items: list[OutageItemInput]) -> list[dict]:
    return [_item_document(item, index + 1) for index, item in enumerate(items)]


def _summary(items: list[dict]) -> dict:
    return {
        "total_duration_hrs": round(sum(float(item["duration_hrs"]) for item in items), 4),
        "total_expected_mw_reduction": round(
            sum(float(item.get("expected_mw_reduction") or 0) for item in items),
            4,
        ),
    }


def _serialize_item(item: dict) -> dict:
    return {
        **item,
        "start_at": _parse_datetime(item["start_at"]),
        "restore_at": _parse_datetime(item["restore_at"]),
    }


def _serialize_request(record: dict) -> dict:
    items = [_serialize_item(item) for item in record.get("items", [])]
    return {
        "id": str(record["_id"]),
        "document_no": record["document_no"],
        "revision_no": record["revision_no"],
        "implementation_date": _parse_date(record["implementation_date"]),
        "document_owner": record["document_owner"],
        "approver": record["approver"],
        "date_approved": (
            _parse_date(record["date_approved"]) if record.get("date_approved") else None
        ),
        "status": record["status"],
        "created_by": record["created_by"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "items": items,
        "summary": _summary(items),
    }


def _request_document(payload: OutageRequestCreate | OutageRequestReplace) -> dict:
    items = _items_document(payload.items)
    document = payload.model_dump(exclude={"items"})
    document["document_no"] = payload.document_no.strip()
    document["revision_no"] = payload.revision_no.strip()
    document["implementation_date"] = payload.implementation_date.isoformat()
    document["document_owner"] = payload.document_owner.strip()
    document["approver"] = payload.approver.strip()
    document["date_approved"] = (
        payload.date_approved.isoformat() if payload.date_approved else None
    )
    document["created_by"] = payload.created_by.strip()
    document["items"] = items
    if document["status"] == "approved" and not document["date_approved"]:
        document["date_approved"] = _utcnow().date().isoformat()
    return document


def _approved_overlap_query(
    item: dict,
    exclude_id: Optional[ObjectId] = None,
) -> dict:
    query_filter = {
        "status": "approved",
        "items": {
            "$elemMatch": {
                "unit_code": item["unit_code"],
                "start_at": {"$lt": item["restore_at"]},
                "restore_at": {"$gt": item["start_at"]},
            }
        },
        **_active_filter(),
    }
    if exclude_id is not None:
        query_filter["_id"] = {"$ne": exclude_id}
    return query_filter


def _validate_no_approved_overlaps(
    items: list[dict],
    exclude_id: Optional[ObjectId] = None,
) -> None:
    collection = _outage_collection()
    for item in items:
        overlapping = collection.find_one(_approved_overlap_query(item, exclude_id))
        if overlapping:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Approved outage overlap for {item['unit_code']} between "
                    f"{item['start_at']} and {item['restore_at']}"
                ),
            )


def _list_query_filter(
    status_filter: Optional[OutageRequestStatus],
    unit: Optional[str],
    from_datetime: Optional[datetime],
    to_datetime: Optional[datetime],
) -> dict:
    query_filter = _active_filter()
    if status_filter:
        query_filter["status"] = status_filter

    item_filter = {}
    if unit:
        item_filter["unit_code"] = unit.strip().upper()
    if from_datetime:
        item_filter["restore_at"] = {"$gte": _datetime_key(from_datetime)}
    if to_datetime:
        item_filter.setdefault("start_at", {})
        item_filter["start_at"]["$lte"] = _datetime_key(to_datetime)
    if item_filter:
        query_filter["items"] = {"$elemMatch": item_filter}
    return query_filter


@router.get("/generating-units", response_model=list[GeneratingUnitResponse])
def list_generating_units():
    """List generating units available for outage request items."""
    return [
        GeneratingUnitResponse(unit_code=unit_code, unit_name=unit_name)
        for unit_code, unit_name in GENERATING_UNITS.items()
    ]


@router.get("", response_model=OutageRequestListResponse)
def list_outage_requests(
    status: Optional[OutageRequestStatus] = Query(None),
    unit: Optional[str] = Query(None),
    from_datetime: Optional[datetime] = Query(None, alias="from"),
    to_datetime: Optional[datetime] = Query(None, alias="to"),
):
    """List outage requests with optional status, unit, and time-window filters."""
    if from_datetime and to_datetime and _parse_datetime(to_datetime) < _parse_datetime(from_datetime):
        raise HTTPException(status_code=400, detail="to must be on or after from")

    records = list(
        _outage_collection()
        .find(_list_query_filter(status, unit, from_datetime, to_datetime))
        .sort([("implementation_date", -1), ("created_at", -1), ("_id", -1)])
    )
    return OutageRequestListResponse(
        records=[_serialize_request(record) for record in records]
    )


@router.get("/{request_id}", response_model=OutageRequestResponse)
def get_outage_request(request_id: str):
    """Fetch one outage request."""
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
    """Create an outage request with nested outage items."""
    collection = _outage_collection()
    document = _request_document(payload)
    if document["status"] == "approved":
        _validate_no_approved_overlaps(document["items"])

    now = _utcnow()
    document["created_at"] = now
    document["updated_at"] = now

    try:
        result = collection.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="document_no must be unique")

    record = collection.find_one({"_id": result.inserted_id})
    return _serialize_request(record)


@router.put("/{request_id}", response_model=OutageRequestResponse)
def replace_outage_request(request_id: str, payload: OutageRequestReplace):
    """Replace an outage request header and items."""
    collection = _outage_collection()
    request_oid = _parse_object_id(request_id, "outage request")
    existing = collection.find_one({"_id": request_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Outage request not found")

    document = _request_document(payload)
    if document["status"] == "approved":
        _validate_no_approved_overlaps(document["items"], exclude_id=request_oid)

    document["created_at"] = existing["created_at"]
    document["updated_at"] = _utcnow()

    try:
        record = collection.find_one_and_replace(
            {"_id": request_oid, **_active_filter()},
            {**document, "_id": request_oid},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="document_no must be unique")

    return _serialize_request(record)


@router.patch("/{request_id}/status", response_model=OutageRequestResponse)
def update_outage_request_status(request_id: str, payload: OutageStatusUpdate):
    """Apply a workflow status transition."""
    collection = _outage_collection()
    request_oid = _parse_object_id(request_id, "outage request")
    existing = collection.find_one({"_id": request_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Outage request not found")

    current_status = existing["status"]
    new_status = payload.status
    if new_status == current_status:
        return _serialize_request(existing)
    if new_status not in STATUS_TRANSITIONS[current_status]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status transition: {current_status} to {new_status}",
        )
    if new_status == "approved":
        _validate_no_approved_overlaps(existing.get("items", []), exclude_id=request_oid)

    update_data = {"status": new_status, "updated_at": _utcnow()}
    if new_status == "approved":
        update_data["date_approved"] = _utcnow().date().isoformat()

    record = collection.find_one_and_update(
        {"_id": request_oid, **_active_filter()},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_request(record)


@router.delete("/{request_id}")
def delete_outage_request(request_id: str):
    """Soft delete an outage request. Only draft requests can be deleted."""
    collection = _outage_collection()
    request_oid = _parse_object_id(request_id, "outage request")
    existing = collection.find_one({"_id": request_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Outage request not found")
    if existing["status"] != "draft":
        raise HTTPException(status_code=400, detail="Only draft outage requests can be deleted")

    collection.update_one(
        {"_id": request_oid, **_active_filter()},
        {"$set": {"deleted_at": _utcnow(), "updated_at": _utcnow()}},
    )
    return {"deleted": True}
