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
    calculate_dam_level_at_volume,
    calculate_dam_projection,
    calculate_dam_volume_at_level,
    get_dam_calculation_config,
    list_dam_calculation_configs,
)
from app.core.energy_scheduling import (
    MONTH_KEYS,
    calculate_yearly_power_sources_budget,
    default_yearly_budget_payload,
)
from app.db.database import get_db
from app.schemas.resource_forecasting import (
    DamCalculationConfigResponse,
    DamCalculationRequest,
    DamCalculationResponse,
    HydrologyForecastRequest,
    HydrologyForecastResponse,
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
    SolarForecastResponse,
    SolarIrradiationAggregationRecord,
    SolarIrradiationAggregationResponse,
    SolarIrradiationListResponse,
    SolarIrradiationRecordCreate,
    SolarIrradiationRecordResponse,
    SolarIrradiationRecordUpdate,
    SolarPlantCode,
    SolarPlantInfo,
)

router = APIRouter(prefix="/resource-forecasting", tags=["resource-forecasting"])

FT_TO_M3_FACTOR = 2393.89

RESERVOIRS: dict[ReservoirCode, dict[str, float | str]] = {
    "mps": {"name": "MPS", "min_level_ft": 570.0, "max_level_ft": 641.5},
    "lps": {"name": "LPS", "min_level_ft": 140.0, "max_level_ft": 233.0},
}

RESERVOIR_DAM_CODES: dict[ReservoirCode, str] = {
    "mps": "mulungushi",
    "lps": "mita_hills",
}

SOLAR_PLANTS: dict[SolarPlantCode, dict[str, str]] = {
    "lps_solar": {"name": "LPS Solar Plant"},
}

DEFAULT_MONTHLY_SOLAR_IRRADIATION_W_M2 = [
    760.0,
    740.0,
    700.0,
    660.0,
    620.0,
    590.0,
    610.0,
    660.0,
    720.0,
    780.0,
    800.0,
    790.0,
]


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


def _hydrology_forecasts_collection():
    return get_db()["resource_hydrology_forecasts"]


def _solar_records_collection():
    return get_db()["resource_solar_irradiation_records"]


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


