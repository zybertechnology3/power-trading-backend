"""
Pydantic schemas for power system telemetry data
Used for request/response serialization with MongoDB time series
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


# ===== COMPONENT SCHEMAS =====

class UnitsLoad(BaseModel):
    """Generator unit load data in MW"""
    unit_1: float = Field(..., ge=0, description="Unit 1 load in MW")
    unit_2: float = Field(..., ge=0, description="Unit 2 load in MW")
    unit_3: float = Field(..., ge=0, description="Unit 3 load in MW")
    unit_4: float = Field(..., ge=0, description="Unit 4 load in MW")
    total: float = Field(..., ge=0, description="Total generation load in MW")


class TransmissionLines(BaseModel):
    """Transmission line flow data in MW"""
    line_c: float = Field(..., ge=0, description="Line C flow in MW")
    line_d: float = Field(..., ge=0, description="Line D flow in MW")
    line_a: float = Field(..., ge=0, description="Line A flow in MW")
    line_b: float = Field(..., ge=0, description="Line B flow in MW")
    total_export: float = Field(..., ge=0, description="Total LHPC export in MW")


# ===== REQUEST SCHEMAS =====

class TelemetryCreate(BaseModel):
    """Schema for creating telemetry data point"""
    timestamp: datetime = Field(..., description="Time of measurement")
    units_load: UnitsLoad
    transmission_lines: TransmissionLines
    busbar_voltage_kv: float = Field(..., gt=0, description="Busbar voltage in kV")


class TelemetryBulkImport(BaseModel):
    """Schema for bulk importing telemetry records"""
    records: list[TelemetryCreate] = Field(..., min_items=1, description="List of telemetry records to import")


# ===== RESPONSE SCHEMAS =====

class TelemetryResponse(TelemetryCreate):
    """Telemetry response with server fields"""
    id: Optional[str] = Field(None, alias="_id", description="MongoDB ObjectId")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class TelemetryListResponse(BaseModel):
    """Paginated list of telemetry records"""
    records: list[TelemetryResponse]
    total: int
    page: int
    page_size: int


class BulkImportResponse(BaseModel):
    """Response from bulk import operation"""
    imported: int = Field(..., description="Number of records successfully imported")
    failed: int = Field(default=0, description="Number of records that failed to import")
    errors: list[dict] = Field(default_factory=list, description="List of import errors if any")


class TelemetryStatsResponse(BaseModel):
    """Statistics for telemetry data"""
    total_records: int
    date_range_start: Optional[datetime] = None
    date_range_end: Optional[datetime] = None
    avg_total_generation_mw: float
    avg_total_transmission_mw: float
    avg_busbar_voltage_kv: float
    max_busbar_voltage_kv: float
    min_busbar_voltage_kv: float
