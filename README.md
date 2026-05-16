# Power Trading Backend

A FastAPI-based backend for power trading applications featuring **hybrid MongoDB architecture** with both relational and time series data.

## 🎯 Features

- **MongoDB Integration**: Simple and flexible NoSQL database
- **Native Time Series Support**: MongoDB time series collections for 1-hour OHLC price candles
- **Business Data Storage**: Markets, trades, and configuration in standard collections
- **Automatic P&L Calculation**: Closed trades automatically calculate profit/loss
- **Trading Statistics**: Win rates, total returns, and performance metrics
- **RESTful API**: Complete REST API with OpenAPI/Swagger documentation
- **CORS Support**: Ready for frontend integration

## 📁 Project Structure

```
power-trading-backend/
├── app/
│   ├── api/                     # API route handlers
│   │   ├── health.py            # Health check endpoints
│   │   ├── markets.py           # Market and price candle endpoints
│   │   └── trades.py            # Trading endpoints
│   │
│   ├── core/
│   │   └── config.py            # Application settings from .env
│   │
│   ├── db/
│   │   └── database.py          # MongoDB connection and setup
│   │
│   ├── models/                  # MongoDB collection documentation
│   │   ├── market.py            # Market and candle collection structure
│   │   └── trade.py             # Trade collection structure
│   │
│   ├── schemas/                 # Pydantic validation schemas
│   │   ├── market.py            # Market request/response schemas
│   │   └── trade.py             # Trade request/response schemas
│   │
│   ├── __init__.py
│   └── main.py                  # FastAPI application entry point
│
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variables template
├── run.bat                      # Windows startup script
├── run.sh                       # Linux/macOS startup script
└── README.md                    # This file
```

## 🚀 Quick Start

### 1. Prerequisites

