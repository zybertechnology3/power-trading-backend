"""
Pydantic schemas for resource forecasting.
"""

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


ReservoirCode = Literal["mps", "lps"]
ReservoirLevelUnit = Literal["ft", "m3"]
ResourceCustomFieldType = Literal["text", "number", "date", "boolean", "json"]
ResourceAggregationGroup = Literal["week", "month", "year"]
DamCalculationCode = Literal["mita_hills", "mulungushi"]
HydrologyForecastSource = Literal["monitoring", "projected"]


class ReservoirInfo(BaseModel):
    """Reservoir option returned to the frontend."""

    code: ReservoirCode
    name: str
    min_level_ft: float
    min_level_m3: float
    max_level_ft: float
    max_level_m3: float


class LevelMonitoringFieldCreate(BaseModel):
    """Create a persistent extra field for reservoir level monitoring."""

    reservoir: ReservoirCode
    key: Optional[str] = Field(None, min_length=1)
    label: str = Field(..., min_length=1)
    field_type: ResourceCustomFieldType = "text"
    unit: Optional[str] = Field(None, min_length=1)

    @field_validator("key", "label", "unit")
    @classmethod
    def strip_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class LevelMonitoringFieldUpdate(BaseModel):
    """Partial update for a persistent level monitoring field."""

    label: Optional[str] = Field(None, min_length=1)
    field_type: Optional[ResourceCustomFieldType] = None
    unit: Optional[str] = Field(None, min_length=1)

    @field_validator("label", "unit")
    @classmethod
    def strip_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class LevelMonitoringFieldResponse(BaseModel):
    """Persistent field definition returned by the API."""

    id: str
    reservoir: ReservoirCode
    key: str
    label: str
    field_type: ResourceCustomFieldType
    unit: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class LevelMonitoringRecordCreate(BaseModel):
    """Create a daily reservoir level monitoring record."""

    reservoir: ReservoirCode
    record_date: date
    daily_inflow: float = 0
    unaccounted_inflow: float = 0
    reservoir_level_value: float
    reservoir_level_unit: ReservoirLevelUnit
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_custom_field_keys(self):
        for key in self.custom_fields:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("custom field keys must not be blank")
        return self


class LevelMonitoringRecordUpdate(BaseModel):
    """Partial update for a daily reservoir level monitoring record."""

    record_date: Optional[date] = None
    daily_inflow: Optional[float] = None
    unaccounted_inflow: Optional[float] = None
    reservoir_level_value: Optional[float] = None
    reservoir_level_unit: Optional[ReservoirLevelUnit] = None
    custom_fields: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_custom_field_keys(self):
        if self.custom_fields is None:
            return self
        for key in self.custom_fields:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("custom field keys must not be blank")
        return self


class LevelMonitoringRecordResponse(BaseModel):
    """Daily reservoir level monitoring record returned by the API."""

    id: str
    reservoir: ReservoirCode
    record_date: date
    daily_inflow: float
    unaccounted_inflow: float
    total_daily_inflow: float
    reservoir_level_value: float
    reservoir_level_unit: ReservoirLevelUnit
    reservoir_level_ft: Optional[float] = None
    reservoir_level_m3: Optional[float] = None
    min_level_ft: float
    min_level_m3: float
    max_level_ft: float
    max_level_m3: float
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class LevelMonitoringListResponse(BaseModel):
    """Cursor-paginated level monitoring records plus field definitions."""

    records: list[LevelMonitoringRecordResponse]
    fields: list[LevelMonitoringFieldResponse] = Field(default_factory=list)
    next_cursor: Optional[str] = None


class LevelMonitoringAggregationRecord(BaseModel):
    """Aggregated reservoir level monitoring values."""

    reservoir: ReservoirCode
    group_by: ResourceAggregationGroup
    period_start_date: date
    period_end_date: date
    record_count: int
    daily_inflow: float
    unaccounted_inflow: float
    total_daily_inflow: float
    avg_reservoir_level_ft: Optional[float] = None
    avg_reservoir_level_m3: Optional[float] = None


class LevelMonitoringAggregationResponse(BaseModel):
    """Aggregated level monitoring list."""

    records: list[LevelMonitoringAggregationRecord]


class DamCalculationLookupRangeResponse(BaseModel):
    """Selected level/volume range used for interpolation."""

    lower_level_ft: float
    upper_level_ft: float
    lower_volume_m3: float
    upper_volume_m3: float


