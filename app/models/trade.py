"""
Trade Data Model Documentation
MongoDB document structure (not ORM)

Trades Collection:
  _id: ObjectId (auto-generated)
  symbol: str (indexed) - Market symbol for the trade
  position_type: str - "long" or "short"
  entry_price: float - Price at entry
  exit_price: float (nullable) - Price at exit
  quantity: float - MWh quantity
  status: str - "pending", "open", "closed", or "cancelled"
  entry_time: datetime - When position was opened
  exit_time: datetime (nullable) - When position was closed
  profit_loss: float (nullable) - Calculated P&L amount
  profit_loss_percentage: float (nullable) - Calculated P&L %
  strategy: str (optional) - Trading strategy name
  notes: str (optional) - Additional notes
  created_at: datetime
  updated_at: datetime
"""

# No ORM models needed - MongoDB works with plain documents
# Pydantic schemas in app/schemas/trade.py handle validation

