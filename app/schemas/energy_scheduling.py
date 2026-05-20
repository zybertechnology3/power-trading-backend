"""
Pydantic schemas for energy scheduling.
"""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


MonthlyOptionalValues = list[Optional[float]]


class YearlyBudgetInputs(BaseModel):
    """Editable inputs for the Power Sources yearly budget table."""

    mps_mw: MonthlyOptionalValues = Field(..., min_length=12, max_length=12)
    lps_mw: MonthlyOptionalValues = Field(..., min_length=12, max_length=12)
    lps_solar_mw: MonthlyOptionalValues = Field(..., min_length=12, max_length=12)
    sapp_purchase_mw: MonthlyOptionalValues = Field(..., min_length=12, max_length=12)

    @field_validator(
        "mps_mw",
        "lps_mw",
        "lps_solar_mw",
        "sapp_purchase_mw",
    )
    @classmethod
    def validate_non_negative_monthly_values(
        cls,
        values: MonthlyOptionalValues,
    ) -> MonthlyOptionalValues:
        for value in values:
            if value is not None and value < 0:
                raise ValueError("monthly values must be non-negative or null")
        return values


class YearlyBudgetCalculateRequest(BaseModel):
    """Calculate a yearly budget schedule without saving it."""

    year: int = Field(..., ge=2000, le=2100)
    name: Optional[str] = Field(None, min_length=1)
    target_year: Optional[int] = Field(None, ge=2000, le=2100)
    comparison_year: Optional[int] = Field(None, ge=2000, le=2100)
    inputs: YearlyBudgetInputs

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class YearlyBudgetCreate(YearlyBudgetCalculateRequest):
    """Create and save a yearly budget schedule."""

    name: str = Field(..., min_length=1)


class YearlyBudgetUpdate(BaseModel):
    """Partial update for a saved yearly budget schedule."""

    name: Optional[str] = Field(None, min_length=1)
    year: Optional[int] = Field(None, ge=2000, le=2100)
    target_year: Optional[int] = Field(None, ge=2000, le=2100)
    comparison_year: Optional[int] = Field(None, ge=2000, le=2100)
    inputs: Optional[YearlyBudgetInputs] = None

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class BudgetMonthInfo(BaseModel):
    """Month metadata for the schedule table."""

    key: str
    month: int
    date: date


class BudgetRowResponse(BaseModel):
    """Calculated Power Sources table row."""

    code: str
    label: str
    unit: str
    category: str
    source: str
    summary_type: str
    months: dict[str, Optional[float]]
    summary_value: Optional[float] = None
    annualized_gwh: Optional[float] = None
    prior_year_value: Optional[float] = None
    variance: Optional[float] = None
    formula: Optional[str] = None


class YearlyBudgetCalculationResponse(BaseModel):
    """Calculated yearly budget table."""

    year: int
    target_year: int
    comparison_year: int
    name: Optional[str] = None
    months: list[BudgetMonthInfo]
    inputs: dict[str, Any]
    rows: list[BudgetRowResponse]
    equivalent_water_volume: dict[str, dict[str, dict[str, Any]]]


class YearlyBudgetResponse(YearlyBudgetCalculationResponse):
    """Saved yearly budget schedule."""

    id: str
    created_at: datetime
    updated_at: datetime


class BudgetableYearResponse(YearlyBudgetCalculationResponse):
    """Current/next year budget, saved or defaulted."""

    id: Optional[str] = None
    is_saved: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BudgetableYearsResponse(BaseModel):
    """Budgetable current and next year schedules."""

    base_year: int
    years: list[int]
    records: list[BudgetableYearResponse]


class YearlyBudgetListResponse(BaseModel):
    """Paginated yearly budget schedules."""

    records: list[YearlyBudgetResponse]
    next_cursor: Optional[str] = None


class EquivalentWaterVolumeRequest(BaseModel):
    """Backpass generation to equivalent water volume."""

    dam: str = Field(..., pattern="^(mita_hills|mulungushi)$")
    generation_mw: Optional[float] = Field(None, ge=0)
    energy_gwh: Optional[float] = Field(None, ge=0)
    hours: float = Field(720, gt=0)

    @model_validator(mode="after")
    def validate_generation_input(self):
        if self.generation_mw is None and self.energy_gwh is None:
            raise ValueError("Provide either generation_mw or energy_gwh")
        if self.generation_mw is not None and self.energy_gwh is not None:
            raise ValueError("Provide only one of generation_mw or energy_gwh")
        return self


class EquivalentWaterVolumeResponse(BaseModel):
    """Equivalent water volume result."""

    dam: str
    dam_name: str
    generation_mw: Optional[float] = None
    hours: Optional[float] = None
    energy_mwh: Optional[float] = None
    energy_gwh: float
    energy_kwh: float
    energy_m3_per_kwh: float
    water_volume_m3: float
    water_volume_mm3: float
