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

    if "sapp_participant_portfolio_results" not in db.list_collection_names():
        db.create_collection("sapp_participant_portfolio_results")
        participant_portfolio_collection = db["sapp_participant_portfolio_results"]
        participant_portfolio_collection.create_index(
            [("timestamp", DESCENDING)],
            name="idx_sapp_participant_portfolio_timestamp",
        )
        participant_portfolio_collection.create_index(
            [("delivery_date", ASCENDING), ("hour", ASCENDING)],
            unique=True,
            name="idx_sapp_participant_portfolio_delivery_date_hour_unique",
        )
        participant_portfolio_collection.create_index(
            [("metadata.data_source", ASCENDING)],
            name="idx_sapp_participant_portfolio_data_source",
        )
        print("Created 'sapp_participant_portfolio_results' collection")

    if "sapp_trading_invoice_credit_notes" not in db.list_collection_names():
        db.create_collection("sapp_trading_invoice_credit_notes")
        trading_invoice_collection = db["sapp_trading_invoice_credit_notes"]
        trading_invoice_collection.create_index(
            [("timestamp", DESCENDING)],
            name="idx_sapp_trading_invoice_timestamp",
        )
        trading_invoice_collection.create_index(
            [("delivery_date", ASCENDING)],
            unique=True,
            name="idx_sapp_trading_invoice_delivery_date_unique",
        )
        trading_invoice_collection.create_index(
            [("metadata.data_source", ASCENDING)],
            name="idx_sapp_trading_invoice_data_source",
        )
        print("Created 'sapp_trading_invoice_credit_notes' collection")

    if "sapp_trading_invoice_hourly_details" not in db.list_collection_names():
        db.create_collection("sapp_trading_invoice_hourly_details")
        trading_invoice_hourly_collection = db["sapp_trading_invoice_hourly_details"]
        trading_invoice_hourly_collection.create_index(
            [("timestamp", DESCENDING)],
            name="idx_sapp_trading_invoice_hourly_timestamp",
        )
        trading_invoice_hourly_collection.create_index(
            [("delivery_date", ASCENDING), ("market", ASCENDING), ("hour", ASCENDING)],
            unique=True,
            name="idx_sapp_trading_invoice_hourly_date_market_hour_unique",
        )
        trading_invoice_hourly_collection.create_index(
            [("metadata.data_source", ASCENDING)],
            name="idx_sapp_trading_invoice_hourly_data_source",
        )
        print("Created 'sapp_trading_invoice_hourly_details' collection")

    if "sapp_bids" not in db.list_collection_names():
        db.create_collection("sapp_bids")
        print("Created 'sapp_bids' collection")
    bids_collection = db["sapp_bids"]
    bids_collection.create_index(
        [("delivery_date", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
        name="idx_sapp_bids_date_status_updated",
    )
    bids_collection.create_index(
        [("market", ASCENDING), ("period_start_date", ASCENDING), ("status", ASCENDING)],
        name="idx_sapp_bids_market_period_status",
    )
    bids_collection.create_index(
        [("status", ASCENDING), ("submitted_at", DESCENDING)],
        name="idx_sapp_bids_status_submitted",
    )
    bids_collection.create_index(
        [("created_at", DESCENDING)],
        name="idx_sapp_bids_created_at",
    )

    if "sapp_bid_templates" not in db.list_collection_names():
        db.create_collection("sapp_bid_templates")
        print("Created 'sapp_bid_templates' collection")
    bid_templates_collection = db["sapp_bid_templates"]
    bid_templates_collection.create_index(
        [("name_key", ASCENDING)],
        unique=True,
        name="idx_sapp_bid_templates_name_key_unique",
    )
    bid_templates_collection.create_index(
        [("updated_at", DESCENDING)],
        name="idx_sapp_bid_templates_updated_at",
    )
    bid_templates_collection.create_index(
        [("market", ASCENDING), ("updated_at", DESCENDING)],
        name="idx_sapp_bid_templates_market_updated",
    )

    if "contracts" not in db.list_collection_names():
        db.create_collection("contracts")
        print("Created 'contracts' collection")
    contracts_collection = db["contracts"]
    contracts_collection.create_index(
        [("deleted_at", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="idx_contracts_active_created",
    )
    contracts_collection.create_index(
        [("contract_type", ASCENDING), ("firmness", ASCENDING), ("created_at", DESCENDING)],
        name="idx_contracts_type_firmness_created",
    )
    contracts_collection.create_index(
        [("customer", ASCENDING)],
        name="idx_contracts_customer",
    )

    if "resource_level_monitoring_records" not in db.list_collection_names():
        db.create_collection("resource_level_monitoring_records")
        print("Created 'resource_level_monitoring_records' collection")
    level_records_collection = db["resource_level_monitoring_records"]
    level_records_collection.create_index(
        [("reservoir", ASCENDING), ("record_date", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        partialFilterExpression={"deleted_at": {"$exists": False}},
        name="idx_resource_level_records_reservoir_date_unique",
    )
    level_records_collection.create_index(
        [("reservoir", ASCENDING), ("record_date", DESCENDING), ("_id", DESCENDING)],
        name="idx_resource_level_records_reservoir_date",
    )
    level_records_collection.create_index(
        [("created_at", DESCENDING)],
        name="idx_resource_level_records_created_at",
    )

    if "resource_level_monitoring_fields" not in db.list_collection_names():
        db.create_collection("resource_level_monitoring_fields")
        print("Created 'resource_level_monitoring_fields' collection")
    level_fields_collection = db["resource_level_monitoring_fields"]
    level_fields_collection.create_index(
        [("reservoir", ASCENDING), ("key", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        partialFilterExpression={"deleted_at": {"$exists": False}},
        name="idx_resource_level_fields_reservoir_key_unique",
    )
    level_fields_collection.create_index(
        [("reservoir", ASCENDING), ("created_at", ASCENDING)],
        name="idx_resource_level_fields_reservoir_created",
    )


def get_db():
    """Get MongoDB database reference."""
    if MongoDB.db is None:
        connect_db()
    return MongoDB.db
