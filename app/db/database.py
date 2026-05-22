"""
MongoDB database connection and collection setup.
"""

import math
from datetime import date, datetime, timedelta, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ServerSelectionTimeoutError

from app.core.config import settings


class MongoDB:
    """MongoDB connection holder."""

    client = None
    db = None


DEFAULT_MONTHLY_SOLAR_IRRADIATION_W_M2 = [
    760.0,
    740.0,
    700.0,
    660.0,
    620.0,
    590.0,
    610.0,
    660.0,
    720.0,
    780.0,
    800.0,
    790.0,
]


def _solar_weather_condition(value: float) -> str:
    if value >= 760:
        return "sunny"
    if value >= 680:
        return "partly_cloudy"
    if value >= 610:
        return "cloudy"
    return "overcast"


def _seed_solar_irradiation_records(collection):
    """Seed deterministic daily solar irradiation readings for demo use."""
    if collection.count_documents({}) > 0:
        return

    today = datetime.now(timezone.utc).date()
    start_date = date(today.year - 1, 1, 1)
    end_date = today
    now = datetime.now(timezone.utc)
    records = []
    current_date = start_date

    while current_date <= end_date:
        monthly_base = DEFAULT_MONTHLY_SOLAR_IRRADIATION_W_M2[current_date.month - 1]
        day_wave = math.sin((current_date.timetuple().tm_yday / 365) * math.tau)
        short_wave = math.sin(current_date.day * 1.7)
        irradiation_w_m2 = round(monthly_base + day_wave * 22 + short_wave * 28, 2)
        irradiation_w_m2 = max(0.0, irradiation_w_m2)
        records.append(
            {
                "plant": "lps_solar",
                "record_date": current_date.isoformat(),
                "irradiation_w_m2": irradiation_w_m2,
                "weather_condition": _solar_weather_condition(irradiation_w_m2),
                "notes": "Seeded demo irradiation reading",
                "created_at": now,
                "updated_at": now,
            }
        )
        current_date += timedelta(days=1)

    if records:
        collection.insert_many(records, ordered=False)


