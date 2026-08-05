"""
Export DAM hourly history from MongoDB into a single CSV file.

The exporter builds one row per delivery_date + hour_index and pulls each
requested field from the most appropriate source collection:

- constrained_area_price_usd_mwh from the standalone DAM constrained collection
- unconstrained_market_price_usd_mwh from the standalone DAM unconstrained collection
- confirmed_purchase_mwh / confirmed_sale_mwh from sapp_trading_invoice_hourly_details
- confirmed_purchase_price_usd_mwh / confirmed_sale_price_usd_mwh are derived from
  the matching invoice turnover divided by MWh
 - import_atc_mw / export_atc_mw are pulled from the BM ATC collection using the
   ZAMZ_TO_ZAML and ZAML_TO_ZAMZ headers.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pymongo import MongoClient

DEFAULT_START_DATE = date(2025, 1, 1)
DEFAULT_END_DATE = date.today()
DEFAULT_OUTPUT_FILE = f"dam_hourly_history_{DEFAULT_START_DATE.isoformat()}_to_{DEFAULT_END_DATE.isoformat()}.csv"

CONSTRAINED_DATA_SOURCE = "SAPP_AMT_DAM_CONSTRAINED_PRICE_RESULTS"
UNCONSTRAINED_DATA_SOURCE = "SAPP_AMT_DAM_UNCONSTRAINED_PRICE_RESULTS"
INVOICE_DATA_SOURCE = "SAPP_MTP_TRADING_INVOICE_HOURLY_DETAIL"
BM_ATC_DATA_SOURCE = "TSAM_BM_ATC_RESULTS"
BM_ATC_AREA = "All Areas"
BM_ATC_IMPORT_COLUMN = "ZAMZ_TO_ZAML"
BM_ATC_EXPORT_COLUMN = "ZAML_TO_ZAMZ"

CSV_FIELDS = [
    "delivery_date",
    "hour_index",
    "constrained_area_price_usd_mwh",
    "unconstrained_market_price_usd_mwh",
    "confirmed_purchase_price_usd_mwh",
    "confirmed_sale_price_usd_mwh",
    "confirmed_purchase_mwh",
    "confirmed_sale_mwh",
    "import_atc_mw",
    "export_atc_mw",
]


@dataclass(frozen=True)
class HourlyRow:
    delivery_date: str
    hour_index: int
    constrained_area_price_usd_mwh: Optional[float] = None
    unconstrained_market_price_usd_mwh: Optional[float] = None
    confirmed_purchase_price_usd_mwh: Optional[float] = None
    confirmed_sale_price_usd_mwh: Optional[float] = None
    confirmed_purchase_mwh: Optional[float] = None
    confirmed_sale_mwh: Optional[float] = None
    import_atc_mw: Optional[float] = None
    export_atc_mw: Optional[float] = None

    def as_csv_row(self) -> dict[str, Any]:
        return {
            "delivery_date": self.delivery_date,
            "hour_index": self.hour_index,
            "constrained_area_price_usd_mwh": self.constrained_area_price_usd_mwh,
            "unconstrained_market_price_usd_mwh": self.unconstrained_market_price_usd_mwh,
            "confirmed_purchase_price_usd_mwh": self.confirmed_purchase_price_usd_mwh,
            "confirmed_sale_price_usd_mwh": self.confirmed_sale_price_usd_mwh,
            "confirmed_purchase_mwh": self.confirmed_purchase_mwh,
            "confirmed_sale_mwh": self.confirmed_sale_mwh,
            "import_atc_mw": self.import_atc_mw,
            "export_atc_mw": self.export_atc_mw,
        }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _date_range(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_price(amount_usd: Any, volume_mwh: Any) -> Optional[float]:
    amount = _to_float(amount_usd)
    volume = _to_float(volume_mwh)
    if amount is None or volume in (None, 0):
        return None
    return amount / volume


def _load_db():
    load_dotenv(".env")
    import os

    mongo_url = os.getenv("MONGODB_URL") or os.getenv("DATABASE_URL")
    database_name = os.getenv("DATABASE_NAME")
    if not mongo_url:
        raise RuntimeError("MONGODB_URL or DATABASE_URL is not set in .env")
    if not database_name:
        raise RuntimeError("DATABASE_NAME is not set in .env")

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    db = client[database_name]
    db.command("ping")
    return client, db


def _fetch_constrained_prices(db, start_date: date, end_date: date) -> dict[tuple[str, int], Optional[float]]:
    query = {
        "delivery_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
        "metadata.data_source": CONSTRAINED_DATA_SOURCE,
    }
    rows = db["sapp_constrained_area_results"].find(
        query,
        {"_id": 0, "delivery_date": 1, "hour": 1, "area_price_usd_per_mwh": 1},
    )
    return {
        (row["delivery_date"], int(row["hour"])): _to_float(row.get("area_price_usd_per_mwh"))
        for row in rows
        if row.get("delivery_date") is not None and row.get("hour") is not None
    }


def _fetch_unconstrained_prices(db, start_date: date, end_date: date) -> dict[tuple[str, int], Optional[float]]:
    query = {
        "delivery_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
        "metadata.data_source": UNCONSTRAINED_DATA_SOURCE,
    }
    rows = db["sapp_unconstrained_area_results"].find(
        query,
        {"_id": 0, "delivery_date": 1, "hour": 1, "price_usd_per_mwh": 1},
    )
    return {
        (row["delivery_date"], int(row["hour"])): _to_float(row.get("price_usd_per_mwh"))
        for row in rows
        if row.get("delivery_date") is not None and row.get("hour") is not None
    }


def _fetch_invoice_rows(db, start_date: date, end_date: date) -> dict[tuple[str, int], dict[str, Optional[float]]]:
    query = {
        "market": "dam",
        "delivery_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
    }
    rows = db["sapp_trading_invoice_hourly_details"].find(
        query,
        {
            "_id": 0,
            "delivery_date": 1,
            "hour": 1,
            "traded_purchases_mwh": 1,
            "traded_sales_mwh": 1,
            "purchase_turnover_usd": 1,
            "sale_turnover_usd": 1,
        },
    )

    invoice_rows: dict[tuple[str, int], dict[str, Optional[float]]] = {}
    for row in rows:
        delivery_day = row.get("delivery_date")
        hour = row.get("hour")
        if delivery_day is None or hour is None:
            continue

        purchase_mwh = _to_float(row.get("traded_purchases_mwh"))
        sale_mwh = _to_float(row.get("traded_sales_mwh"))
        purchase_amount = _to_float(row.get("purchase_turnover_usd"))
        sale_amount = _to_float(row.get("sale_turnover_usd"))
        invoice_rows[(delivery_day, int(hour))] = {
            "confirmed_purchase_mwh": purchase_mwh,
            "confirmed_sale_mwh": sale_mwh,
            "confirmed_purchase_price_usd_mwh": _safe_price(purchase_amount, purchase_mwh),
            "confirmed_sale_price_usd_mwh": _safe_price(sale_amount, sale_mwh),
        }
    return invoice_rows


def _fetch_atc_rows(db, start_date: date, end_date: date) -> dict[tuple[str, int], dict[str, Optional[float]]]:
    query = {
        "delivery_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
        "metadata.data_source": BM_ATC_DATA_SOURCE,
        "area": BM_ATC_AREA,
    }
    rows = db["sapp_bm_atc_results"].find(
        query,
        {
            "_id": 0,
            "delivery_date": 1,
            "hour": 1,
            "column_values": 1,
            BM_ATC_IMPORT_COLUMN: 1,
            BM_ATC_EXPORT_COLUMN: 1,
        },
    )

    atc_rows: dict[tuple[str, int], dict[str, Optional[float]]] = {}
    for row in rows:
        delivery_day = row.get("delivery_date")
        hour = row.get("hour")
        if delivery_day is None or hour is None:
            continue

        column_values = row.get("column_values") or {}
        import_atc = column_values.get(BM_ATC_IMPORT_COLUMN, row.get(BM_ATC_IMPORT_COLUMN))
        export_atc = column_values.get(BM_ATC_EXPORT_COLUMN, row.get(BM_ATC_EXPORT_COLUMN))
        atc_rows[(delivery_day, int(hour))] = {
            "import_atc_mw": _to_float(import_atc),
            "export_atc_mw": _to_float(export_atc),
        }

    return atc_rows


def build_rows(db, start_date: date, end_date: date) -> list[HourlyRow]:
    constrained_prices = _fetch_constrained_prices(db, start_date, end_date)
    unconstrained_prices = _fetch_unconstrained_prices(db, start_date, end_date)
    invoice_rows = _fetch_invoice_rows(db, start_date, end_date)
    atc_rows = _fetch_atc_rows(db, start_date, end_date)

    rows: list[HourlyRow] = []
    for current_date in _date_range(start_date, end_date):
        delivery_date = current_date.isoformat()
        for hour_index in range(1, 25):
            key = (delivery_date, hour_index)
            invoice = invoice_rows.get(key, {})
            atc = atc_rows.get(key, {})
            rows.append(
                HourlyRow(
                    delivery_date=delivery_date,
                    hour_index=hour_index,
                    constrained_area_price_usd_mwh=constrained_prices.get(key),
                    unconstrained_market_price_usd_mwh=unconstrained_prices.get(key),
                    confirmed_purchase_price_usd_mwh=invoice.get(
                        "confirmed_purchase_price_usd_mwh"
                    ),
                    confirmed_sale_price_usd_mwh=invoice.get("confirmed_sale_price_usd_mwh"),
                    confirmed_purchase_mwh=invoice.get("confirmed_purchase_mwh"),
                    confirmed_sale_mwh=invoice.get("confirmed_sale_mwh"),
                    import_atc_mw=atc.get("import_atc_mw"),
                    export_atc_mw=atc.get("export_atc_mw"),
                )
            )
    return rows


def write_csv(rows: list[HourlyRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_row())


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Export DAM hourly history to CSV.")
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=DEFAULT_START_DATE,
        help="First delivery date to export, inclusive. Default: 2025-01-01",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=DEFAULT_END_DATE,
        help="Last delivery date to export, inclusive. Default: today",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT_FILE),
        help="CSV output path",
    )
    args = parser.parse_args()

    if args.end_date < args.start_date:
        raise SystemExit("end-date must be greater than or equal to start-date")

    client = None
    try:
        client, db = _load_db()
        rows = build_rows(db, args.start_date, args.end_date)
        write_csv(rows, args.output)
        print(
            f"Exported {len(rows)} hourly rows from {args.start_date.isoformat()} "
            f"to {args.end_date.isoformat()} into {args.output}"
        )
        print(
            "Note: confirmed prices are derived from invoice turnover divided by MWh, "
            "and ATC is pulled from the BM ATC table using the ZAMZ_TO_ZAML and "
            "ZAML_TO_ZAMZ columns."
        )
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
