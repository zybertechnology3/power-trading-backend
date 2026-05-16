"""
Pydantic schemas for trade data validation
Used for request/response serialization with MongoDB
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


# ===== REQUEST SCHEMAS =====

class TradeCreate(BaseModel):
    """Schema for creating a new trade"""
    symbol: str = Field(..., min_length=1, max_length=50, description="Market symbol")
    position_type: str = Field(..., pattern="^(long|short)$", description="Position type: long or short")
    entry_price: float = Field(..., gt=0, description="Entry price")
    quantity: float = Field(..., gt=0, description="Quantity in MWh")
    entry_time: datetime = Field(..., description="Entry time")
    strategy: Optional[str] = None
    notes: Optional[str] = None


class TradeUpdate(BaseModel):
    """Schema for updating a trade (typically to close it)"""
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    status: Optional[str] = None
    notes: Optional[str] = None


# ===== RESPONSE SCHEMAS =====

class TradeResponse(TradeCreate):
    """Trade response with calculated and server fields"""
    id: Optional[str] = Field(None, alias="_id", description="MongoDB ObjectId")
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    status: str = Field(default="pending")
    profit_loss: Optional[float] = None
    profit_loss_percentage: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True


class TradeListResponse(BaseModel):
    """Paginated list of trades"""
    trades: list[TradeResponse]
    total: int
    page: int
    page_size: int


class TradeStatsResponse(BaseModel):
    """Trading statistics summary"""
    total_trades: int = Field(..., description="Total number of trades")
    closed_trades: int = Field(..., description="Number of closed trades")
    open_trades: int = Field(..., description="Number of open trades")
    win_rate: float = Field(..., description="Win rate percentage")
    total_profit_loss: float = Field(..., description="Total P&L")
    avg_profit_loss: float = Field(..., description="Average P&L per trade")

