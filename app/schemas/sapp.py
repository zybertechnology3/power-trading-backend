"""
Schemas for SAPP MTP constrained area results.
"""

from datetime import date, datetime
from typing import Any, Optional

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


class SappParticipantPortfolioResultCreate(BaseModel):
    """Hourly participant portfolio result extracted from a SAPP MTP DAM document."""

    timestamp: datetime = Field(..., description="Delivery timestamp for this hourly result")
    delivery_date: date = Field(..., description="SAPP delivery date")
    hour: Optional[int] = Field(None, ge=1, le=24, description="Delivery hour, 1-24")
    hour_label: Optional[str] = Field(None, description="Original hour label from the document")
    participant_total_area_schedule_mwh: Optional[float] = Field(
        None,
        description="Participant total area schedule in MWh",
    )
    area_price_usd_per_mwh: Optional[float] = Field(None, description="Area price in USD/MWh")
    unconstrained_market_price_usd_per_mwh: Optional[float] = Field(
        None,
        description="Unconstrained market price in USD/MWh",
    )
    total_dam_turnover_mwh: Optional[float] = Field(
        None,
        description="Total DAM turnover in MWh",
    )
    data_source: str = Field("SAPP_MTP_DAM_PARTICIPANT_PORTFOLIO_RESULTS")
    source_file: Optional[str] = None


class SappParticipantPortfolioResultResponse(SappParticipantPortfolioResultCreate):
    """SAPP participant portfolio result with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class SappParticipantPortfolioResultList(BaseModel):
    """Paginated list of SAPP participant portfolio results."""

    records: list[SappParticipantPortfolioResultResponse]
    total: int
    page: int
    page_size: int


class SappTradingInvoiceCreditNoteCreate(BaseModel):
    """Daily trading invoice / credit note extracted from a SAPP MTP document."""

    timestamp: datetime = Field(..., description="Delivery timestamp for this invoice")
    delivery_date: date = Field(..., description="SAPP delivery date")
    currency: Optional[str] = None
    market_turnover_usd: Optional[float] = None
    confirmed_trade_type: Optional[str] = None
    fpm_m: dict[str, Any] = Field(default_factory=dict)
    fpm_w: dict[str, Any] = Field(default_factory=dict)
    dam: dict[str, Any] = Field(default_factory=dict)
    idm: dict[str, Any] = Field(default_factory=dict)
    balancing_market: dict[str, Any] = Field(default_factory=dict)
    total_purchases: dict[str, Any] = Field(default_factory=dict)
    total_sales: dict[str, Any] = Field(default_factory=dict)
    net_amount_traded_usd: Optional[float] = None
    admin_fee_mwh: Optional[float] = None
    admin_fee_usd: Optional[float] = None
    wheeling_fee_usd: Optional[float] = None
    losses_fee_usd: Optional[float] = None
    total_fees_usd: Optional[float] = None
    total_amount_due_usd: Optional[float] = None
    gross_total_mwh: Optional[float] = None
    gross_total_amount_usd: Optional[float] = None
    gross_average_price_usd_per_mwh: Optional[float] = None
    total_expenditure_usd: Optional[float] = None
    sapp_net_turnover_usd: Optional[float] = None
    data_source: str = Field("SAPP_MTP_TRADING_INVOICE_CREDIT_NOTE")
    source_file: Optional[str] = None


class SappTradingInvoiceCreditNoteResponse(SappTradingInvoiceCreditNoteCreate):
    """SAPP trading invoice / credit note with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class SappTradingInvoiceCreditNoteList(BaseModel):
    """Paginated list of SAPP trading invoice / credit note records."""

    records: list[SappTradingInvoiceCreditNoteResponse]
    total: int
    page: int
    page_size: int


class SappTradingInvoiceHourlyDetailCreate(BaseModel):
    """Hourly trading invoice detail extracted from a SAPP MTP document."""

    timestamp: datetime = Field(..., description="Delivery timestamp for this hourly detail")
    delivery_date: date = Field(..., description="SAPP delivery date")
    market: str = Field(..., description="Trading section, such as fpm_w or dam")
    hour: Optional[int] = Field(None, ge=1, le=24, description="Delivery hour, 1-24")
    hour_label: Optional[str] = Field(None, description="Original hour label from the document")
    price_usd_per_mwh: Optional[float] = None
    traded_purchases_mwh: Optional[float] = None
    traded_sales_mwh: Optional[float] = None
    purchase_turnover_usd: Optional[float] = None
    sale_turnover_usd: Optional[float] = None
    admin_fees_usd: Optional[float] = None
    wheeling_cost_usd: Optional[float] = None
    data_source: str = Field("SAPP_MTP_TRADING_INVOICE_HOURLY_DETAIL")
    source_file: Optional[str] = None


class SappTradingInvoiceHourlyDetailResponse(SappTradingInvoiceHourlyDetailCreate):
    """SAPP trading invoice hourly detail with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class SappTradingInvoiceHourlyDetailList(BaseModel):
    """Paginated list of SAPP trading invoice hourly detail records."""

    records: list[SappTradingInvoiceHourlyDetailResponse]
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
