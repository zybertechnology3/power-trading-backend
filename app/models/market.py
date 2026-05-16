"""
Market Data Models Documentation
These are MongoDB document structures (not ORM models)

PowerMarket Collection:
  _id: ObjectId (auto-generated)
  symbol: str (unique, indexed) - e.g., "PJM_HB_CHICAGO"
  name: str - Market display name
  region: str - Geographic region
  market_type: str - Type like "Day-ahead" or "Real-time"
  currency: str - 3-letter currency code
  status: str - "active" or "inactive"
  created_at: datetime
  updated_at: datetime

PowerPriceCandles Time Series Collection:
  timestamp: datetime (indexed) - Time of the candle
  metadata:
    symbol: str (indexed) - Market symbol
    data_source: str - Source of the data
    is_verified: bool - Data verification status
  open: float - Opening price
  high: float - Highest price in period
  low: float - Lowest price in period
  close: float - Closing price
  volume: float - MWh traded
  weighted_avg_price: float (optional)
"""

# No ORM models needed - MongoDB works with plain documents
# Pydantic schemas in app/schemas/market.py handle validation

