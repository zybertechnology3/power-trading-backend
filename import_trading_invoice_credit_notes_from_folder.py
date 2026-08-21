from __future__ import annotations

import argparse
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
    TRADING_INVOICE_RESULTS_JOB,
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


def _import_one_file(file_path: Path, dry_run: bool) -> dict:
    records = extract_trading_invoice_results(
        file_path,
        data_source=TRADING_INVOICE_RESULTS_JOB.data_source,
    )
    delivery_dates = sorted({record["delivery_date"] for record in records})
    record_count = len(records)

    if dry_run:
        return {
            "file": file_path.name,
            "status": "dry_run",
            "record_count": record_count,
            "delivery_dates": delivery_dates,
            "imported": 0,
            "updated": 0,
            "source_file": file_path.name,
        }

    result = store_records_in_database(records, TRADING_INVOICE_RESULTS_JOB)
    result.update(
        {
            "file": file_path.name,
            "status": "success",
            "record_count": record_count,
            "delivery_dates": delivery_dates,
        }
    )
    return result


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
                result = _import_one_file(file_path, dry_run=dry_run)
                results.append(result)
                print(
                    f"  -> {result['status']} file={result['file']} "
                    f"delivery_dates={result['delivery_dates']} rows={result['record_count']} "
                    f"imported={result.get('imported', 0)} updated={result.get('updated', 0)}"
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

        elapsed_seconds = time.perf_counter() - started_at
        successful_results = [result for result in results if result["status"] != "failed"]
        failed_results = [result for result in results if result["status"] == "failed"]
        total_rows = sum(result.get("record_count", 0) for result in successful_results)
        total_imported = sum(result.get("imported", 0) for result in successful_results)
        total_updated = sum(result.get("updated", 0) for result in successful_results)
        delivery_dates = sorted(
            {
                delivery_date
                for result in successful_results
                for delivery_date in result.get("delivery_dates", [])
            }
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
