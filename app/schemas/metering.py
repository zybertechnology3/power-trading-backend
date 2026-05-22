"""
Pydantic schemas for metering.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


MeteringSiteCode = Literal["mps", "lps"]
MeterEntryMode = Literal["manual", "automatic"]


class MeteringSiteInfo(BaseModel):
    """Metering site option returned to the frontend."""

    code: MeteringSiteCode
    name: str


class MeterResponse(BaseModel):
    """Meter definition returned by the API."""

    id: str
    site: MeteringSiteCode
    name: str
    column_key: str
    entry_mode: MeterEntryMode
    unit: str = "MWh"
    sort_order: int
    created_at: datetime
    updated_at: datetime


class MeterUpdate(BaseModel):
    """Update manual/automatic entry mode for a meter."""

    entry_mode: MeterEntryMode


class MeterCaptureReadingCreate(BaseModel):
    """Create a 30-minute meter capture row."""

    site: MeteringSiteCode
    interval_start: datetime
    readings: dict[str, float] = Field(default_factory=dict)
    source: MeterEntryMode = "manual"
    notes: Optional[str] = Field(None, min_length=1)

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("notes must not be blank")
        return stripped_value

    @model_validator(mode="after")
    def validate_interval(self):
        if (
            self.interval_start.minute not in (0, 30)
            or self.interval_start.second != 0
            or self.interval_start.microsecond != 0
        ):
            raise ValueError("interval_start must be aligned to a 30-minute boundary")
        for meter_id, value in self.readings.items():
            if not isinstance(meter_id, str) or not meter_id.strip():
                raise ValueError("reading meter IDs must not be blank")
            if value < 0:
                raise ValueError("meter readings must be non-negative")
        return self


class MeterCaptureReadingUpdate(BaseModel):
    """Partial update for a 30-minute meter capture row."""

    interval_start: Optional[datetime] = None
    readings: Optional[dict[str, float]] = None
    source: Optional[MeterEntryMode] = None
    notes: Optional[str] = Field(None, min_length=1)

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("notes must not be blank")
        return stripped_value

    @model_validator(mode="after")
    def validate_values(self):
        if self.interval_start and (
            self.interval_start.minute not in (0, 30)
            or self.interval_start.second != 0
            or self.interval_start.microsecond != 0
        ):
            raise ValueError("interval_start must be aligned to a 30-minute boundary")
        if self.readings is not None:
            for meter_id, value in self.readings.items():
                if not isinstance(meter_id, str) or not meter_id.strip():
                    raise ValueError("reading meter IDs must not be blank")
                if value < 0:
                    raise ValueError("meter readings must be non-negative")
        return self


class MeterCaptureReadingResponse(BaseModel):
    """One 30-minute meter capture row returned by the API."""

    id: str
    site: MeteringSiteCode
    site_name: str
    interval_start: datetime
    interval_end: datetime
    readings: dict[str, Optional[float]]
    source: MeterEntryMode
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MeterCaptureListResponse(BaseModel):
    """Meter capture rows plus meter definitions for table rendering."""

    site: MeteringSiteCode
    site_name: str
    interval_minutes: int = 30
    meters: list[MeterResponse]
    records: list[MeterCaptureReadingResponse]
    next_cursor: Optional[str] = None
