"""
Pydantic schemas for power outage records.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


OutageReason = Literal["PM", "CM", "SF", "CO"]


class GeneratingUnitResponse(BaseModel):
    """Generating unit option used by outage records."""

    unit_code: str
    unit_name: str


class OutageRequestCreate(BaseModel):
    """Create one outage window."""

    unit_code: str = Field(..., min_length=1)
    reason: OutageReason
    start_at: datetime
    restore_at: datetime
    expected_mw_reduction: Optional[float] = Field(None, ge=0)
    description: Optional[str] = Field(None, min_length=1)

    @field_validator("unit_code", "description")
    @classmethod
    def strip_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value

    @model_validator(mode="after")
    def validate_times(self):
        if self.restore_at <= self.start_at:
            raise ValueError("restore_at must be after start_at")
        return self


class OutageRequestReplace(OutageRequestCreate):
    """Replace one outage window."""


class OutageRequestResponse(BaseModel):
    """Outage record returned by the API."""

    id: str
    unit_code: str
    unit_name: str
    reason: OutageReason
    start_at: datetime
    restore_at: datetime
    duration_hrs: float
    expected_mw_reduction: Optional[float] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class OutageRequestListResponse(BaseModel):
    """Outage record list response."""

    records: list[OutageRequestResponse]
