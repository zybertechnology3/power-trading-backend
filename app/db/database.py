"""
MongoDB database connection and collection setup.
"""

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ServerSelectionTimeoutError

from app.core.config import settings


class MongoDB:
    """MongoDB connection holder."""

    client = None
    db = None


def connect_db():
    """Establish MongoDB connection and initialize required collections."""
    try:
        MongoDB.client = MongoClient(
            settings.MONGODB_URL,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=None,
            retryWrites=True,
            maxPoolSize=50,
        )
        MongoDB.db = MongoDB.client[settings.DATABASE_NAME]
        MongoDB.client.admin.command("ping")

        print(f"Connected to MongoDB: {settings.DATABASE_NAME}")
        _initialize_collections()

    except ServerSelectionTimeoutError:
        print(f"Failed to connect to MongoDB at {settings.MONGODB_URL}")
        raise


def disconnect_db():
    """Close MongoDB connection."""
    if MongoDB.client:
        MongoDB.client.close()
        print("MongoDB connection closed")


def _initialize_collections():
    """Create required collections and indexes."""
    db = MongoDB.db

    if "power_system_telemetry" not in db.list_collection_names():
        db.create_collection(
            "power_system_telemetry",
            timeseries={
                "timeField": "timestamp",
                "metaField": "metadata",
                "granularity": "hours",
            },
        )
        print("Created 'power_system_telemetry' time series collection")

    if "sapp_constrained_area_results" not in db.list_collection_names():
        db.create_collection("sapp_constrained_area_results")
        sapp_collection = db["sapp_constrained_area_results"]
        sapp_collection.create_index(
            [("timestamp", DESCENDING)],
            name="idx_sapp_timestamp",
        )
        sapp_collection.create_index(
            [("delivery_date", ASCENDING), ("hour", ASCENDING)],
            unique=True,
            name="idx_sapp_delivery_date_hour_unique",
        )
        sapp_collection.create_index(
            [("metadata.data_source", ASCENDING)],
            name="idx_sapp_data_source",
        )
        print("Created 'sapp_constrained_area_results' collection")


def get_db():
    """Get MongoDB database reference."""
    if MongoDB.db is None:
        connect_db()
    return MongoDB.db
