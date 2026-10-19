"""Move legacy IDM rows into the participant-portfolio-results collection."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.db.database import get_db
from sapp_email_document_importer import IDM_PORTFOLIO_COLLECTION


SOURCE_COLLECTION = "sapp_trading_invoice_hourly_details"


def _portfolio_record(row: dict) -> dict:
    record = {key: value for key, value in row.items() if key != "_id"}
    purchase = float(row.get("traded_purchases_mwh") or 0)
    sale = float(row.get("traded_sales_mwh") or 0)
    quantity = row.get("idm_quantity_mwh") or purchase + sale or None
    trade_type = row.get("idm_trade_type") or row.get("trade_type")
    price_usd = row.get("idm_price_usd_per_mwh") or row.get("price_usd_per_mwh")
    price_zar = row.get("idm_price_zar_per_mwh") or row.get("price_zar_per_mwh")
    trade_ids = list(row.get("trade_ids") or [])
    if row.get("trade_id") and row["trade_id"] not in trade_ids:
        trade_ids.append(row["trade_id"])

    record.update(
        {
            "market": "idm",
            "source_delivery_date": row.get("delivery_date"),
            "period_start_date": row.get("delivery_date"),
            "period_end_date": row.get("delivery_date"),
            "participant_total_area_schedule_mwh": purchase - sale,
            "area_price_usd_per_mwh": row.get("area_price_usd_per_mwh") or price_usd,
            "idm_quantity_mwh": quantity,
            "idm_price_usd_per_mwh": price_usd,
            "idm_price_zar_per_mwh": price_zar,
            "idm_trade_type": trade_type,
            "idm_trade_id": row.get("trade_id"),
            "idm_trade_ids": trade_ids,
            "metadata": {
                **(row.get("metadata") or {}),
                "storage_dataset": "participant_portfolio_results",
                "migrated_from": SOURCE_COLLECTION,
            },
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return record


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Copy legacy IDM rows into participant portfolio results."
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete each legacy source row after it is copied successfully.",
    )
    args = parser.parse_args()

    db = get_db()
    source = db[SOURCE_COLLECTION]
    target = db[IDM_PORTFOLIO_COLLECTION]
    rows = list(source.find({"market": "idm"}))
    copied = 0
    skipped = 0
    deleted = 0
    for row in rows:
        query = {
            "market": "idm",
            "delivery_date": row.get("delivery_date"),
            "hour": row.get("hour"),
        }
        result = target.update_one(
            query,
            {"$setOnInsert": _portfolio_record(row)},
            upsert=True,
        )
        if result.upserted_id is not None:
            copied += 1
        else:
            skipped += 1
        if args.delete_source:
            source.delete_one({"_id": row["_id"]})
            deleted += 1

    print(
        f"[idm-migration] source_rows={len(rows)} copied={copied} "
        f"skipped_existing={skipped} deleted={deleted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
