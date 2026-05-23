"""
Pydantic schemas for power outage requests.
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


OutageRequestStatus = Literal["draft", "submitted", "approved", "rejected", "completed"]
OutageReason = Literal["PM", "CM", "SF", "CO"]


class GeneratingUnitResponse(BaseModel):
    """Generating unit option used by outage requests."""

    unit_code: str
    unit_name: str


class OutageItemInput(BaseModel):
    """Create or replace one outage request line."""

    unit_code: str = Field(..., min_length=1)
    reason: OutageReason
    start_at: datetime
    restore_at: datetime
    expected_mw_reduction: Optional[float] = Field(None, ge=0)
    description: str = Field(..., min_length=1)

    @field_validator("unit_code", "description")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value

    @model_validator(mode="after")
    def validate_times(self):
        if self.restore_at <= self.start_at:
            raise ValueError("restore_at must be after start_at")
        return self


class OutageRequestCreate(BaseModel):
    """Create an outage request with one or more items."""

    document_no: str = Field(..., min_length=1)
    revision_no: str = Field(..., min_length=1)
    implementation_date: date
    document_owner: str = Field(..., min_length=1)
    approver: str = Field(..., min_length=1)
    date_approved: Optional[date] = None
    status: OutageRequestStatus = "draft"
    created_by: str = Field(..., min_length=1)
    items: list[OutageItemInput] = Field(..., min_length=1)

    @field_validator("document_no", "revision_no", "document_owner", "approver", "created_by")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class OutageRequestReplace(BaseModel):
    """Replace an outage request header and items."""

    document_no: str = Field(..., min_length=1)
    revision_no: str = Field(..., min_length=1)
    implementation_date: date
    document_owner: str = Field(..., min_length=1)
    approver: str = Field(..., min_length=1)
    date_approved: Optional[date] = None
    status: OutageRequestStatus
    created_by: str = Field(..., min_length=1)
    items: list[OutageItemInput] = Field(..., min_length=1)

    @field_validator("document_no", "revision_no", "document_owner", "approver", "created_by")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("field must not be blank")
        return stripped_value


class OutageStatusUpdate(BaseModel):
    """Workflow status transition request."""

    status: OutageRequestStatus


class OutageItemResponse(BaseModel):
    """One outage request line returned by the API."""

    outage_no: int
    unit_code: str
    unit_name: str
    reason: OutageReason
    start_at: datetime
    restore_at: datetime
    duration_hrs: float
    expected_mw_reduction: Optional[float] = None
    description: str


class OutageRequestSummary(BaseModel):
    """Per-request outage summary."""

    total_duration_hrs: float
    total_expected_mw_reduction: float


class OutageRequestResponse(BaseModel):
    """Outage request returned by the API."""

    id: str
    document_no: str
    revision_no: str
    implementation_date: date
    document_owner: str
    approver: str
    date_approved: Optional[date] = None
    status: OutageRequestStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    items: list[OutageItemResponse]
    summary: OutageRequestSummary


class OutageRequestListResponse(BaseModel):
    """Outage request list response."""

    records: list[OutageRequestResponse]
