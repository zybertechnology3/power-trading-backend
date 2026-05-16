"""
Trading API Endpoints
Handles all trade operations, P&L calculations, and statistics
"""

from fastapi import APIRouter, HTTPException, Query
from datetime import datetime
from typing import Optional
from bson.objectid import ObjectId
from app.db.database import get_db
from app.schemas.trade import (
    TradeCreate,
    TradeUpdate,
    TradeResponse,
    TradeListResponse,
    TradeStatsResponse,
)

router = APIRouter(prefix="/trades", tags=["trades"])


# ===== TRADE LISTING AND RETRIEVAL =====

@router.get("", response_model=TradeListResponse)
def list_trades(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    symbol: str = Query(None),
    status: str = Query(None),
    position_type: str = Query(None),
):
    """
    List trades with optional filtering
    Can filter by symbol, status, or position type
    """
    db = get_db()
    trades_collection = db["trades"]
    
    # Build query filter
    query_filter = {}
    if symbol:
        query_filter["symbol"] = symbol
    if status:
        query_filter["status"] = status
    if position_type:
        query_filter["position_type"] = position_type
    
    # Get total count before pagination
    total = trades_collection.count_documents(query_filter)
    
    # Execute query with pagination (most recent first)
    trades = list(
        trades_collection.find(query_filter)
        .sort("entry_time", -1)
        .skip(skip)
        .limit(limit)
    )
    
    # Convert ObjectIds to strings
    for trade in trades:
        trade["_id"] = str(trade["_id"])
    
    # Calculate page number
    page = (skip // limit) + 1 if limit > 0 else 1
    
    return TradeListResponse(
        trades=trades,
        total=total,
        page=page,
        page_size=limit
    )


@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade(trade_id: str):
    """Get a specific trade by ID"""
    db = get_db()
    trades_collection = db["trades"]
    
    # Convert string ID to ObjectId
    try:
        trade_oid = ObjectId(trade_id)
    except:
        raise HTTPException(status_code=400, detail="Invalid trade ID format")
    
    trade = trades_collection.find_one({"_id": trade_oid})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    
    trade["_id"] = str(trade["_id"])
    return trade


# ===== TRADE CREATION AND MODIFICATION =====

@router.post("", response_model=TradeResponse)
def create_trade(trade: TradeCreate):
    """
    Create a new trade
    Initial status is 'pending'
    """
    db = get_db()
    trades_collection = db["trades"]
    
    # Create trade document
    trade_doc = {
        **trade.model_dump(),
        "status": "pending",
        "exit_price": None,
        "exit_time": None,
        "profit_loss": None,
        "profit_loss_percentage": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    # Insert into database
    result = trades_collection.insert_one(trade_doc)
    trade_doc["_id"] = str(result.inserted_id)
    
    return trade_doc


@router.put("/{trade_id}", response_model=TradeResponse)
def update_trade(trade_id: str, trade_update: TradeUpdate):
    """
    Update a trade (typically to close position)
    Automatically calculates P&L if exit_price is provided
    """
    db = get_db()
    trades_collection = db["trades"]
    
    # Convert string ID to ObjectId
    try:
        trade_oid = ObjectId(trade_id)
    except:
        raise HTTPException(status_code=400, detail="Invalid trade ID format")
    
    # Get existing trade
    trade = trades_collection.find_one({"_id": trade_oid})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    
    # Prepare update data
    update_data = trade_update.model_dump(exclude_unset=True)
    update_data["updated_at"] = datetime.utcnow()
    
    # Calculate P&L if exit_price is provided
    if "exit_price" in update_data and update_data["exit_price"]:
        exit_price = update_data["exit_price"]
        
        # Calculate profit/loss based on position type
        if trade["position_type"] == "long":
            # For long: profit = (exit - entry) * quantity
            pnl = (exit_price - trade["entry_price"]) * trade["quantity"]
            pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
        else:  # short
            # For short: profit = (entry - exit) * quantity
            pnl = (trade["entry_price"] - exit_price) * trade["quantity"]
            pnl_pct = (trade["entry_price"] - exit_price) / trade["entry_price"] * 100
        
        update_data["profit_loss"] = pnl
        update_data["profit_loss_percentage"] = pnl_pct
        
        # Auto-set status to closed if not explicitly set
        if "status" not in update_data:
            update_data["status"] = "closed"
    
    # Update trade in database
    result = trades_collection.update_one(
        {"_id": trade_oid},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Trade not found")
    
    # Return updated trade
    updated_trade = trades_collection.find_one({"_id": trade_oid})
    updated_trade["_id"] = str(updated_trade["_id"])
    return updated_trade


@router.delete("/{trade_id}")
def delete_trade(trade_id: str):
    """Delete a trade"""
    db = get_db()
    trades_collection = db["trades"]
    
    # Convert string ID to ObjectId
    try:
        trade_oid = ObjectId(trade_id)
    except:
        raise HTTPException(status_code=400, detail="Invalid trade ID format")
    
    result = trades_collection.delete_one({"_id": trade_oid})
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Trade not found")
    
    return {"message": "Trade deleted successfully"}


# ===== STATISTICS AND ANALYTICS =====

@router.get("/{symbol}/stats", response_model=TradeStatsResponse)
def get_trade_stats(symbol: str):
    """
    Get trading statistics for a symbol
    Includes win rate, total P&L, and other metrics
    """
    db = get_db()
    trades_collection = db["trades"]
    
    # Query for all trades of this symbol
    all_trades = list(trades_collection.find({"symbol": symbol}))
    
    total_trades = len(all_trades)
    
    # Count trades by status
    closed_trades = [t for t in all_trades if t.get("status") == "closed"]
    open_trades = [t for t in all_trades if t.get("status") == "open"]
    
    closed_count = len(closed_trades)
    
    # Calculate win rate (winning trades / closed trades)
    winning_trades = [t for t in closed_trades if t.get("profit_loss", 0) > 0]
    win_rate = (len(winning_trades) / closed_count * 100) if closed_count > 0 else 0.0
    
    # Calculate total and average P&L (only for closed trades)
    total_pnl = sum(t.get("profit_loss", 0) for t in closed_trades)
    avg_pnl = total_pnl / closed_count if closed_count > 0 else 0.0
    
    return TradeStatsResponse(
        total_trades=total_trades,
        closed_trades=closed_count,
        open_trades=len(open_trades),
        win_rate=win_rate,
        total_profit_loss=total_pnl,
        avg_profit_loss=avg_pnl
    )