def _energy_yearly_budgets_collection():
    return get_db()["energy_yearly_budgets"]


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_end(year: int, month: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return next_month - timedelta(days=1)


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _forecast_months(base_date: date) -> list[dict[str, Any]]:
    years = [base_date.year, base_date.year + 1]
    return [
        {
            "year": year,
            "month": month,
            "month_key": _month_key(year, month),
            "period_start_date": _month_start(year, month),
            "period_end_date": _month_end(year, month),
        }
        for year in years
        for month in range(1, 13)
    ]


def _latest_budget_record_for_year(year: int) -> Optional[dict]:
    return _energy_yearly_budgets_collection().find_one(
        {"year": year, **_active_filter()},
        sort=[("updated_at", -1), ("created_at", -1), ("_id", -1)],
    )


def _budget_payload_from_record(record: dict) -> dict:
    return {
        "year": record["year"],
        "name": record.get("name"),
        "target_year": record.get("target_year") or record["year"],
        "comparison_year": record.get("comparison_year") or record["year"] - 1,
        **record["inputs"],
    }


def _calculated_budget_for_year(year: int) -> dict:
    record = _latest_budget_record_for_year(year)
    if record:
        return calculate_yearly_power_sources_budget(_budget_payload_from_record(record))
    return calculate_yearly_power_sources_budget(default_yearly_budget_payload(year))


def _calculated_budgets_by_year(years: list[int]) -> dict[int, dict]:
    return {year: _calculated_budget_for_year(year) for year in years}


def _budget_water_for_month(
    budgets_by_year: dict[int, dict],
    reservoir: ReservoirCode,
    year: int,
    month: int,
) -> dict[str, Optional[float]]:
    month_name = MONTH_KEYS[month - 1]
    water_record = budgets_by_year[year]["equivalent_water_volume"][reservoir][month_name]
    gwh_row_code = "mps_gwh" if reservoir == "mps" else "lps_gwh"
    energy_gwh = None
    for row in budgets_by_year[year]["rows"]:
        if row["code"] == gwh_row_code:
            energy_gwh = row["months"][month_name]
            break
    return {
        "water_volume_m3": float(water_record["water_volume_m3"]),
        "water_volume_mm3": float(water_record["water_volume_mm3"]),
        "energy_gwh": energy_gwh,
    }


def _latest_records_by_month_before(
    reservoir: ReservoirCode,
    start_date: date,
    before_date: date,
) -> dict[str, dict]:
    records = list(
        _records_collection()
        .find(
            {
                "reservoir": reservoir,
                "record_date": {
                    "$gte": start_date.isoformat(),
                    "$lt": before_date.isoformat(),
                },
                **_active_filter(),
            }
        )
        .sort([("record_date", 1), ("_id", 1)])
    )
    records_by_month: dict[str, dict] = {}
    for record in records:
        record_date = _parse_record_date(record["record_date"])
        records_by_month[_month_key(record_date.year, record_date.month)] = record
    return records_by_month


def _latest_level_record_on_or_before(
    reservoir: ReservoirCode,
    target_date: date,
) -> Optional[dict]:
    return _records_collection().find_one(
        {
            "reservoir": reservoir,
            "record_date": {"$lte": target_date.isoformat()},
            **_active_filter(),
        },
        sort=[("record_date", -1), ("_id", -1)],
    )


def _record_level_ft(record: dict) -> float:
    return _level_to_ft(record["reservoir_level_value"], record["reservoir_level_unit"])


def _volume_to_level_for_forecast(dam: str, volume_m3: float) -> dict:
    level_result = calculate_dam_level_at_volume(dam, volume_m3, clamp=True)
    return {
        "level_ft": level_result["level_ft"],
        "is_clamped": level_result["is_clamped"],
    }


def _projection_start_for_reservoir(
    reservoir: ReservoirCode,
    base_date: date,
) -> dict:
    dam = RESERVOIR_DAM_CODES[reservoir]
    current_month_start = _month_start(base_date.year, base_date.month)
    previous_month_end = current_month_start - timedelta(days=1)
    latest_record = _latest_level_record_on_or_before(reservoir, previous_month_end)
    if latest_record:
        level_ft = _record_level_ft(latest_record)
        source = f"previous_month_monitoring:{latest_record['record_date']}"
    else:
        config = get_dam_calculation_config(dam)
        level_ft = config.default_current_level_ft
        source = "dam_default"

    volume_result = calculate_dam_volume_at_level(dam, level_ft)
    if volume_result["is_off_range"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{reservoir.upper()} projection start level is outside the "
                "configured dam calculation range"
            ),
        )
    return {
        "level_ft": level_ft,
        "volume_m3": volume_result["volume_m3"],
        "source": source,
    }


def _rainfall_input_for_reservoir(
    payload: HydrologyForecastRequest,
    reservoir: ReservoirCode,
) -> dict:
    rainfall = payload.rainfall.get(reservoir)
    if not rainfall:
        return {
            "total_volume_mm3": 0.0,
            "monthly_allocations_mm3": {},
            "allocated_volume_mm3": 0.0,
            "remaining_volume_mm3": 0.0,
            "overallocated_volume_mm3": 0.0,
        }

    allocations = dict(rainfall.monthly_allocations_mm3)
    allocated = sum(allocations.values())
    remaining = rainfall.total_volume_mm3 - allocated
    return {
        "total_volume_mm3": rainfall.total_volume_mm3,
        "monthly_allocations_mm3": allocations,
        "allocated_volume_mm3": allocated,
        "remaining_volume_mm3": remaining,
        "overallocated_volume_mm3": max(abs(remaining), 0.0) if remaining < 0 else 0.0,
    }


