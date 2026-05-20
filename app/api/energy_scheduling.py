"""
Energy scheduling endpoints.
"""

import base64
import json
from datetime import datetime, timezone
from typing import Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo import ReturnDocument

from app.core.dam_calculations import (
    calculate_equivalent_water_volume,
    calculate_equivalent_water_volume_from_energy_gwh,
)
from app.core.energy_scheduling import (
    calculate_yearly_power_sources_budget,
    default_yearly_budget_payload,
)
from app.db.database import get_db
from app.schemas.energy_scheduling import (
    BudgetableYearsResponse,
    EquivalentWaterVolumeRequest,
    EquivalentWaterVolumeResponse,
    YearlyBudgetCalculateRequest,
    YearlyBudgetCalculationResponse,
    YearlyBudgetCreate,
    YearlyBudgetListResponse,
    YearlyBudgetResponse,
    YearlyBudgetUpdate,
)

router = APIRouter(prefix="/energy-scheduling", tags=["energy-scheduling"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_object_id(record_id: str, record_name: str) -> ObjectId:
    try:
        return ObjectId(record_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {record_name} ID format")


def _collection():
    return get_db()["energy_yearly_budgets"]


def _active_filter() -> dict:
    return {"deleted_at": {"$exists": False}}


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


def _calculation_payload_from_request(payload: YearlyBudgetCalculateRequest) -> dict:
    data = {
        "year": payload.year,
        "name": payload.name,
        "target_year": payload.target_year or payload.year,
        "comparison_year": payload.comparison_year or payload.year - 1,
        **payload.inputs.model_dump(),
    }
    return data


def _calculation_payload_from_record(record: dict) -> dict:
    return {
        "year": record["year"],
        "name": record.get("name"),
        "target_year": record.get("target_year") or record["year"],
        "comparison_year": record.get("comparison_year") or record["year"] - 1,
        **record["inputs"],
    }


def _serialize_budget(record: dict) -> dict:
    calculated = calculate_yearly_power_sources_budget(_calculation_payload_from_record(record))
    return {
        "id": str(record["_id"]),
        **calculated,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _current_year() -> int:
    return _utcnow().year


def _budgetable_years(base_year: Optional[int] = None) -> list[int]:
    first_year = base_year or _current_year()
    return [first_year, first_year + 1]


def _latest_budget_for_year(year: int) -> Optional[dict]:
    return _collection().find_one(
        {"year": year, **_active_filter()},
        sort=[("updated_at", -1), ("created_at", -1), ("_id", -1)],
    )


def _default_budgetable_year(year: int) -> dict:
    calculated = calculate_yearly_power_sources_budget(default_yearly_budget_payload(year))
    return {
        "id": None,
        "is_saved": False,
        **calculated,
        "created_at": None,
        "updated_at": None,
    }


def _serialize_budgetable_year(year: int) -> dict:
    record = _latest_budget_for_year(year)
    if not record:
        return _default_budgetable_year(year)
    return {
        **_serialize_budget(record),
        "is_saved": True,
    }


def _ensure_budgetable_year(year: int, base_year: Optional[int] = None) -> None:
    allowed_years = _budgetable_years(base_year)
    if year not in allowed_years:
        raise HTTPException(
            status_code=400,
            detail=f"year must be one of {allowed_years[0]} or {allowed_years[1]}",
        )


@router.get(
    "/yearly-budget/defaults",
    response_model=YearlyBudgetCalculationResponse,
)
def get_yearly_budget_defaults(
    year: int = Query(2026, ge=2000, le=2100),
):
    """Return workbook-derived default yearly Power Sources budget."""
    return calculate_yearly_power_sources_budget(default_yearly_budget_payload(year))


@router.post(
    "/yearly-budget/calculate",
    response_model=YearlyBudgetCalculationResponse,
)
def calculate_yearly_budget(payload: YearlyBudgetCalculateRequest):
    """Calculate the yearly Power Sources budget without saving."""
    return calculate_yearly_power_sources_budget(_calculation_payload_from_request(payload))


@router.get(
    "/yearly-budgets/budgetable",
    response_model=BudgetableYearsResponse,
)
def get_budgetable_yearly_budgets(
    base_year: Optional[int] = Query(None, ge=2000, le=2100),
):
    """Return current and next year budgets for the yearly budget page."""
    years = _budgetable_years(base_year)
    return BudgetableYearsResponse(
        base_year=years[0],
        years=years,
        records=[_serialize_budgetable_year(year) for year in years],
    )


@router.put(
    "/yearly-budgets/budgetable/{year}",
    response_model=YearlyBudgetResponse,
)
def save_budgetable_yearly_budget(
    year: int,
    payload: YearlyBudgetCreate,
    base_year: Optional[int] = Query(None, ge=2000, le=2100),
):
    """Upsert a budget for the current or next year."""
    _ensure_budgetable_year(year, base_year)
    if payload.year != year:
        raise HTTPException(status_code=400, detail="Path year must match payload year")

    now = _utcnow()
    existing = _latest_budget_for_year(year)
    update_data = {
        "name": payload.name,
        "year": payload.year,
        "target_year": payload.target_year or payload.year,
        "comparison_year": payload.comparison_year or payload.year - 1,
        "inputs": payload.inputs.model_dump(),
        "updated_at": now,
    }

    if existing:
        record = _collection().find_one_and_update(
            {"_id": existing["_id"], **_active_filter()},
            {"$set": update_data},
            return_document=ReturnDocument.AFTER,
        )
    else:
        result = _collection().insert_one({**update_data, "created_at": now})
        record = _collection().find_one({"_id": result.inserted_id})

    return _serialize_budget(record)


@router.post(
    "/yearly-budgets",
    response_model=YearlyBudgetResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_yearly_budget(payload: YearlyBudgetCreate):
    """Create and save a yearly Power Sources budget."""
    now = _utcnow()
    document = {
        "name": payload.name,
        "year": payload.year,
        "target_year": payload.target_year or payload.year,
        "comparison_year": payload.comparison_year or payload.year - 1,
        "inputs": payload.inputs.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    result = _collection().insert_one(document)
    record = _collection().find_one({"_id": result.inserted_id})
    return _serialize_budget(record)


@router.get("/yearly-budgets", response_model=YearlyBudgetListResponse)
def list_yearly_budgets(
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    year: Optional[int] = Query(None, ge=2000, le=2100),
):
    """List saved yearly Power Sources budgets."""
    query_filter = _active_filter()
    if year is not None:
        query_filter["year"] = year
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
        _collection()
        .find(query_filter)
        .sort([("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit + 1)
    )

    next_cursor = None
    if len(records) > limit:
        next_cursor = _encode_cursor(records[limit - 1])
        records = records[:limit]

    return YearlyBudgetListResponse(
        records=[_serialize_budget(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get("/yearly-budgets/{budget_id}", response_model=YearlyBudgetResponse)
def get_yearly_budget(budget_id: str):
    """Fetch one saved yearly Power Sources budget."""
    record = _collection().find_one(
        {"_id": _parse_object_id(budget_id, "budget"), **_active_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Yearly budget not found")
    return _serialize_budget(record)


@router.patch("/yearly-budgets/{budget_id}", response_model=YearlyBudgetResponse)
def update_yearly_budget(budget_id: str, payload: YearlyBudgetUpdate):
    """Partially update a saved yearly Power Sources budget."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No budget fields provided")

    budget_oid = _parse_object_id(budget_id, "budget")
    existing = _collection().find_one({"_id": budget_oid, **_active_filter()})
    if not existing:
        raise HTTPException(status_code=404, detail="Yearly budget not found")

    if "inputs" in update_data and update_data["inputs"] is not None:
        update_data["inputs"] = update_data["inputs"]
    if "year" in update_data:
        update_data.setdefault("target_year", update_data["year"])
        update_data.setdefault("comparison_year", update_data["year"] - 1)
    update_data["updated_at"] = _utcnow()

    record = _collection().find_one_and_update(
        {"_id": budget_oid, **_active_filter()},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_budget(record)


@router.delete("/yearly-budgets/{budget_id}")
def delete_yearly_budget(budget_id: str):
    """Soft delete a saved yearly Power Sources budget."""
    now = _utcnow()
    result = _collection().update_one(
        {"_id": _parse_object_id(budget_id, "budget"), **_active_filter()},
        {"$set": {"deleted_at": now, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Yearly budget not found")
    return {"deleted": True}


@router.post(
    "/equivalent-water-volume",
    response_model=EquivalentWaterVolumeResponse,
)
def calculate_generation_water_volume(payload: EquivalentWaterVolumeRequest):
    """Backpass generation to equivalent water volume for dam projections."""
    if payload.energy_gwh is not None:
        return calculate_equivalent_water_volume_from_energy_gwh(
            payload.dam,
            payload.energy_gwh,
        )
    return calculate_equivalent_water_volume(
        payload.dam,
        payload.generation_mw,
        payload.hours,
    )
