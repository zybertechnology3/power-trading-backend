"""
Customer contract endpoints.
"""

import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pymongo import ReturnDocument

from app.db.database import get_db
from app.schemas.contracts import (
    DEFAULT_INDEXATION_FORMULA,
    ContractCreate,
    ContractFileMetadata,
    ContractListResponse,
    ContractResponse,
    ContractType,
    ContractUpdate,
    Firmness,
)

router = APIRouter(prefix="/contracts", tags=["contracts"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _encode_cursor(record: dict) -> str:
    cursor_payload = {
        "created_at": record["created_at"].isoformat(),
        "_id": str(record["_id"]),
    }
    raw_cursor = json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_cursor).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    try:
        decoded_cursor = base64.urlsafe_b64decode(cursor.encode("ascii"))
        cursor_payload = json.loads(decoded_cursor.decode("utf-8"))
        created_at = datetime.fromisoformat(cursor_payload["created_at"])
        record_id = _parse_object_id(cursor_payload["_id"], "cursor")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid cursor")

    return {"created_at": created_at, "_id": record_id}


def _serialize_contract(record: dict) -> dict:
    effective_date = record["effective_date"]
    expiration_date = record.get("expiration_date") or effective_date
    return {
        "id": str(record["_id"]),
        "customer": record["customer"],
        "contract_type": record["contract_type"],
        "effective_date": effective_date,
        "expiration_date": expiration_date,
        "duration": record.get("duration") or _derive_duration_text(
            _parse_contract_date(effective_date),
            _parse_contract_date(expiration_date),
        ),
        "firmness": record["firmness"],
        "capacity_mw": record["capacity_mw"],
        "tariff_energy_usd_per_mwh": record["tariff_energy_usd_per_mwh"],
        "tariff_demand_usd_per_mw_month": record.get(
            "tariff_demand_usd_per_mw_month"
        ),
        "tariff_overall_usd_per_mwh": None,
        "indexation_formula": record["indexation_formula"],
        "ppi_series": record["ppi_series"],
        "expiration_reminder": record.get("expiration_reminder"),
        "custom_fields": record.get("custom_fields", []),
        "files": record.get("files", []),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _contract_collection():
    db = get_db()
    return db["contracts"]


def _active_contract_filter() -> dict:
    return {"deleted_at": {"$exists": False}}


def _parse_contract_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, month_lengths[month - 1])
    return date(year, month, day)


def _derive_duration_text(effective_date: date, expiration_date: date) -> str:
    return f"{effective_date.isoformat()} to {expiration_date.isoformat()}"


def _expiration_from_duration(effective_date: date, duration: str) -> Optional[date]:
    match = re.fullmatch(
        r"\s*(\d+)\s*(day|days|week|weeks|month|months|year|years)\s*",
        duration,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("day"):
        return effective_date + timedelta(days=amount) - timedelta(days=1)
    if unit.startswith("week"):
        return effective_date + timedelta(weeks=amount) - timedelta(days=1)
    if unit.startswith("month"):
        return _add_months(effective_date, amount) - timedelta(days=1)
    return _add_months(effective_date, amount * 12) - timedelta(days=1)


def _contract_payload_document(payload: ContractCreate) -> dict:
    document = payload.model_dump()
    effective_date = payload.effective_date
    expiration_date = payload.expiration_date
    document["effective_date"] = effective_date.isoformat()
    document["expiration_date"] = expiration_date.isoformat()
    if not document.get("duration"):
        document["duration"] = _derive_duration_text(effective_date, expiration_date)
    return document


def _upload_contract_file_to_storage(
    contract_id: str,
    file_id: str,
    filename: str,
    content: bytes,
    content_type: Optional[str],
) -> str:
    """
    Placeholder for Supabase object storage upload.

    Replace this with the Supabase client upload call and return the public or
    signed URL for the stored document.
    """
    safe_filename = filename.replace("/", "_").replace("\\", "_")
    return f"supabase://placeholder/contracts/{contract_id}/{file_id}/{safe_filename}"


def _delete_contract_file_from_storage(file_url: str) -> None:
    """
    Placeholder for Supabase object storage deletion.

    Replace this with the Supabase client delete call once storage is configured.
    """
    return None


@router.post(
    "",
    response_model=ContractResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_contract(payload: ContractCreate):
    """Create a customer contract."""
    collection = _contract_collection()
    now = _utcnow()
    document = {
        **_contract_payload_document(payload),
        "indexation_formula": DEFAULT_INDEXATION_FORMULA,
        "files": [],
        "created_at": now,
        "updated_at": now,
    }
    result = collection.insert_one(document)
    record = collection.find_one({"_id": result.inserted_id})
    return _serialize_contract(record)


@router.get("", response_model=ContractListResponse)
def list_contracts(
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    contract_type: Optional[ContractType] = Query(None),
    firmness: Optional[Firmness] = Query(None),
    customer: Optional[str] = Query(None),
):
    """List active contracts, newest first."""
    collection = _contract_collection()
    query_filter = _active_contract_filter()

    if contract_type:
        query_filter["contract_type"] = contract_type
    if firmness:
        query_filter["firmness"] = firmness
    if customer:
        query_filter["customer"] = {"$regex": customer, "$options": "i"}
    if cursor:
        decoded_cursor = _decode_cursor(cursor)
        query_filter["$or"] = [
            {"created_at": {"$lt": decoded_cursor["created_at"]}},
            {
                "created_at": decoded_cursor["created_at"],
                "_id": {"$lt": decoded_cursor["_id"]},
            },
        ]

    skip = ((page - 1) * limit) if page and not cursor else 0
    records = list(
        collection.find(query_filter)
        .sort([("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit + 1)
    )

    next_cursor = None
    if len(records) > limit:
        next_cursor = _encode_cursor(records[limit - 1])
        records = records[:limit]

    return ContractListResponse(
        records=[_serialize_contract(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get("/{contract_id}", response_model=ContractResponse)
def get_contract(contract_id: str):
    """Fetch one active contract."""
    collection = _contract_collection()
    record = collection.find_one(
        {"_id": _parse_object_id(contract_id, "contract"), **_active_contract_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Contract not found")

    return _serialize_contract(record)


@router.patch("/{contract_id}", response_model=ContractResponse)
def update_contract(contract_id: str, payload: ContractUpdate):
    """Partially update an active contract."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No contract fields provided")

    collection = _contract_collection()
    contract_oid = _parse_object_id(contract_id, "contract")
    existing = collection.find_one({"_id": contract_oid, **_active_contract_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Contract not found")

    effective_date = update_data.get(
        "effective_date",
        _parse_contract_date(existing["effective_date"]),
    )
    expiration_date = update_data.get(
        "expiration_date",
        _parse_contract_date(existing.get("expiration_date") or existing["effective_date"]),
    )

    if "duration" in update_data and "expiration_date" not in update_data:
        parsed_expiration_date = _expiration_from_duration(
            effective_date,
            update_data["duration"],
        )
        if parsed_expiration_date is not None:
            expiration_date = parsed_expiration_date
            update_data["expiration_date"] = parsed_expiration_date

    if expiration_date < effective_date:
        raise HTTPException(
            status_code=400,
            detail="expiration_date must be on or after effective_date",
        )

    if ("effective_date" in update_data or "expiration_date" in update_data) and "duration" not in update_data:
        update_data["duration"] = _derive_duration_text(effective_date, expiration_date)

    if "effective_date" in update_data:
        update_data["effective_date"] = update_data["effective_date"].isoformat()
    if "expiration_date" in update_data:
        update_data["expiration_date"] = update_data["expiration_date"].isoformat()
    update_data["updated_at"] = _utcnow()

    record = collection.find_one_and_update(
        {"_id": contract_oid, **_active_contract_filter()},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_contract(record)


@router.delete("/{contract_id}")
def delete_contract(contract_id: str):
    """Soft delete a contract."""
    now = _utcnow()
    collection = _contract_collection()
    result = collection.update_one(
        {"_id": _parse_object_id(contract_id, "contract"), **_active_contract_filter()},
        {"$set": {"deleted_at": now, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contract not found")

    return {"deleted": True}


@router.post("/{contract_id}/files", response_model=ContractResponse)
async def upload_contract_files(
    contract_id: str,
    files: list[UploadFile] = File(...),
):
    """Append supporting document metadata to a contract."""
    collection = _contract_collection()
    contract_oid = _parse_object_id(contract_id, "contract")
    existing = collection.find_one({"_id": contract_oid, **_active_contract_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Contract not found")

    uploaded_files = []
    for uploaded_file in files:
        if not uploaded_file.filename:
            raise HTTPException(status_code=400, detail="Uploaded file must have a name")

        content = await uploaded_file.read()
        file_id = str(uuid4())
        file_url = _upload_contract_file_to_storage(
            contract_id=contract_id,
            file_id=file_id,
            filename=uploaded_file.filename,
            content=content,
            content_type=uploaded_file.content_type,
        )
        uploaded_files.append(
            ContractFileMetadata(
                id=file_id,
                name=uploaded_file.filename,
                size=len(content),
                url=file_url,
                content_type=uploaded_file.content_type,
            ).model_dump()
        )

    now = _utcnow()
    record = collection.find_one_and_update(
        {"_id": contract_oid, **_active_contract_filter()},
        {
            "$push": {"files": {"$each": uploaded_files}},
            "$set": {"updated_at": now},
        },
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_contract(record)


@router.delete("/{contract_id}/files/{file_id}", response_model=ContractResponse)
def delete_contract_file(contract_id: str, file_id: str):
    """Remove supporting document metadata from a contract."""
    collection = _contract_collection()
    contract_oid = _parse_object_id(contract_id, "contract")
    existing = collection.find_one({"_id": contract_oid, **_active_contract_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Contract not found")

    existing_files = existing.get("files", [])
    file_to_delete = next(
        (file_record for file_record in existing_files if file_record["id"] == file_id),
        None,
    )
    if not file_to_delete:
        raise HTTPException(status_code=404, detail="File not found")

    _delete_contract_file_from_storage(file_to_delete["url"])
    remaining_files = [
        file_record for file_record in existing_files if file_record["id"] != file_id
    ]

    record = collection.find_one_and_update(
        {"_id": contract_oid, **_active_contract_filter()},
        {"$set": {"files": remaining_files, "updated_at": _utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_contract(record)