- Python 3.10+
- MongoDB 5.0+ ([Download](https://www.mongodb.com/try/download/community))
- MongoDB Atlas (optional cloud alternative)

### 2. Installation

**Clone/Setup:**

```bash
cd power-trading-backend
```

**Create virtual environment:**

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# or
source venv/bin/activate       # macOS/Linux
```

**Install dependencies:**

```bash
pip install -r requirements.txt
```

**Configure environment:**

```bash
cp .env.example .env
```

Edit `.env` with your MongoDB connection:

```
MONGODB_URL=mongodb://localhost:27017
DATABASE_NAME=power_trading
DEBUG=True
```

### 3. MongoDB Setup

**Option A: Local MongoDB**

```bash
# Start MongoDB service (Windows)
net start MongoDB

# Or start manually (macOS/Linux)
mongod --dbpath /path/to/data
```

**Option B: MongoDB Atlas (Cloud)**

1. Create account at [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
2. Create a cluster and get connection string
3. Update `MONGODB_URL` in `.env`

### 4. Run Application

**Windows:**

```bash
run.bat
```

**macOS/Linux:**

```bash
./run.sh
``# Health Endpoints
```

GET /health - API status
GET /health/db - MongoDB connection status

```

### Market Endpoints
```

GET /markets - List all markets
POST /markets - Create market
GET /markets/{symbol} - Get market details
PUT /markets/{symbol} - Update market

POST /markets/{symbol}/candles - Add price candle
GET /markets/{symbol}/candles - Get candles (time range filter)
GET /markets/{symbol}/candles/{timestamp} - Get specific candle

```

### Trading Endpoints
```

GET /trades - List trades (filterable)
POST /trades - Create trade
GET /trades/{trade_id} - Get trade details
PUT /trades/{trade_id} - Update/close trade
DELETE /trades/{trade_id} - Delete trade
GET /trades/{symbol}/stats - Get trading statistics

````

## 💾 MongoDB Collections

### `power_markets` Collection
**Purpose**: Business data for power markets

**Document Structure:**
```json
{
  "_id": ObjectId,
  "symbol": "PJM_HB_CHICAGO",      // Unique market identifier
  "name": "PJM Hourly Based Chicago",
  "region": "PJM",
  "market_type": "Day-ahead",
  "currency": "USD",
  "status": "active",
  "created_at": ISODate("2024-01-15T10:00:00Z"),
  "updated_at": ISODate("2024-01-15T10:00:00Z")
}
````

**Indexes:**

- `symbol` (unique)
- `status`
- `region`

### `power_price_candles` Collection (Time Series)

**Purpose**: OHLC price data at 1-hour granularity

MongoDB time series collections automatically handle:

- Data compression
- Efficient time-based queries
- A📋 Example Usage

### Create a Market

```bash
curl -X POST "http://localhost:8000/markets" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "PJM_HB_CHICAGO",
    "name": "PJM Hourly Based Chicago",
    "region": "PJM",
    "market_type": "Day-ahead",
    "currency": "USD"
  }'
```

### Add Price Candle

```bash
curl -X POST "http://localhost:8000/markets/PJM_HB_CHICAGO/candles" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "PJM_HB_CHICAGO",
    "timestamp": "2024-01-15T10:00:00",
    "open": 45.50,
    "high": 46.75,
    "low": 45.25,
    "close": 46.00,
    "volume": 1500.0,
    "data_source": "PJM_API"
  }'
```

### Create Trade

```bash
curl -X POST "http://localhost:8000/trades" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "PJM_HB_CHICAGO",
    "position_type": "long",
    "entry_price": 45.50,
    "quantity": 100.0,
    "entry_time": "2024-01-15T09:30:00",
    "strategy": "Mean Reversion"
  }'
```

### Close Trade & Calculate P&L

```bash
curl -X PUT "http://localhost:8000/trades/{trade_id}" \
  -H "Content-Type: application/json" \
  -d '{
    "exit_price": 46.00,
    "exit_time": "2024-01-15T16:00:00",
    "status": "closed"
  }'
```

### Get Trading Statistics

```bash
curl -X GET "http://localhost:8000/trades/PJM_HB_CHICAGO/stats"
```

Response:

```json
{
  "total_trades": 10,
  "closed_trades": 8,
  "open_trades": 2,
  "win_rate": 75.0,
  "total_profit_loss": 450.5,
  "avg_profit_loss": 56.31
}
```

## ⚙️ Environment Variables

| Variable        | Description               | Default                     |
| --------------- | ------------------------- | --------------------------- |
| `MONGODB_URL`   | MongoDB connection string | `mongodb://localhost:27017` |
| `DATABASE_NAME` | Database name             | `power_trading`             |
| `API_TITLE`     | API title in docs         | `Power Trading API`         |
| `API_VERSION`   | API version               | `v1`                        |
| `DEBUG`         | Enable debug mode         | `False`                     |
| `HOST`          | Server host               | `0.0.0.0`                   |
| `PORT`          | Server port               | `8000`                      |

## 🔧 Why MongoDB?

**Advantages for Power Trading:**

- ✅ **Flexible Schema**: Easy to add new fields without migrations
- ✅ **Time Series Collections**: Native support for time-stamped OHLC data
- ✅ **Horizontal Scaling**: Sharding for large datasets
- ✅ **Rich Queries**: Complex aggregations on trading data
- ✅ **JSON-like Documents**: Natural fit for API responses
- ✅ **No SQL Learning Curve**: Simple and intuitive

**Hybrid Architecture:**

- Business data (markets, trades) → Standard collections with indexes
- Price history (candles) → Time series collections with auto-compression

## 📈 Performance Optimization Tips

### 1. Index Strategy

```python
# Already created automatically on startup:
# Markets: symbol (unique), status, region
# Trades: symbol, status, entry_time, (symbol, status)
# Candles: timestamp, (symbol, data_source, is_verified)
```

### 2. Query Optimization

```javascript
// Get last 24 hours of candles efficiently
db.power_price_candles
  .find({
    "metadata.symbol": "PJM_HB_CHICAGO",
    timestamp: {
      $gte: ISODate("2024-01-14T10:00:00Z"),
      $lte: ISODate("2024-01-15T10:00:00Z"),
    },
  })
  .sort({ timestamp: -1 });
```

### 3. Connection Pooling

```python
# App uses 50-size connection pool (configurable in database.py)
# Pre-ping enabled to maintain connection health
```

## 🚦 Status Codes

- `200` - Success
- `201` - Created
- `400` - Bad request (validation error)
- `404` - Resource not found
- `500` - Server error

## 🐛 Troubleshooting

### MongoDB Connection Failed

```
Error: Failed to connect to MongoDB at mongodb://localhost:27017
```

**Solution**:

- Ensure MongoDB is running
- Check connection string in `.env`
- Verify network connectivity if using MongoDB Atlas

### ObjectId Format Error

```
Error: Invalid trade ID format
```

**Solution**: Trade IDs must be valid MongoDB ObjectIds (24-character hex strings)

### Time Series Collection Already Exists

```
Error: namespace already exists with different options
```

**Solution**: Delete the collection from MongoDB Compass and restart

## 📚 Learning Resources

- [MongoDB Documentation](https://docs.mongodb.com/)
- [MongoDB Time Series Collections](https://docs.mongodb.com/manual/core/timeseries-collections/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Pydantic Documentation](https://docs.pydantic.dev/)

## 🔮 Future Enhancements

- [ ] WebSocket support for live price feeds
- [ ] Data ingestion pipeline for real-time updates
- [ ] Advanced analytics and backtesting engine
- [ ] User authentication and authorization
- [ ] Rate limiting and API keys
- [ ] Caching layer (Redis)
- [ ] GraphQL API
- [ ] Comprehensive test suite
- [ ] Docker containerization
- [ ] Kubernetes deployment configs

## 📄 License

MIT License - See LICENSE file for details

## 💬 Support

For issues, questions, or contributions:

1. Check existing issues
2. Create detailed issue with reproducible steps
3. Include version numbers and environment details

---

**Happy Trading! 📈**
"volume": 1500.0,
"data_source": "PJM_API"
}'

````

### Create Trade
```bash
curl -X POST "http://localhost:8000/trades" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "PJM_HB_CHICAGO",
    "position_type": "long",
    "entry_price": "45.50",
    "quantity": 100.0,
    "entry_time": "2024-01-15T09:30:00",
    "strategy": "Mean Reversion"
  }'
````

## Future Enhancements

- [ ] Database migration system (Alembic)
- [ ] User authentication and authorization
- [ ] Advanced analytics and backtesting
- [ ] Real-time data ingestion pipeline
- [ ] WebSocket support for live updates
- [ ] Caching layer (Redis)
- [ ] Advanced filtering and search
- [ ] Rate limiting
- [ ] Comprehensive testing suite

## Database Considerations

### For Time Series Data

The `PowerPriceCandle` table uses:

- Composite index on (symbol, timestamp) for efficient range queries
- Single index on timestamp for time-based sorting
- Separate index for verified status for query optimization

Consider converting to TimescaleDB hypertables for:

- Automatic partitioning by time
- Compression for historical data
- Better performance on large datasets

### Connection Pooling

- Pool size: 20
- Max overflow: 40
- Pre-ping enabled to maintain connection health

## Environment Variables

| Variable                  | Description                            | Default                                                   |
| ------------------------- | -------------------------------------- | --------------------------------------------------------- |
| `DATABASE_URL`            | PostgreSQL connection string           | `postgresql://user:password@localhost:5432/power_trading` |
| `TIMESERIES_DATABASE_URL` | Optional separate time series database | Same as DATABASE_URL                                      |
| `API_TITLE`               | API title for docs                     | `Power Trading API`                                       |
| `API_VERSION`             | API version                            | `v1`                                                      |
| `DEBUG`                   | Enable debug mode                      | `False`                                                   |
| `HOST`                    | Server host                            | `0.0.0.0`                                                 |
| `PORT`                    | Server port                            | `8000`                                                    |

## License

MIT

## Support

For issues and questions, please create an issue in the repository.
