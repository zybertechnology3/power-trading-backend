from __future__ import annotations

import argparse
from copy import deepcopy
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")

from app.db.database import connect_db, disconnect_db  # noqa: E402
from sapp_scraper import (  # noqa: E402
    INTERNAL_UNSET_FIELDS_FIELD,
    TRADING_INVOICE_RESULTS_JOB,
    TRADING_INVOICE_HOURLY_COLLECTION,
    extract_trading_invoice_results,
    store_records_in_database,
)


def _iter_invoice_files(folder: Path, recursive: bool) -> list[Path]:
    patterns = ("*.xlsx", "*.xlsm")
    files: list[Path] = []
    for pattern in patterns:
        iterator = folder.rglob(pattern) if recursive else folder.glob(pattern)
        files.extend(path for path in iterator if path.is_file())
    return sorted(
        {path.resolve() for path in files},
        key=lambda path: str(path.relative_to(folder.resolve())).lower()
        if path.is_relative_to(folder.resolve())
        else path.name.lower(),
    )


def _iter_ignored_pdf_files(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*.pdf") if recursive else folder.glob("*.pdf")
    return sorted(
        {path.resolve() for path in iterator if path.is_file()},
        key=lambda path: str(path.relative_to(folder.resolve())).lower()
        if path.is_relative_to(folder.resolve())
        else path.name.lower(),
    )


def _is_hourly_detail_record(record: dict) -> bool:
    return "hour" in record and "market" in record and "delivery_date" in record


def _merge_summary_value_dicts(values: list[dict]) -> dict:
    has_mwh = any(value.get("mwh") is not None for value in values)
    has_amount = any(value.get("amount_usd") is not None for value in values)
    total_mwh = sum((value.get("mwh") or 0.0) for value in values)
    total_amount = sum((value.get("amount_usd") or 0.0) for value in values)
    return {
        "mwh": total_mwh if has_mwh else None,
        "amount_usd": total_amount if has_amount else None,
        "average_price_usd_per_mwh": (
            total_amount / total_mwh if has_mwh and total_mwh not in (None, 0) else None
        ),
    }


def _merge_market_section_dicts(sections: list[dict]) -> dict:
    if not sections:
        return {}

    purchases = _merge_summary_value_dicts(
        [section.get("purchases", {}) for section in sections if section.get("purchases")]
    )
    sales = _merge_summary_value_dicts(
        [section.get("sales", {}) for section in sections if section.get("sales")]
    )

    purchase_mwh = purchases["mwh"] or 0.0
    purchase_amount = purchases["amount_usd"] or 0.0
    sales_mwh = sales["mwh"] or 0.0
    sales_amount = sales["amount_usd"] or 0.0
    net_mwh = purchase_mwh - sales_mwh
    net_amount = purchase_amount + sales_amount

    has_purchases = purchase_mwh != 0.0 or purchase_amount != 0.0
    has_sales = sales_mwh != 0.0 or sales_amount != 0.0

    if has_purchases and (not has_sales or abs(purchase_amount) >= abs(sales_amount)):
        direction = "PURCHASE"
        display_mwh = purchase_mwh
        display_amount = purchase_amount
    elif has_sales:
        direction = "SALE"
        display_mwh = sales_mwh
        display_amount = sales_amount
    else:
        direction = "NONE"
        display_mwh = max(purchase_mwh, sales_mwh)
        display_amount = max(purchase_amount, sales_amount)

    return {
        "direction": direction,
        "mwh": display_mwh,
        "amount_usd": display_amount,
        "average_price_usd_per_mwh": (
            display_amount / display_mwh if display_mwh not in (None, 0) else None
        ),
        "purchases": purchases,
        "sales": sales,
        "net_mwh": net_mwh,
        "net_amount_usd": net_amount,
        "net_average_price_usd_per_mwh": (
            abs(net_amount) / abs(net_mwh) if net_mwh not in (None, 0) else None
        ),
    }


def _merge_trading_invoice_records(records: list[dict]) -> list[dict]:
    summary_records = [record for record in records if not _is_hourly_detail_record(record)]
    hourly_records = [record for record in records if _is_hourly_detail_record(record)]

    if not summary_records:
        raise RuntimeError("Could not find a trading invoice summary record to merge.")

    merged_summary = deepcopy(summary_records[0])
    merged_summary_sources = []
    merged_unset_fields = set()
    present_market_sections = set()

    for record in summary_records:
        source_file = record.get("source_file")
        if source_file:
            merged_summary_sources.append(source_file)
        unset_fields = record.get(INTERNAL_UNSET_FIELDS_FIELD)
        if unset_fields:
            merged_unset_fields.update(unset_fields)
        for market in ("fpm_m", "fpm_w", "dam", "idm"):
            if record.get(market):
                present_market_sections.add(market)

    if merged_summary.get("timestamp") is None:
        merged_summary["timestamp"] = summary_records[0].get("timestamp")
    merged_summary["delivery_date"] = summary_records[0]["delivery_date"]

    for field in (
        "market_turnover_usd",
        "net_amount_traded_usd",
        "admin_fee_mwh",
        "admin_fee_usd",
        "wheeling_fee_usd",
        "losses_fee_usd",
        "total_fees_usd",
        "total_amount_due_usd",
        "gross_total_mwh",
        "gross_total_amount_usd",
        "total_expenditure_usd",
        "sapp_net_turnover_usd",
    ):
        values = [record.get(field) for record in summary_records if record.get(field) is not None]
        merged_summary[field] = sum(values) if values else None

    merged_summary["gross_average_price_usd_per_mwh"] = (
        merged_summary["gross_total_amount_usd"] / merged_summary["gross_total_mwh"]
        if merged_summary.get("gross_total_mwh") not in (None, 0)
        else None
    )

    merged_summary["total_purchases"] = _merge_summary_value_dicts(
        [record.get("total_purchases", {}) for record in summary_records if record.get("total_purchases")]
    )
    merged_summary["total_sales"] = _merge_summary_value_dicts(
        [record.get("total_sales", {}) for record in summary_records if record.get("total_sales")]
    )

    for market in ("fpm_m", "fpm_w", "dam", "idm"):
        market_sections = [record.get(market) for record in summary_records if record.get(market)]
        if market_sections:
            merged_summary[market] = _merge_market_section_dicts(market_sections)
        else:
            merged_summary.pop(market, None)

    balancing_market = {}
    for key in {
        nested_key
        for record in summary_records
        for nested_key in (record.get("balancing_market") or {}).keys()
    }:
        nested_values = [
            record.get("balancing_market", {}).get(key)
            for record in summary_records
            if record.get("balancing_market", {}).get(key)
        ]
        if nested_values:
            balancing_market[key] = _merge_summary_value_dicts(nested_values)
    merged_summary["balancing_market"] = balancing_market

    merged_summary["confirmed_trade_type"] = (
        "PURCHASE"
        if (merged_summary.get("net_amount_traded_usd") or 0.0) > 0
        else "SALE"
        if (merged_summary.get("net_amount_traded_usd") or 0.0) < 0
        else "NONE"
    )

    merged_summary_sources = list(dict.fromkeys(merged_summary_sources))
    merged_summary["source_file"] = " + ".join(merged_summary_sources) if merged_summary_sources else None
    if merged_summary.get("metadata") is not None:
        merged_summary["metadata"] = {
            **merged_summary["metadata"],
            "source_file": merged_summary["source_file"],
        }

    merged_unset_fields -= present_market_sections
    merged_summary[INTERNAL_UNSET_FIELDS_FIELD] = tuple(sorted(merged_unset_fields))

    merged_hourly: dict[tuple[str, str, int], dict] = {}
    for record in hourly_records:
        key = (record["delivery_date"], record["market"], record["hour"])
        existing = merged_hourly.get(key)
        if existing is None:
            merged_hourly[key] = deepcopy(record)
            continue

        for field in (
            "traded_purchases_mwh",
            "traded_sales_mwh",
            "purchase_turnover_usd",
            "sale_turnover_usd",
            "admin_fees_usd",
            "wheeling_cost_usd",
        ):
            values = [existing.get(field), record.get(field)]
            values = [value for value in values if value is not None]
            existing[field] = sum(values) if values else None

        if existing.get("price_usd_per_mwh") is None:
            existing["price_usd_per_mwh"] = record.get("price_usd_per_mwh")
        if existing.get("hour_label") is None:
            existing["hour_label"] = record.get("hour_label")
        if existing.get("timestamp") is None:
            existing["timestamp"] = record.get("timestamp")
        if existing.get("source_file"):
            existing["source_file"] = f"{existing['source_file']} + {record.get('source_file')}"
        else:
            existing["source_file"] = record.get("source_file")
        if existing.get("metadata") is not None and record.get("metadata") is not None:
            existing["metadata"] = {
                **existing["metadata"],
                "source_file": existing["source_file"],
            }

    merged_records = [merged_summary, *merged_hourly.values()]
    return sorted(
        merged_records,
        key=lambda record: (
            record["delivery_date"],
            record.get("market", ""),
            record.get("hour", 0),
        ),
    )


def _extract_invoice_file(file_path: Path) -> tuple[str, list[dict]]:
    records = extract_trading_invoice_results(
        file_path,
        data_source=TRADING_INVOICE_RESULTS_JOB.data_source,
    )
    delivery_dates = sorted({record["delivery_date"] for record in records})
    return file_path.name, records


def import_trading_invoice_credit_notes_from_folder(
    folder: Path,
    recursive: bool = False,
    dry_run: bool = False,
    continue_on_error: bool = True,
) -> dict:
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Folder not found or not a directory: {folder}")

    files = _iter_invoice_files(folder, recursive=recursive)
    if not files:
        ignored_pdfs = _iter_ignored_pdf_files(folder, recursive=recursive)
        if ignored_pdfs:
            pdf_list = ", ".join(path.name for path in ignored_pdfs[:5])
            extra = "" if len(ignored_pdfs) <= 5 else f" ... (+{len(ignored_pdfs) - 5} more)"
            raise ValueError(
                f"No Excel invoice workbooks found in folder: {folder}. "
                f"PDF files were found but are ignored: {pdf_list}{extra}"
            )
        raise ValueError(f"No invoice workbooks found in folder: {folder}")

    started_at = time.perf_counter()
    results: list[dict] = []
    import_started = False
    records_by_delivery_date: dict[str, list[dict]] = {}

    try:
        if not dry_run:
            connect_db()
            import_started = True

        print(
            f"[run] Importing {len(files)} invoice workbook(s) from {folder} "
            f"(recursive={recursive}, dry_run={dry_run})"
        )

        for index, file_path in enumerate(files, start=1):
            relative_name = file_path.relative_to(folder.resolve())
            print(f"[{index}/{len(files)}] Processing {relative_name}")
            try:
                file_name, records = _extract_invoice_file(file_path)
                delivery_dates = sorted({record["delivery_date"] for record in records})
                file_result = {
                    "file": file_name,
                    "status": "dry_run" if dry_run else "parsed",
                    "record_count": len(records),
                    "delivery_dates": delivery_dates,
                    "imported": 0,
                    "updated": 0,
                    "source_file": file_name,
                }
                results.append(file_result)
                for delivery_date in delivery_dates:
                    records_by_delivery_date.setdefault(delivery_date, []).extend(records)
                print(
                    f"  -> parsed file={file_name} "
                    f"delivery_dates={delivery_dates} rows={len(records)}"
                )
            except Exception as exc:
                failure = {
                    "file": file_path.name,
                    "status": "failed",
                    "error": str(exc),
                }
                results.append(failure)
                print(f"  -> failed file={file_path.name} error={exc}")
                if not continue_on_error:
                    raise

        merged_records: list[dict] = []
        for delivery_date in sorted(records_by_delivery_date):
            merged_records.extend(_merge_trading_invoice_records(records_by_delivery_date[delivery_date]))

        elapsed_seconds = time.perf_counter() - started_at
        successful_results = [result for result in results if result["status"] != "failed"]
        failed_results = [result for result in results if result["status"] == "failed"]
        total_rows = len(merged_records)
        total_imported = 0
        total_updated = 0
        delivery_dates = sorted(
            {
                delivery_date
                for result in successful_results
                for delivery_date in result.get("delivery_dates", [])
            }
        )

        if merged_records and not dry_run:
            result = store_records_in_database(merged_records, TRADING_INVOICE_RESULTS_JOB)
            total_imported = result.get("imported", 0)
            total_updated = result.get("updated", 0)
            print(
                f"[db] merged summary rows={sum(1 for r in merged_records if 'hour' not in r)} "
                f"hourly rows={sum(1 for r in merged_records if 'hour' in r)}"
            )

        summary = {
            "folder": str(folder),
            "recursive": recursive,
            "dry_run": dry_run,
            "file_count": len(files),
            "successful_files": len(successful_results),
            "failed_files": len(failed_results),
            "row_count": total_rows,
            "imported": total_imported,
            "updated": total_updated,
            "delivery_dates": delivery_dates,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "results": results,
        }
        print(
            f"[done] files={len(files)} success={len(successful_results)} failed={len(failed_results)} "
            f"rows={total_rows} imported={total_imported} updated={total_updated} "
            f"elapsed={elapsed_seconds:.2f}s"
        )
        return summary
    finally:
        if import_started:
            disconnect_db()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import local SAPP trading invoice / credit note files into MongoDB."
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Folder containing invoice workbooks to import.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search for workbook files recursively under the folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and report what would be imported without writing to MongoDB.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately if one workbook fails to parse or import.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    folder = Path(args.folder).expanduser()
    import_trading_invoice_credit_notes_from_folder(
        folder=folder,
        recursive=args.recursive,
        dry_run=args.dry_run,
        continue_on_error=not args.stop_on_error,
    )


if __name__ == "__main__":
    main()
