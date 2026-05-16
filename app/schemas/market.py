"""
Pydantic schemas for market data validation
Used for request/response serialization with MongoDB
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


# ===== REQUEST SCHEMAS =====
# Used for incoming POST/PUT requests

class PowerMarketCreate(BaseModel):
    """Schema for creating a new power market"""
    symbol: str = Field(..., min_length=1, max_length=50, description="Market symbol (e.g., PJM_HB_CHICAGO)")
    name: str = Field(..., min_length=1, max_length=255, description="Market display name")
    region: Optional[str] = Field(None, max_length=100, description="Geographic region")
    market_type: str = Field(..., max_length=50, description="Type: Day-ahead, Real-time, etc.")
    currency: str = Field("USD", max_length=3, description="3-letter currency code")


class PowerMarketUpdate(BaseModel):
    """Schema for updating market details"""
    name: Optional[str] = None
    region: Optional[str] = None
    market_type: Optional[str] = None
    status: Optional[str] = None


# ===== RESPONSE SCHEMAS =====
# Used for API responses

class PowerMarketResponse(PowerMarketCreate):
    """Market response with server-generated fields"""
    id: Optional[str] = Field(None, alias="_id", description="MongoDB ObjectId")
    status: str = Field(default="active")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


# ===== TIME SERIES SCHEMAS =====

class PowerPriceCandleCreate(BaseModel):
    """Schema for creating a price candle (OHLC data)"""
    symbol: str = Field(..., min_length=1, max_length=50)
    timestamp: datetime = Field(..., description="Time of the candle")
    open: float = Field(..., description="Opening price")
    high: float = Field(..., description="Highest price in period")
    low: float = Field(..., description="Lowest price in period")
    close: float = Field(..., description="Closing price")
    volume: float = Field(default=0.0, ge=0, description="MWh traded")
    weighted_avg_price: Optional[float] = None
    data_source: Optional[str] = None


class PowerPriceCandleResponse(PowerPriceCandleCreate):
    """Price candle response with server fields"""
    id: Optional[str] = Field(None, alias="_id")
    is_verified: bool = Field(default=False)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class PowerPriceCandleList(BaseModel):
    """Paginated list of price candles"""
    symbol: str
    candles: list[PowerPriceCandleResponse]
    count: int

