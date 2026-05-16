"""
MongoDB Database Connection and Setup
Handles connection pooling, collection creation, and indexes
"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ServerSelectionTimeoutError
from app.core.config import settings


class MongoDB:
    """MongoDB connection and collection management"""
    
    client = None
    db = None


def connect_db():
    """
    Establish MongoDB connection and initialize collections
    Called on application startup
    """
    try:
        # Create MongoDB client with connection pooling
        MongoDB.client = MongoClient(
            settings.MONGODB_URL,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=None,
            retryWrites=True,
            maxPoolSize=50
        )
        
        # Get database reference
        MongoDB.db = MongoDB.client[settings.DATABASE_NAME]
        
        # Test connection
        MongoDB.client.admin.command('ping')
        
        print(f"✓ Connected to MongoDB: {settings.DATABASE_NAME}")
        
        # Initialize collections and indexes
        _initialize_collections()
        
    except ServerSelectionTimeoutError:
        print(f"✗ Failed to connect to MongoDB at {settings.MONGODB_URL}")
        raise


def disconnect_db():
    """Close MongoDB connection"""
    if MongoDB.client:
        MongoDB.client.close()
        print("✓ MongoDB connection closed")


def _initialize_collections():
    """
    Create collections and set up indexes
    Indexes improve query performance significantly
    """
    db = MongoDB.db
    
    # ===== BUSINESS DATA COLLECTIONS =====
    
    # Markets Collection: Stores market metadata and configuration
    if "power_markets" not in db.list_collection_names():
        db.create_collection("power_markets")
        markets_collection = db["power_markets"]
        markets_collection.create_index(
            [("symbol", ASCENDING)],
            unique=True,
            name="idx_symbol_unique"
        )
        markets_collection.create_index(
            [("status", ASCENDING)],
            name="idx_status"
        )
        markets_collection.create_index(
            [("region", ASCENDING)],
            name="idx_region"
        )
        print("  ✓ Created 'power_markets' collection")
    
    # Trades Collection: Stores all trading transactions
    if "trades" not in db.list_collection_names():
        db.create_collection("trades")
        trades_collection = db["trades"]
        trades_collection.create_index(
            [("symbol", ASCENDING)],
            name="idx_trade_symbol"
        )
        trades_collection.create_index(
            [("status", ASCENDING)],
            name="idx_trade_status"
        )
        trades_collection.create_index(
            [("entry_time", DESCENDING)],
            name="idx_trade_entry_time"
        )
        trades_collection.create_index(
            [("symbol", ASCENDING), ("status", ASCENDING)],
            name="idx_trade_symbol_status"
        )
        print("  ✓ Created 'trades' collection")
    
    # ===== TIME SERIES COLLECTIONS =====
    
    # PowerPriceCandles: MongoDB native time series collection for OHLC data
    # Time series collections automatically handle data at scale with built-in compression
    if "power_price_candles" not in db.list_collection_names():
        db.create_collection(
            "power_price_candles",
            timeseries={
                "timeField": "timestamp",
                "metaField": "metadata",
                "granularity": "hours"  # 1-hour candles as specified
            }
        )
        print("  ✓ Created 'power_price_candles' time series collection")
    
    # PowerSystemTelemetry: Time series collection for operational/system data
    # Stores hourly generator loads, transmission flows, and busbar voltages
    if "power_system_telemetry" not in db.list_collection_names():
        db.create_collection(
            "power_system_telemetry",
            timeseries={
                "timeField": "timestamp",
                "metaField": "metadata",
                "granularity": "hours"  # 1-hour operational data
            }
        )
        print("  ✓ Created 'power_system_telemetry' time series collection")

    # SAPP constrained area results: hourly DAM data imported from SAPP MTP documents
    if "sapp_constrained_area_results" not in db.list_collection_names():
        db.create_collection("sapp_constrained_area_results")
        sapp_collection = db["sapp_constrained_area_results"]
        sapp_collection.create_index(
            [("timestamp", DESCENDING)],
            name="idx_sapp_timestamp"
        )
        sapp_collection.create_index(
            [("delivery_date", ASCENDING), ("hour", ASCENDING)],
            unique=True,
            name="idx_sapp_delivery_date_hour_unique"
        )
        sapp_collection.create_index(
            [("metadata.data_source", ASCENDING)],
            name="idx_sapp_data_source"
        )
        print("  Created 'sapp_constrained_area_results' collection")


def get_db():
    """Dependency for getting MongoDB database reference"""
    if MongoDB.db is None:
        connect_db()
    return MongoDB.db

