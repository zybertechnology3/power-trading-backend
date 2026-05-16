"""
Market API Endpoints
Handles all market and price candle operations
"""

from fastapi import APIRouter, HTTPException, Query
from datetime import datetime
from typing import Optional
from bson.objectid import ObjectId
from app.db.database import get_db
from app.schemas.market import (
    PowerMarketCreate,
    PowerMarketResponse,
    PowerMarketUpdate,
    PowerPriceCandleCreate,
    PowerPriceCandleResponse,
    PowerPriceCandleList,
)

router = APIRouter(prefix="/markets", tags=["markets"])


# ===== MARKET ENDPOINTS =====

@router.get("", response_model=list[PowerMarketResponse])
def list_markets(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    status: str = Query("active"),
):
    """
    List all power markets
    Query: skip (pagination), limit (page size), status (filter)
    """
    db = get_db()
    markets_collection = db["power_markets"]
    
    # Build query filter
    query_filter = {}
    if status:
        query_filter["status"] = status
    
    # Execute query with pagination
    markets = list(
        markets_collection.find(query_filter)
        .skip(skip)
        .limit(limit)
    )
    
    # Convert ObjectId to string for JSON serialization
    for market in markets:
        market["_id"] = str(market["_id"])
    
    return markets


@router.post("", response_model=PowerMarketResponse)
def create_market(market: PowerMarketCreate):
    """
    Create a new power market
    Symbol must be unique
    """
    db = get_db()
    markets_collection = db["power_markets"]
    
    # Check if market with this symbol already exists
    existing = markets_collection.find_one({"symbol": market.symbol})
    if existing:
        raise HTTPException(status_code=400, detail="Market with this symbol already exists")
    
    # Create market document
    market_doc = {
        **market.model_dump(),
        "status": "active",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    # Insert into database
    result = markets_collection.insert_one(market_doc)
    market_doc["_id"] = str(result.inserted_id)
    
    return market_doc


@router.get("/{symbol}", response_model=PowerMarketResponse)
def get_market(symbol: str):
    """Get market details by symbol"""
    db = get_db()
    markets_collection = db["power_markets"]
    
    market = markets_collection.find_one({"symbol": symbol})
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    market["_id"] = str(market["_id"])
    return market


@router.put("/{symbol}", response_model=PowerMarketResponse)
def update_market(symbol: str, market_update: PowerMarketUpdate):
    """Update market configuration"""
    db = get_db()
    markets_collection = db["power_markets"]
    
    # Find existing market
    market = markets_collection.find_one({"symbol": symbol})
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Prepare update data (only include non-null fields)
    update_data = market_update.model_dump(exclude_unset=True)
    update_data["updated_at"] = datetime.utcnow()
    
    # Update market in database
    result = markets_collection.update_one(
        {"symbol": symbol},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Return updated market
    updated_market = markets_collection.find_one({"symbol": symbol})
    updated_market["_id"] = str(updated_market["_id"])
    return updated_market


# ===== PRICE CANDLE ENDPOINTS (TIME SERIES) =====

@router.post("/{symbol}/candles", response_model=PowerPriceCandleResponse)
def create_price_candle(symbol: str, candle: PowerPriceCandleCreate):
    """
    Create a new price candle for a market
    MongoDB stores this in the time series collection automatically
    """
    db = get_db()
    markets_collection = db["power_markets"]
    candles_collection = db["power_price_candles"]
    
    # Verify market exists
    market = markets_collection.find_one({"symbol": symbol})
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Prepare time series document
    candle_doc = {
        "timestamp": candle.timestamp,
        "metadata": {
            "symbol": symbol,
            "data_source": candle.data_source,
            "is_verified": False
        },
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "weighted_avg_price": candle.weighted_avg_price,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    # Insert into time series collection
    result = candles_collection.insert_one(candle_doc)
    candle_doc["_id"] = str(result.inserted_id)
    
    return candle_doc


@router.get("/{symbol}/candles", response_model=PowerPriceCandleList)
def get_price_candles(
    symbol: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    verified_only: bool = Query(False),
):
    """
    Get price candles for a market with optional filtering
    Can filter by time range and verification status
    """
    db = get_db()
    markets_collection = db["power_markets"]
    candles_collection = db["power_price_candles"]
    
    # Verify market exists
    market = markets_collection.find_one({"symbol": symbol})
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    
    # Build query filter
    query_filter = {"metadata.symbol": symbol}
    
    # Add time range filtering
    if start_time or end_time:
        query_filter["timestamp"] = {}
        if start_time:
            query_filter["timestamp"]["$gte"] = start_time
        if end_time:
            query_filter["timestamp"]["$lte"] = end_time
    
    # Add verification filter
    if verified_only:
        query_filter["metadata.is_verified"] = True
    
    # Execute query with pagination
    candles = list(
        candles_collection.find(query_filter)
        .sort("timestamp", -1)  # Most recent first
        .skip(skip)
        .limit(limit)
    )
    
    # Convert ObjectIds to strings
    for candle in candles:
        candle["_id"] = str(candle["_id"])
    
    return PowerPriceCandleList(
        symbol=symbol,
        candles=candles,
        count=len(candles)
    )


@router.get("/{symbol}/candles/{timestamp}", response_model=PowerPriceCandleResponse)
def get_price_candle(symbol: str, timestamp: datetime):
    """Get a specific price candle by timestamp"""
    db = get_db()
    candles_collection = db["power_price_candles"]
    
    candle = candles_collection.find_one({
        "metadata.symbol": symbol,
        "timestamp": timestamp
    })
    
    if not candle:
        raise HTTPException(status_code=404, detail="Price candle not found")
    
    candle["_id"] = str(candle["_id"])
    return candle

