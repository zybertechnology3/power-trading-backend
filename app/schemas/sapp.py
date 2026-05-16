"""
Schemas for SAPP MTP constrained area results.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class SappConstrainedAreaResultCreate(BaseModel):
    """Hourly constrained area result extracted from a SAPP MTP DAM document."""

    timestamp: datetime = Field(..., description="Delivery timestamp for this hourly result")
    delivery_date: date = Field(..., description="SAPP delivery date")
    hour: Optional[int] = Field(None, ge=1, le=24, description="Delivery hour, 1-24")
    hour_label: Optional[str] = Field(None, description="Original hour label from the document")
    area_purchase_mw: Optional[float] = Field(None, description="Area purchase in MW")
    area_sales_mw: Optional[float] = Field(None, description="Area sales in MW")
    area_price_usd_per_mwh: Optional[float] = Field(None, description="Area price in USD/MWh")
    data_source: str = Field("SAPP_MTP_DAM_CONSTRAINED_AREA_RESULTS")
    source_file: Optional[str] = None
    frequency: Optional[str] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    sample_count: Optional[int] = None


class SappConstrainedAreaResultResponse(SappConstrainedAreaResultCreate):
    """SAPP constrained area result with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class SappConstrainedAreaResultList(BaseModel):
    """Paginated list of SAPP constrained area results."""

    records: list[SappConstrainedAreaResultResponse]
    total: int
    page: int
    page_size: int


class SappScrapeResponse(BaseModel):
    """Response returned after running the scraper."""

    job: str = "constrained_area_results"
    delivery_date: date
    imported: int
    updated: int
    source_file: str


class SappScrapeRangeResult(BaseModel):
    """Per-date result returned by a range scrape."""

    job: str = "constrained_area_results"
    delivery_date: date
    status: str
    imported: int = 0
    updated: int = 0
    source_file: Optional[str] = None
    error: Optional[str] = None


class SappScrapeRangeResponse(BaseModel):
    """Response returned after running the scraper for a date range."""

    job: str = "constrained_area_results"
    start_date: date
    end_date: date
    requested_dates: int
    successful_dates: int
    failed_dates: int
    imported: int
    updated: int
    results: list[SappScrapeRangeResult]
