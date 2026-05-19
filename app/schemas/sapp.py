"""
Schemas for SAPP MTP constrained area results.
"""

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


BidStatus = Literal["draft", "submitted"]
SappBidMarket = Literal["dam", "fpm_w", "fpm_m"]
SappBidProduct = Literal["off_peak", "peak", "standard"]


class SappBidQuantity(BaseModel):
    """Sparse bid cell value for one DAM hour or FPM product and one price column."""

    hour: Optional[int] = Field(None, ge=1, le=24, description="DAM delivery hour, 1-24")
    product: Optional[SappBidProduct] = Field(
        None,
        description="FPM period product: off_peak, peak, or standard",
    )
    price_usd_per_mwh: float = Field(..., ge=0, description="Bid price column")
    energy_mwh: float = Field(..., ge=0, description="Energy to buy at this hour and price")


class SappBidBase(BaseModel):
    """Editable bid construction grid stored as sparse quantities."""

    market: SappBidMarket = Field("dam", description="Bid market: dam, fpm_w, or fpm_m")
    delivery_date: Optional[date] = Field(
        None,
        description="DAM delivery date this bid applies to",
    )
    week_start_date: Optional[date] = Field(
        None,
        description="FPM-W week start date. The trade applies every day that week.",
    )
    month_start_date: Optional[date] = Field(
        None,
        description="FPM-M month start date. The trade applies every day that month.",
    )
    price_columns: list[float] = Field(
        ...,
        min_length=1,
        description="Ordered price columns shown in the bid grid",
    )
    quantities: list[SappBidQuantity] = Field(
        default_factory=list,
        description="Non-empty grid cells. Zero-value cells can be omitted.",
    )
    template_id: Optional[str] = Field(None, description="Template used to initialize the bid")
    notes: Optional[str] = None

    @field_validator("price_columns")
    @classmethod
    def validate_price_columns(cls, prices: list[float]) -> list[float]:
        if len(set(prices)) != len(prices):
            raise ValueError("price_columns must be unique")
        return prices

    @model_validator(mode="after")
    def validate_quantities(self):
        valid_prices = set(self.price_columns)
        seen_cells = set()
        for quantity in self.quantities:
            if quantity.price_usd_per_mwh not in valid_prices:
                raise ValueError(
                    "quantity price_usd_per_mwh must exist in price_columns"
                )
            if self.market == "dam":
                if quantity.hour is None or quantity.product is not None:
                    raise ValueError("DAM quantities must use hour and must not use product")
                cell_key = (quantity.hour, quantity.price_usd_per_mwh)
            else:
                if quantity.product is None or quantity.hour is not None:
                    raise ValueError(
                        "FPM quantities must use product and must not use hour"
                    )
                cell_key = (quantity.product, quantity.price_usd_per_mwh)
            if cell_key in seen_cells:
                raise ValueError("quantities must not contain duplicate grid cells")
            seen_cells.add(cell_key)

        if self.market == "dam":
            if self.delivery_date is None:
                raise ValueError("delivery_date is required for DAM bids")
            if self.week_start_date is not None or self.month_start_date is not None:
                raise ValueError("DAM bids must not include week_start_date or month_start_date")
        elif self.market == "fpm_w":
            if self.week_start_date is None:
                raise ValueError("week_start_date is required for FPM-W bids")
            if self.week_start_date.weekday() != 0:
                raise ValueError("week_start_date must be a Monday")
            if self.delivery_date is not None or self.month_start_date is not None:
                raise ValueError("FPM-W bids must not include delivery_date or month_start_date")
        elif self.market == "fpm_m":
            if self.month_start_date is None:
                raise ValueError("month_start_date is required for FPM-M bids")
            if self.month_start_date.day != 1:
                raise ValueError("month_start_date must be the first day of the month")
            if self.delivery_date is not None or self.week_start_date is not None:
                raise ValueError("FPM-M bids must not include delivery_date or week_start_date")
        return self


class SappBidCreate(SappBidBase):
    """Create a draft bid."""


