"""Correct one stored IDM hourly row and notify the frontend to resync it."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from dotenv import load_dotenv

from app.db.database import get_db
from app.services.sapp_sync_notifier import record_external_run
from sapp_email_document_importer import (
    IDM_PORTFOLIO_COLLECTION,
    IDM_PORTFOLIO_DATASET_ID,
    IDM_PORTFOLIO_JOB,
    _idm_trade_fingerprint,
)


COLLECTION = IDM_PORTFOLIO_COLLECTION


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected date format YYYY-MM-DD") from exc


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Correct a duplicate IDM hourly row and send a fresh sync event."
    )
    parser.add_argument("--delivery-date", required=True, type=_date)
    parser.add_argument(
        "--hour",
        required=True,
        type=int,
        help="Stored SAPP hour index; 20-21 is represented as hour 21.",
    )
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--trade-type", choices=("BUY", "SELL"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.hour < 1 or args.hour > 24:
        parser.error("--hour must be between 1 and 24")
    if args.quantity <= 0:
        parser.error("--quantity must be greater than zero")

    collection = get_db()[COLLECTION]
    query = {
        "market": "idm",
        "delivery_date": args.delivery_date,
        "hour": args.hour,
    }
    current = collection.find_one(query)
    if not current:
        print("[idm-repair] ERROR | no matching IDM row found")
        return 1

    price_usd = float(current.get("price_usd_per_mwh") or 0)
    price_zar = current.get("price_zar_per_mwh")
    portfolio = current.get("portfolio")
    fingerprint = _idm_trade_fingerprint(
        delivery_date=args.delivery_date,
        hour=args.hour,
        trade_type=args.trade_type,
        portfolio=portfolio,
        quantity=args.quantity,
        price_usd=price_usd,
        price_zar=float(price_zar) if price_zar is not None else None,
    )
    amount_usd = args.quantity * price_usd
    now = datetime.now(timezone.utc)
    updated = {
        "trade_id": fingerprint,
        "trade_fingerprint": fingerprint,
        "trade_ids": [fingerprint],
        "trade_fingerprints": [fingerprint],
        "trade_type": args.trade_type,
        "traded_purchases_mwh": args.quantity if args.trade_type == "BUY" else None,
        "traded_sales_mwh": args.quantity if args.trade_type == "SELL" else None,
        "purchase_turnover_usd": amount_usd if args.trade_type == "BUY" else None,
        "sale_turnover_usd": -amount_usd if args.trade_type == "SELL" else None,
        "participant_total_area_schedule_mwh": (
            args.quantity if args.trade_type == "BUY" else -args.quantity
        ),
        "area_price_usd_per_mwh": price_usd,
        "idm_quantity_mwh": args.quantity,
        "idm_price_usd_per_mwh": price_usd,
        "idm_price_zar_per_mwh": price_zar,
        "idm_trade_type": args.trade_type,
        "idm_trade_id": fingerprint,
        "idm_trade_ids": [fingerprint],
        "metadata.idm_trade_ids": [fingerprint],
        "metadata.idm_trade_fingerprints": [fingerprint],
        "metadata.repaired_duplicate": True,
        "updated_at": now,
    }
    old_quantity = current.get("traded_purchases_mwh") or current.get("traded_sales_mwh")
    print(
        f"[idm-repair] delivery={args.delivery_date} hour={args.hour} "
        f"old_quantity={old_quantity} new_quantity={args.quantity} "
        f"old_trade_ids={len(current.get('trade_ids', []))}"
    )
    if args.dry_run:
        print("[idm-repair] DRY RUN | no database update or webhook sent")
        return 0

    collection.update_one(query, {"$set": updated})
    run_id = f"manual:idm-repair:{args.delivery_date}:{args.hour}:{fingerprint}"
    notification = record_external_run(
        run_id=run_id,
        dataset_id=IDM_PORTFOLIO_DATASET_ID,
        job=IDM_PORTFOLIO_JOB,
        status="success",
        start_date=args.delivery_date,
        end_date=args.delivery_date,
        result={
            "market": "idm",
            "delivery_date": args.delivery_date,
            "hour": args.hour,
            "rows": 1,
            "corrected_quantity_mwh": args.quantity,
            "source": "idm_duplicate_repair",
        },
        source="idm_repair",
    )
    print(
        f"[idm-repair] corrected | webhook={notification.get('status')} "
        f"http={notification.get('http_status', '-')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
