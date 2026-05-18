"""
Pydantic schemas for customer contracts.
"""

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


ContractType = Literal["ppa", "wheeling"]
Firmness = Literal["firm", "non-firm"]

DEFAULT_INDEXATION_FORMULA = "T(n) = T(0) × (1 + α·ΔCPI + β·ΔPPI + γ·ΔFX)"


class ContractCustomField(BaseModel):
    """User-defined custom contract field."""

    id: Optional[str] = None
    label: Optional[str] = None
    value: Any = None


class ContractFileMetadata(BaseModel):
    """Uploaded supporting document metadata."""

    id: str
    name: str
    size: int = Field(..., ge=0)
    url: str
    content_type: Optional[str] = None


class ContractCreate(BaseModel):
    """Create a customer contract."""

    customer: str = Field(..., min_length=1)
    contract_type: ContractType
    effective_date: date
    duration: str = Field(..., min_length=1)
    firmness: Firmness
    capacity_mw: float = Field(..., ge=0)
    tariff_energy_usd_per_mwh: float = Field(..., ge=0)
    tariff_overall_usd_per_mwh: float = Field(..., ge=0)
    ppi_series: str = Field(..., min_length=1)
    custom_fields: list[ContractCustomField] = Field(default_factory=list)

    @field_validator("customer", "duration", "ppi_series")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class ContractUpdate(BaseModel):
    """Partial update for a customer contract."""

    customer: Optional[str] = Field(None, min_length=1)
    contract_type: Optional[ContractType] = None
    effective_date: Optional[date] = None
    duration: Optional[str] = Field(None, min_length=1)
    firmness: Optional[Firmness] = None
    capacity_mw: Optional[float] = Field(None, ge=0)
    tariff_energy_usd_per_mwh: Optional[float] = Field(None, ge=0)
    tariff_overall_usd_per_mwh: Optional[float] = Field(None, ge=0)
    ppi_series: Optional[str] = Field(None, min_length=1)
    custom_fields: Optional[list[ContractCustomField]] = None

    @field_validator("customer", "duration", "ppi_series")
    @classmethod
    def strip_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class ContractResponse(BaseModel):
    """Customer contract returned by the API."""

    id: str
    customer: str
    contract_type: ContractType
    effective_date: date
    duration: str
    firmness: Firmness
    capacity_mw: float
    tariff_energy_usd_per_mwh: float
    tariff_overall_usd_per_mwh: float
    indexation_formula: str
    ppi_series: str
    custom_fields: list[ContractCustomField] = Field(default_factory=list)
    files: list[ContractFileMetadata] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ContractListResponse(BaseModel):
    """Cursor-paginated contract list."""

    records: list[ContractResponse]
    next_cursor: Optional[str] = None