class DamCalculationConfigResponse(BaseModel):
    """Calculation tool configuration for one dam."""

    code: DamCalculationCode
    name: str
    min_level_ft: float
    max_level_ft: float
    default_current_level_ft: float
    default_evaporation_rate: float
    default_production_rate_mw: float
    dead_storage_volume_m3: Optional[float] = None
    fill_reference_volume_m3: Optional[float] = None
    energy_m3_per_kwh: Optional[float] = None
    generation_factor: Optional[float] = None
    lookup_table: Optional[list[DamCalculationLookupRangeResponse]] = None


class DamCalculationInputEcho(BaseModel):
    """Inputs echoed back with the calculation result."""

    current_level_ft: float
    evaporation_rate: float
    production_rate_mw: float


class DamCalculationRequest(BaseModel):
    """Inputs for the dam volume calculation tool."""

    dam: DamCalculationCode = "mita_hills"
    current_level_ft: float = Field(..., ge=0)
    evaporation_rate: float = Field(..., ge=0, lt=1)
    production_rate_mw: float = Field(..., gt=0)


class DamCalculationResponse(BaseModel):
    """Dam volume and generation projection calculation result."""

    dam: DamCalculationCode
    dam_name: str
    is_off_range: bool
    message: Optional[str] = None
    input: DamCalculationInputEcho
    lookup_range: Optional[DamCalculationLookupRangeResponse] = None
    calculated_dam_volume_m3: Optional[float] = None
    useful_dam_volume_m3: Optional[float] = None
    percentage_fill: Optional[float] = None
    equivalent_energy_kwh: Optional[float] = None
    equivalent_energy_gwh: Optional[float] = None
    projected_generation_days: Optional[float] = None
    projected_generation_months: Optional[float] = None


class HydrologyRainfallAllocationInput(BaseModel):
    """Rainfall volume forecast allocated by calendar month."""

    total_volume_mm3: float = Field(0, ge=0)
    monthly_allocations_mm3: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_monthly_allocations(self):
        for month_key, value in self.monthly_allocations_mm3.items():
            try:
                date.fromisoformat(f"{month_key}-01")
            except ValueError:
                raise ValueError("monthly allocation keys must use YYYY-MM format")
            if value < 0:
                raise ValueError("monthly allocation values must be non-negative")
        if sum(self.monthly_allocations_mm3.values()) > self.total_volume_mm3:
            raise ValueError("monthly allocations cannot exceed total_volume_mm3")
        return self


class HydrologyForecastRequest(BaseModel):
    """Calculate current and next year hydrology level forecast."""

    base_date: Optional[date] = None
    rainfall: dict[ReservoirCode, HydrologyRainfallAllocationInput] = Field(
        default_factory=dict
    )


class HydrologyForecastMonthResponse(BaseModel):
    """One month in the hydrology forecast chart."""

    reservoir: ReservoirCode
    dam: DamCalculationCode
    month_key: str
    year: int
    month: int
    period_start_date: date
    period_end_date: date
    source: HydrologyForecastSource
    is_past_month: bool
    monitoring_record_id: Optional[str] = None
    monitoring_record_date: Optional[date] = None
    observed_level_ft: Optional[float] = None
    projected_level_ft: Optional[float] = None
    rainfall_adjusted_level_ft: Optional[float] = None
    projected_volume_m3: Optional[float] = None
    rainfall_adjusted_volume_m3: Optional[float] = None
    budget_water_volume_m3: float = 0
    budget_water_volume_mm3: float = 0
    rainfall_volume_m3: float = 0
    rainfall_volume_mm3: float = 0
    budget_energy_gwh: Optional[float] = None
    projected_level_clamped: bool = False
    rainfall_adjusted_level_clamped: bool = False


class HydrologyForecastReservoirResponse(BaseModel):
    """Hydrology forecast for one reservoir."""

    reservoir: ReservoirCode
    reservoir_name: str
    dam: DamCalculationCode
    dam_name: str
    min_level_ft: float
    max_level_ft: float
    projection_start_level_ft: float
    projection_start_volume_m3: float
    projection_start_source: str
    rainfall_total_volume_mm3: float
    rainfall_allocated_volume_mm3: float
    rainfall_remaining_volume_mm3: float
    months: list[HydrologyForecastMonthResponse]


class HydrologyForecastResponse(BaseModel):
    """Hydrology forecast response for both dams."""

    base_date: date
    years: list[int]
    records: list[HydrologyForecastReservoirResponse]