def _seed_metering_meters(collection):
    """Seed the default four metering columns for each site."""
    if collection.count_documents({}) > 0:
        return

    now = datetime.now(timezone.utc)
    records = []
    for site, site_name in {"mps": "MPS", "lps": "LPS"}.items():
        for index in range(1, 5):
            records.append(
                {
                    "meter_id": f"{site}_meter_{index}",
                    "site": site,
                    "name": f"{site_name} Meter {index}",
                    "column_key": f"meter_{index}",
                    "entry_mode": "manual",
                    "unit": "MWh",
                    "sort_order": index,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    collection.insert_many(records, ordered=False)


def _floor_to_meter_interval(value: datetime) -> datetime:
    minute = 30 if value.minute >= 30 else 0
    return value.replace(minute=minute, second=0, microsecond=0)


def _seed_metering_interval_readings(collection):
    """Seed recent deterministic 30-minute meter capture rows for demo use."""
    if collection.count_documents({}) > 0:
        return

    now = datetime.now(timezone.utc)
    end_time = _floor_to_meter_interval(now)
    start_time = end_time - timedelta(days=7)
    records = []
    current_time = start_time

    while current_time <= end_time:
        interval_index = int((current_time - start_time).total_seconds() / 1800)
        day_factor = math.sin((current_time.hour + current_time.minute / 60) / 24 * math.tau)
        for site, site_offset in {"mps": 0.0, "lps": 3.5}.items():
            readings = {}
            for meter_index in range(1, 5):
                base_value = 18 + meter_index * 2.75 + site_offset
                variation = day_factor * 4.5 + math.sin(interval_index / 5 + meter_index) * 1.2
                readings[f"{site}_meter_{meter_index}"] = round(max(0.0, base_value + variation), 3)
            records.append(
                {
                    "site": site,
                    "interval_start": current_time.isoformat(),
                    "readings": readings,
                    "source": "manual",
                    "notes": "Seeded demo meter capture row",
                    "created_at": now,
                    "updated_at": now,
                }
            )
        current_time += timedelta(minutes=30)

    if records:
        collection.insert_many(records, ordered=False)


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
        name="idx_resource_level_fields_reservoir_key_unique",
    )
    level_fields_collection.create_index(
        [("reservoir", ASCENDING), ("created_at", ASCENDING)],
        name="idx_resource_level_fields_reservoir_created",
    )

    if "resource_hydrology_forecasts" not in db.list_collection_names():
        db.create_collection("resource_hydrology_forecasts")
        print("Created 'resource_hydrology_forecasts' collection")
    hydrology_forecasts_collection = db["resource_hydrology_forecasts"]
    hydrology_forecasts_collection.create_index(
        [("start_year", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        name="idx_resource_hydrology_forecasts_start_year_unique",
    )
    hydrology_forecasts_collection.create_index(
        [("updated_at", DESCENDING)],
        name="idx_resource_hydrology_forecasts_updated_at",
    )

    if "resource_solar_irradiation_records" not in db.list_collection_names():
        db.create_collection("resource_solar_irradiation_records")
        print("Created 'resource_solar_irradiation_records' collection")
    solar_irradiation_collection = db["resource_solar_irradiation_records"]
    solar_irradiation_collection.create_index(
        [("plant", ASCENDING), ("record_date", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        name="idx_resource_solar_irradiation_plant_date_unique",
    )
    solar_irradiation_collection.create_index(
        [("plant", ASCENDING), ("record_date", DESCENDING), ("_id", DESCENDING)],
        name="idx_resource_solar_irradiation_plant_date",
    )
    solar_irradiation_collection.create_index(
        [("created_at", DESCENDING)],
        name="idx_resource_solar_irradiation_created_at",
    )
    _seed_solar_irradiation_records(solar_irradiation_collection)

    if "metering_meters" not in db.list_collection_names():
        db.create_collection("metering_meters")
        print("Created 'metering_meters' collection")
    metering_meters_collection = db["metering_meters"]
    metering_meters_collection.create_index(
        [("meter_id", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        name="idx_metering_meters_meter_id_unique",
    )
    metering_meters_collection.create_index(
        [("site", ASCENDING), ("sort_order", ASCENDING)],
        name="idx_metering_meters_site_sort",
    )
    _seed_metering_meters(metering_meters_collection)

    if "metering_interval_readings" not in db.list_collection_names():
        db.create_collection("metering_interval_readings")
        print("Created 'metering_interval_readings' collection")
    metering_readings_collection = db["metering_interval_readings"]
    metering_readings_collection.create_index(
        [("site", ASCENDING), ("interval_start", ASCENDING), ("deleted_at", ASCENDING)],
        unique=True,
        name="idx_metering_readings_site_interval_unique",
    )
    metering_readings_collection.create_index(
        [("site", ASCENDING), ("interval_start", DESCENDING), ("_id", DESCENDING)],
        name="idx_metering_readings_site_interval",
    )
    metering_readings_collection.create_index(
        [("created_at", DESCENDING)],
        name="idx_metering_readings_created_at",
    )
    _seed_metering_interval_readings(metering_readings_collection)

    if "energy_yearly_budgets" not in db.list_collection_names():
        db.create_collection("energy_yearly_budgets")
        print("Created 'energy_yearly_budgets' collection")
    energy_yearly_budgets_collection = db["energy_yearly_budgets"]
    energy_yearly_budgets_collection.create_index(
        [("deleted_at", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="idx_energy_yearly_budgets_active_created",
    )
    energy_yearly_budgets_collection.create_index(
        [("year", ASCENDING), ("created_at", DESCENDING)],
        name="idx_energy_yearly_budgets_year_created",
    )


def get_db():
    """Get MongoDB database reference."""
    if MongoDB.db is None:
        connect_db()
    return MongoDB.db