def _validate_rainfall_months(
    payload: HydrologyForecastRequest,
    base_date: date,
) -> None:
    valid_months = {month["month_key"] for month in _forecast_months(base_date)}
    for reservoir, rainfall in payload.rainfall.items():
        for allocation_month in rainfall.monthly_allocations_mm3:
            if allocation_month not in valid_months:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{reservoir} rainfall allocation month {allocation_month} "
                        "is outside the current/next year forecast window"
                    ),
                )


def _saved_hydrology_forecast_for_base_date(base_date: date) -> Optional[dict]:
    return _hydrology_forecasts_collection().find_one(
        {"start_year": base_date.year, **_active_filter()},
        sort=[("updated_at", -1), ("_id", -1)],
    )


def _hydrology_request_from_saved_forecast(
    base_date: date,
    saved_forecast: Optional[dict],
) -> HydrologyForecastRequest:
    if not saved_forecast:
        return HydrologyForecastRequest(base_date=base_date)
    return HydrologyForecastRequest(
        base_date=base_date,
        rainfall=saved_forecast.get("rainfall") or {},
    )


def _save_hydrology_forecast(payload: HydrologyForecastRequest) -> dict:
    base_date = payload.base_date or _utcnow().date()
    payload = HydrologyForecastRequest(
        base_date=base_date,
        rainfall=payload.rainfall,
    )
    _validate_rainfall_months(payload, base_date)

    now = _utcnow()
    rainfall = {
        reservoir: forecast.model_dump()
        for reservoir, forecast in payload.rainfall.items()
    }
    return _hydrology_forecasts_collection().find_one_and_update(
        {"start_year": base_date.year, **_active_filter()},
        {
            "$set": {
                "start_year": base_date.year,
                "years": [base_date.year, base_date.year + 1],
                "base_date": base_date.isoformat(),
                "rainfall": rainfall,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


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


def _solar_plant_name(plant: SolarPlantCode) -> str:
    return SOLAR_PLANTS[plant]["name"]


def _solar_record_response(record: dict) -> dict:
    plant = record["plant"]
    return {
        "id": str(record["_id"]),
        "plant": plant,
        "plant_name": _solar_plant_name(plant),
        "record_date": record["record_date"],
        "irradiation_w_m2": record["irradiation_w_m2"],
        "weather_condition": record.get("weather_condition"),
        "notes": record.get("notes"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _solar_weather_condition(value: float) -> str:
    if value >= 760:
        return "sunny"
    if value >= 680:
        return "partly_cloudy"
    if value >= 610:
        return "cloudy"
    return "overcast"


def _solar_monthly_actuals(
    plant: SolarPlantCode,
    start_date: date,
    end_date: date,
) -> dict[str, dict[str, Any]]:
    records = list(
        _solar_records_collection()
        .find(
            {
                "plant": plant,
                "record_date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat(),
                },
                **_active_filter(),
            }
        )
        .sort([("record_date", 1), ("_id", 1)])
    )
    monthly: dict[str, dict[str, Any]] = {}
    for record in records:
        record_date = _parse_record_date(record["record_date"])
        month_key = _month_key(record_date.year, record_date.month)
        bucket = monthly.setdefault(
            month_key,
            {
                "total": 0.0,
                "count": 0,
                "weather_counts": {},
            },
        )
        bucket["total"] += float(record["irradiation_w_m2"])
        bucket["count"] += 1
        weather_condition = record.get("weather_condition")
        if weather_condition:
            bucket["weather_counts"][weather_condition] = (
                bucket["weather_counts"].get(weather_condition, 0) + 1
            )
    return monthly


def _solar_historical_month_averages(
    plant: SolarPlantCode,
    before_date: date,
) -> dict[int, float]:
    records = list(
        _solar_records_collection().find(
            {
                "plant": plant,
                "record_date": {"$lt": before_date.isoformat()},
                **_active_filter(),
            }
        )
    )
    monthly: dict[int, dict[str, float]] = {}
    for record in records:
        record_date = _parse_record_date(record["record_date"])
        bucket = monthly.setdefault(record_date.month, {"total": 0.0, "count": 0})
        bucket["total"] += float(record["irradiation_w_m2"])
        bucket["count"] += 1
    return {
        month: bucket["total"] / bucket["count"]
        for month, bucket in monthly.items()
        if bucket["count"]
    }


def _solar_prediction_w_m2(
    month: int,
    historical_month_averages: dict[int, float],
) -> float:
    return historical_month_averages.get(
        month,
        DEFAULT_MONTHLY_SOLAR_IRRADIATION_W_M2[month - 1],
    )


def _dominant_weather_condition(
    weather_counts: dict[str, int],
    fallback_value: float,
) -> str:
    if weather_counts:
        return max(weather_counts.items(), key=lambda item: (item[1], item[0]))[0]
    return _solar_weather_condition(fallback_value)


@router.get(
    "/solar-forecasting/plants",
    response_model=list[SolarPlantInfo],
)
def list_solar_plants():
    """List solar plants available for resource forecasting."""
    return [
        SolarPlantInfo(code=plant_code, name=plant["name"])
        for plant_code, plant in SOLAR_PLANTS.items()
    ]


@router.post(
    "/solar-forecasting/records",
    response_model=SolarIrradiationRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_solar_irradiation_record(payload: SolarIrradiationRecordCreate):
    """Create a daily solar irradiation record."""
    now = _utcnow()
    document = payload.model_dump()
    document["record_date"] = payload.record_date.isoformat()
    document["created_at"] = now
    document["updated_at"] = now

    try:
        result = _solar_records_collection().insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A solar irradiation record already exists for this plant and date",
        )

    record = _solar_records_collection().find_one({"_id": result.inserted_id})
    return _solar_record_response(record)


@router.get(
    "/solar-forecasting/records",
    response_model=SolarIrradiationListResponse,
)
def list_solar_irradiation_records(
    plant: SolarPlantCode = Query("lps_solar"),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
):
    """List solar irradiation records by date range."""
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    query_filter = {"plant": plant, **_active_filter()}
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
        _solar_records_collection()
        .find(query_filter)
        .sort([("record_date", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit + 1)
    )

    next_cursor = None
    if len(records) > limit:
        next_cursor = _encode_cursor(records[limit - 1])
        records = records[:limit]

    return SolarIrradiationListResponse(
        records=[_solar_record_response(record) for record in records],
        next_cursor=next_cursor,
    )


@router.get(
    "/solar-forecasting/records/{record_id}",
    response_model=SolarIrradiationRecordResponse,
)
def get_solar_irradiation_record(record_id: str):
    """Fetch one solar irradiation record."""
    record = _solar_records_collection().find_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return _solar_record_response(record)


@router.patch(
    "/solar-forecasting/records/{record_id}",
    response_model=SolarIrradiationRecordResponse,
)
def update_solar_irradiation_record(
    record_id: str,
    payload: SolarIrradiationRecordUpdate,
):
    """Partially update a solar irradiation record."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No record values provided")

    if "record_date" in update_data:
        update_data["record_date"] = update_data["record_date"].isoformat()
    update_data["updated_at"] = _utcnow()

    try:
        record = _solar_records_collection().find_one_and_update(
            {"_id": _parse_object_id(record_id, "record"), **_active_filter()},
            {"$set": update_data},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="A solar irradiation record already exists for this plant and date",
        )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return _solar_record_response(record)


@router.delete("/solar-forecasting/records/{record_id}")
def delete_solar_irradiation_record(record_id: str):
    """Soft delete a solar irradiation record."""
    result = _solar_records_collection().update_one(
        {"_id": _parse_object_id(record_id, "record"), **_active_filter()},
        {"$set": {"deleted_at": _utcnow(), "updated_at": _utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"deleted": True}


@router.get(
    "/solar-forecasting/aggregate",
    response_model=SolarIrradiationAggregationResponse,
)
def aggregate_solar_irradiation_records(
    plant: SolarPlantCode = Query("lps_solar"),
    group_by: ResourceAggregationGroup = Query(...),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    """Aggregate solar irradiation records to weeks, months, or years."""
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    query_filter = {"plant": plant, **_active_filter()}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date.isoformat()
        if end_date:
            date_filter["$lte"] = end_date.isoformat()
        query_filter["record_date"] = date_filter

    records = list(_solar_records_collection().find(query_filter).sort([("record_date", 1)]))
    grouped: dict[tuple[date, date], dict[str, Any]] = {}

    for record in records:
        record_date = _parse_record_date(record["record_date"])
        period_start, period_end = _period_bounds(record_date, group_by)
        group = grouped.setdefault(
            (period_start, period_end),
            {
                "count": 0,
                "total": 0.0,
                "min": None,
                "max": None,
            },
        )
        value = float(record["irradiation_w_m2"])
        group["count"] += 1
        group["total"] += value
        group["min"] = value if group["min"] is None else min(group["min"], value)
        group["max"] = value if group["max"] is None else max(group["max"], value)

    response_records = [
        SolarIrradiationAggregationRecord(
            plant=plant,
            group_by=group_by,
            period_start_date=period_start,
            period_end_date=period_end,
            record_count=group["count"],
            avg_irradiation_w_m2=group["total"] / group["count"] if group["count"] else None,
            min_irradiation_w_m2=group["min"],
            max_irradiation_w_m2=group["max"],
        )
        for (period_start, period_end), group in grouped.items()
    ]
    return SolarIrradiationAggregationResponse(records=response_records)


@router.get(
    "/solar-forecasting",
    response_model=SolarForecastResponse,
)
def get_solar_forecast(
    plant: SolarPlantCode = Query("lps_solar"),
    base_date: Optional[date] = Query(None),
):
    """Return current and next year solar irradiation actuals and projections."""
    resolved_base_date = base_date or _utcnow().date()
    years = [resolved_base_date.year, resolved_base_date.year + 1]
    months = _forecast_months(resolved_base_date)
    current_month_start = _month_start(resolved_base_date.year, resolved_base_date.month)
    actuals = _solar_monthly_actuals(
        plant,
        date(resolved_base_date.year, 1, 1),
        _month_end(resolved_base_date.year, resolved_base_date.month),
    )
    historical_averages = _solar_historical_month_averages(plant, current_month_start)

    response_records = []
    for month_info in months:
        month_key = month_info["month_key"]
        is_actual_month = month_info["period_start_date"] <= current_month_start
        actual = actuals.get(month_key)
        if is_actual_month and actual and actual["count"]:
            irradiation_w_m2 = actual["total"] / actual["count"]
            source = "actual"
            actual_record_count = actual["count"]
            weather_condition = _dominant_weather_condition(
                actual["weather_counts"],
                irradiation_w_m2,
            )
        else:
            irradiation_w_m2 = _solar_prediction_w_m2(
                month_info["month"],
                historical_averages,
            )
            source = "predicted"
            actual_record_count = 0
            weather_condition = _solar_weather_condition(irradiation_w_m2)

        response_records.append(
            {
                "plant": plant,
                "plant_name": _solar_plant_name(plant),
                "month_key": month_key,
                "year": month_info["year"],
                "month": month_info["month"],
                "period_start_date": month_info["period_start_date"],
                "period_end_date": month_info["period_end_date"],
                "source": source,
                "irradiation_w_m2": irradiation_w_m2,
                "actual_record_count": actual_record_count,
                "weather_condition": weather_condition,
            }
        )

    return SolarForecastResponse(
        base_date=resolved_base_date,
        years=years,
        plant=plant,
        plant_name=_solar_plant_name(plant),
        records=response_records,
    )


def _calculate_hydrology_forecast_response(
    payload: HydrologyForecastRequest,
    saved_forecast: Optional[dict] = None,
) -> HydrologyForecastResponse:
    base_date = payload.base_date or _utcnow().date()
    _validate_rainfall_months(payload, base_date)

    years = [base_date.year, base_date.year + 1]
    months = _forecast_months(base_date)
    budgets_by_year = _calculated_budgets_by_year(years)
    current_month_start = _month_start(base_date.year, base_date.month)

    forecast_records = []
    for reservoir in RESERVOIRS:
        dam = RESERVOIR_DAM_CODES[reservoir]
        dam_config = get_dam_calculation_config(dam)
        historical_records = _latest_records_by_month_before(
            reservoir,
            date(base_date.year, 1, 1),
            current_month_start,
        )
        projection_start = _projection_start_for_reservoir(reservoir, base_date)
        rainfall = _rainfall_input_for_reservoir(payload, reservoir)

        projected_volume_m3 = float(projection_start["volume_m3"])
        response_months = []

        for month_info in months:
            month_key = month_info["month_key"]
            is_past_month = month_info["period_start_date"] < current_month_start
            budget_water = {
                "water_volume_m3": 0.0,
                "water_volume_mm3": 0.0,
                "energy_gwh": None,
            }
            rainfall_volume_mm3 = 0.0
            rainfall_volume_m3 = 0.0
            rainfall_level_adjustment_ft = 0.0
            monitoring_record = None
            observed_level_ft = None
            projected_level_ft = None
            rainfall_adjusted_level_ft = None
            projected_level_clamped = False
            rainfall_adjusted_level_clamped = False
            response_projected_volume_m3 = None
            response_rainfall_adjusted_volume_m3 = None

            if is_past_month:
                monitoring_record = historical_records.get(month_key)
                if monitoring_record:
                    observed_level_ft = _record_level_ft(monitoring_record)
                    projected_level_ft = observed_level_ft
                    observed_volume = calculate_dam_volume_at_level(dam, observed_level_ft)
                    if not observed_volume["is_off_range"]:
                        response_projected_volume_m3 = observed_volume["volume_m3"]
            else:
                budget_water = _budget_water_for_month(
                    budgets_by_year,
                    reservoir,
                    month_info["year"],
                    month_info["month"],
                )
                rainfall_volume_mm3 = rainfall["monthly_allocations_mm3"].get(month_key, 0.0)
                rainfall_volume_m3 = rainfall_volume_mm3 * 1_000_000

                projected_volume_m3 -= float(budget_water["water_volume_m3"])
                projected_level = _volume_to_level_for_forecast(dam, projected_volume_m3)
                projected_level_ft = projected_level["level_ft"]
                rainfall_adjusted_volume_m3 = projected_volume_m3 + rainfall_volume_m3
                rainfall_adjusted_level = _volume_to_level_for_forecast(
                    dam,
                    rainfall_adjusted_volume_m3,
                )
                rainfall_adjusted_level_ft = rainfall_adjusted_level["level_ft"]
                rainfall_level_adjustment_ft = (
                    rainfall_adjusted_level_ft - projected_level_ft
                    if rainfall_adjusted_level_ft is not None and projected_level_ft is not None
                    else 0.0
                )
                projected_level_clamped = projected_level["is_clamped"]
                rainfall_adjusted_level_clamped = rainfall_adjusted_level["is_clamped"]
                response_projected_volume_m3 = projected_volume_m3
                response_rainfall_adjusted_volume_m3 = rainfall_adjusted_volume_m3

            response_months.append(
                {
                    "reservoir": reservoir,
                    "dam": dam,
                    "month_key": month_key,
                    "year": month_info["year"],
                    "month": month_info["month"],
                    "period_start_date": month_info["period_start_date"],
                    "period_end_date": month_info["period_end_date"],
                    "source": "monitoring" if is_past_month else "projected",
                    "is_past_month": is_past_month,
                    "monitoring_record_id": (
                        str(monitoring_record["_id"]) if monitoring_record else None
                    ),
                    "monitoring_record_date": (
                        _parse_record_date(monitoring_record["record_date"])
                        if monitoring_record
                        else None
                    ),
                    "observed_level_ft": observed_level_ft,
                    "projected_level_ft": projected_level_ft,
                    "rainfall_adjusted_level_ft": rainfall_adjusted_level_ft,
                    "projected_volume_m3": response_projected_volume_m3,
                    "rainfall_adjusted_volume_m3": response_rainfall_adjusted_volume_m3,
                    "budget_water_volume_m3": float(budget_water["water_volume_m3"]),
                    "budget_water_volume_mm3": float(budget_water["water_volume_mm3"]),
                    "rainfall_volume_m3": rainfall_volume_m3,
                    "rainfall_volume_mm3": rainfall_volume_mm3,
                    "rainfall_level_adjustment_ft": rainfall_level_adjustment_ft,
                    "budget_energy_gwh": budget_water["energy_gwh"],
                    "projected_level_clamped": projected_level_clamped,
                    "rainfall_adjusted_level_clamped": rainfall_adjusted_level_clamped,
                }
            )

        forecast_records.append(
            {
                "reservoir": reservoir,
                "reservoir_name": str(RESERVOIRS[reservoir]["name"]),
                "dam": dam,
                "dam_name": dam_config.name,
                "min_level_ft": dam_config.min_level_ft,
                "max_level_ft": dam_config.max_level_ft,
                "projection_start_level_ft": projection_start["level_ft"],
                "projection_start_volume_m3": projection_start["volume_m3"],
                "projection_start_source": projection_start["source"],
                "rainfall_total_volume_mm3": rainfall["total_volume_mm3"],
                "rainfall_allocated_volume_mm3": rainfall["allocated_volume_mm3"],
                "rainfall_remaining_volume_mm3": rainfall["remaining_volume_mm3"],
                "rainfall_overallocated_volume_mm3": rainfall["overallocated_volume_mm3"],
                "months": response_months,
            }
        )

    return HydrologyForecastResponse(
        base_date=base_date,
        years=years,
        saved_forecast_id=str(saved_forecast["_id"]) if saved_forecast else None,
        saved_forecast_updated_at=saved_forecast.get("updated_at") if saved_forecast else None,
        records=forecast_records,
    )


@router.get(
    "/hydrology-forecasting",
    response_model=HydrologyForecastResponse,
)
def get_hydrology_forecast(
    base_date: Optional[date] = Query(None),
):
    """Return current and next year hydrology forecast with the saved rainfall forecast."""
    resolved_base_date = base_date or _utcnow().date()
    saved_forecast = _saved_hydrology_forecast_for_base_date(resolved_base_date)
    return _calculate_hydrology_forecast_response(
        _hydrology_request_from_saved_forecast(resolved_base_date, saved_forecast),
        saved_forecast=saved_forecast,
    )


@router.put(
    "/hydrology-forecasting",
    response_model=HydrologyForecastResponse,
)
def save_hydrology_forecast(payload: HydrologyForecastRequest):
    """Persist rainfall forecast adjustments and return the resulting forecast."""
    saved_forecast = _save_hydrology_forecast(payload)
    base_date = payload.base_date or _utcnow().date()
    return _calculate_hydrology_forecast_response(
        _hydrology_request_from_saved_forecast(base_date, saved_forecast),
        saved_forecast=saved_forecast,
    )


@router.post(
    "/hydrology-forecasting/calculate",
    response_model=HydrologyForecastResponse,
)
def calculate_hydrology_forecast(payload: HydrologyForecastRequest):
    """Return current and next year hydrology forecast with rainfall allocations."""
    return _calculate_hydrology_forecast_response(payload)


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