class SappBidUpdate(BaseModel):
    """Update fields for a draft bid."""

    market: Optional[SappBidMarket] = None
    delivery_date: Optional[date] = None
    week_start_date: Optional[date] = None
    month_start_date: Optional[date] = None
    price_columns: Optional[list[float]] = Field(None, min_length=1)
    quantities: Optional[list[SappBidQuantity]] = None
    template_id: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("price_columns")
    @classmethod
    def validate_price_columns(cls, prices: Optional[list[float]]) -> Optional[list[float]]:
        if prices is not None and len(set(prices)) != len(prices):
            raise ValueError("price_columns must be unique")
        return prices


class SappBidResponse(SappBidBase):
    """Bid construction record with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    status: BidStatus
    period_start_date: date
    period_end_date: date
    delivery_days: int = Field(1, ge=1)
    period_hour_counts: dict[SappBidProduct, int] = Field(default_factory=dict)
    period_energy_mwh: dict[SappBidProduct, float] = Field(default_factory=dict)
    daily_energy_mwh: float = 0
    total_energy_mwh: float = 0
    weighted_average_price_usd_per_mwh: Optional[float] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class SappBidList(BaseModel):
    """Paginated bid history."""

    records: list[SappBidResponse]
    total: int
    page: int
    page_size: int


class SappBidTemplateBase(BaseModel):
    """Reusable bid grid template."""

    name: str = Field(..., min_length=1, max_length=100)
    market: SappBidMarket = Field("dam", description="Template market: dam, fpm_w, or fpm_m")
    price_columns: list[float] = Field(..., min_length=1)
    default_quantities: list[SappBidQuantity] = Field(default_factory=list)
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("name must not be blank")
        return normalized_name

    @field_validator("price_columns")
    @classmethod
    def validate_price_columns(cls, prices: list[float]) -> list[float]:
        if len(set(prices)) != len(prices):
            raise ValueError("price_columns must be unique")
        return prices

    @model_validator(mode="after")
    def validate_default_quantities(self):
        valid_prices = set(self.price_columns)
        seen_cells = set()
        for quantity in self.default_quantities:
            if quantity.price_usd_per_mwh not in valid_prices:
                raise ValueError(
                    "default quantity price_usd_per_mwh must exist in price_columns"
                )
            if self.market == "dam":
                if quantity.hour is None or quantity.product is not None:
                    raise ValueError(
                        "DAM default_quantities must use hour and must not use product"
                    )
                cell_key = (quantity.hour, quantity.price_usd_per_mwh)
            else:
                if quantity.product is None or quantity.hour is not None:
                    raise ValueError(
                        "FPM default_quantities must use product and must not use hour"
                    )
                cell_key = (quantity.product, quantity.price_usd_per_mwh)
            if cell_key in seen_cells:
                raise ValueError(
                    "default_quantities must not contain duplicate grid cells"
                )
            seen_cells.add(cell_key)
        return self


class SappBidTemplateCreate(SappBidTemplateBase):
    """Create a bid template."""


class SappBidTemplateUpdate(BaseModel):
    """Update fields for a bid template."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    market: Optional[SappBidMarket] = None
    price_columns: Optional[list[float]] = Field(None, min_length=1)
    default_quantities: Optional[list[SappBidQuantity]] = None
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: Optional[str]) -> Optional[str]:
        if name is None:
            return None
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("name must not be blank")
        return normalized_name

    @field_validator("price_columns")
    @classmethod
    def validate_price_columns(cls, prices: Optional[list[float]]) -> Optional[list[float]]:
        if prices is not None and len(set(prices)) != len(prices):
            raise ValueError("price_columns must be unique")
        return prices


class SappBidTemplateResponse(SappBidTemplateBase):
    """Bid template with server-generated fields."""

    id: Optional[str] = Field(None, alias="_id")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class SappBidTemplateList(BaseModel):
    """Paginated list of bid templates."""

    records: list[SappBidTemplateResponse]
    total: int
    page: int
    page_size: int


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
