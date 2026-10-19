"""Replay frontend notifications for IDM rows already stored in MongoDB.

This does not re-import email attachments or create trades. It only emits a
fresh signed notification for each selected delivery date so the frontend can
fetch the stored hourly IDM rows again.
"""

from __future__ import annotations

import argparse
import os
import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from dotenv import load_dotenv

from app.db.database import get_db
from app.services.sapp_sync_notifier import record_external_run


COLLECTION = "sapp_trading_invoice_hourly_details"
DATASET_ID = "idm_trades"
JOB = "idm_trade_concluded"


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def _date_filter(args: argparse.Namespace) -> dict[str, Any]:
    if args.delivery_date:
        return {"delivery_date": args.delivery_date.isoformat()}
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise ValueError("--start-date cannot be after --end-date")
    if args.start_date or args.end_date:
        return {
            "delivery_date": {
                **({"$gte": args.start_date.isoformat()} if args.start_date else {}),
                **({"$lte": args.end_date.isoformat()} if args.end_date else {}),
            }
        }
    raise ValueError("Provide --delivery-date or --start-date/--end-date")


def _group_idm_rows(date_query: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows = get_db()[COLLECTION].find(
        {"market": "idm", **date_query},
        {
            "_id": 0,
            "delivery_date": 1,
            "hour": 1,
            "traded_purchases_mwh": 1,
            "traded_sales_mwh": 1,
        },
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        delivery_date = str(row.get("delivery_date", "")).strip()
        if delivery_date:
            grouped[delivery_date].append(row)
    return dict(grouped)


def replay(*, date_query: dict[str, Any], dry_run: bool = False) -> int:
    grouped = _group_idm_rows(date_query)
    if not grouped:
        print("[idm-replay] No stored IDM rows matched the requested date range.")
        return 0

    print(
        f"[idm-replay] Found {sum(len(rows) for rows in grouped.values())} row(s) "
        f"across {len(grouped)} delivery date(s)."
    )
    for delivery_date in sorted(grouped):
        rows = grouped[delivery_date]
        purchase_mwh = sum(float(row.get("traded_purchases_mwh") or 0) for row in rows)
        sale_mwh = sum(float(row.get("traded_sales_mwh") or 0) for row in rows)
        result = {
            "market": "idm",
            "delivery_date": delivery_date,
            "rows": len(rows),
            "purchase_mwh": purchase_mwh,
            "sale_mwh": sale_mwh,
            "source": "stored_idm_trade_replay",
        }
        if dry_run:
            print(
                f"[idm-replay] DRY RUN | delivery={delivery_date} "
                f"rows={len(rows)}"
            )
            continue

        run_id = f"manual:idm-replay:{delivery_date}:{uuid.uuid4().hex[:8]}"
        notification = record_external_run(
            run_id=run_id,
            dataset_id=DATASET_ID,
            job=JOB,
            status="success",
            start_date=delivery_date,
            end_date=delivery_date,
            result=result,
            source="idm_replay",
        )
        print(
            f"[idm-replay] delivery={delivery_date} rows={len(rows)} "
            f"webhook={notification.get('status')} "
            f"http={notification.get('http_status', '-') }"
        )
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Replay signed frontend notifications for stored IDM rows."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--delivery-date", type=_date)
    selection.add_argument("--start-date", type=_date)
    parser.add_argument("--end-date", type=_date)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching dates without recording runs or sending webhooks.",
    )
    args = parser.parse_args()
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be provided together")
    try:
        return replay(date_query=_date_filter(args), dry_run=args.dry_run)
    except Exception as exc:
        print(f"[idm-replay] ERROR | {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
