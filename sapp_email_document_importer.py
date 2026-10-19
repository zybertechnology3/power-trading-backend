"""Import supported SAPP Excel documents from an IMAP mailbox.

The importer deliberately reuses the existing workbook extractor and MongoDB
storage path from ``sapp_scraper.py``. It supports both the normal C1 workbook
and the optional P1 workbook, including when they arrive in separate emails.

Examples:
    python sapp_email_document_importer.py --once
    python sapp_email_document_importer.py --poll-seconds 120
"""

from __future__ import annotations

import argparse
import email
import hashlib
import html
import imaplib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import Message
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

from app.db.database import get_db
from app.services.sapp_sync_notifier import record_external_run
from sapp_scraper import (
    CONSTRAINED_AREA_RESULTS_JOB,
    FPM_M_CONSTRAINED_AREA_RESULTS_JOB,
    FPM_M_UNCONSTRAINED_AREA_RESULTS_JOB,
    FPM_W_CONSTRAINED_AREA_RESULTS_JOB,
    FPM_W_UNCONSTRAINED_AREA_RESULTS_JOB,
    INTERNAL_COLLECTION_FIELD,
    INTERNAL_UNIQUE_KEY_FIELDS_FIELD,
    PARTICIPANT_PORTFOLIO_RESULTS_JOB,
    PARTICIPANT_PORTFOLIO_FPM_W_RESULTS_JOB,
    PARTICIPANT_PORTFOLIO_FPM_M_RESULTS_JOB,
    TRADING_INVOICE_HOURLY_COLLECTION,
    TRADING_INVOICE_RESULTS_JOB,
    UNCONSTRAINED_AREA_RESULTS_JOB,
    extract_trading_invoice_job,
    merge_trading_invoice_records,
    store_records_in_database,
)

load_dotenv()

DATE_PATTERN = re.compile(r"(?:for|credit[- ]note)[^0-9]*(\d{4}[/-]\d{2}[/-]\d{2})", re.I)
DOWNLOAD_COPY_PATTERN = re.compile(r" \(\d+\)(?=\.[^.]+$)")
STATE_VERSION = 3
IDM_TRADE_JOB = "idm_trade_concluded"
IDM_TRADE_DATA_SOURCE = "SAPP_MTP_IDM_TRADE_EMAIL"
DEFAULT_SUBJECT_SEARCH_TERMS = (
    "MTP - Trading Invoice / Credit Note",
    "MTP - DAM - Participant Portfolio Results",
    "MTP - FPM-W - Participant Portfolio Results",
    "MTP - FPM-M - Participant Portfolio Results",
    "MTP - DAM - Unconstrained Results",
    "MTP - DAM - Constrained Area Results",
    "MTP - FPM-W - Constrained Area Results",
    "MTP - FPM-W - Unconstrained Results",
    "MTP - FPM-M - Constrained Area Results",
    "MTP - FPM-M - Unconstrained Results",
    "Idm Buy Trade Concluded",
    "Idm Sell Trade Concluded",
)
LOGGER = logging.getLogger("sapp.email_documents")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_date(value: str) -> date:
    return date.fromisoformat(value.replace("/", "-"))


def _message_date(subject: str, filenames: Iterable[str]) -> date:
    candidates = [subject, *filenames]
    for candidate in candidates:
        match = DATE_PATTERN.search(candidate)
        if match:
            return _parse_date(match.group(1))
        match = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", candidate)
        if match:
            return _parse_date(match.group(1))
    raise RuntimeError(f"Could not determine the credit-note delivery date from {subject!r}")


def _safe_filename(filename: str) -> str:
    return Path(filename).name.replace("\x00", "_")


def _attachment_parts(message: Message) -> list[tuple[str, bytes]]:
    attachments = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename or not filename.lower().endswith(".xlsx"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append((_safe_filename(filename), payload))
    return attachments


def _message_text(message: Message) -> str:
    """Return readable plain text from a plain-text or HTML email body."""
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            value = payload.decode(charset, errors="replace")
        except LookupError:
            value = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
            value = re.sub(r"</(?:div|li|p|tr)>", "\n", value, flags=re.I)
            value = re.sub(r"<[^>]+>", " ", value)
            value = html.unescape(value)
        parts.append(value)
    return "\n".join(parts)


def _label_value(text: str, label: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text, re.I | re.M)
    return match.group(1).strip() if match else None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def _extract_idm_trade(message: Message, uid: str) -> dict | None:
    """Extract one IDM trade-concluded email into hourly-detail fields."""
    subject = str(message.get("subject", ""))
    trade_match = re.search(
        r"\bidm\s+(buy|sell)\s+trade\s+concluded\b",
        subject,
        re.I,
    )
    if not trade_match:
        return None

    body = _message_text(message)
    quantity = _number(_label_value(body, "Quantity"))
    price_line = _label_value(body, "Price")
    price_usd = _number(
        re.search(r"\bUSD\s*([-+]?\d[\d,]*(?:\.\d+)?)", price_line or "", re.I).group(1)
        if re.search(r"\bUSD\s*([-+]?\d[\d,]*(?:\.\d+)?)", price_line or "", re.I)
        else None
    )
    price_zar = _number(
        re.search(r"\bZAR\s*([-+]?\d[\d,]*(?:\.\d+)?)", price_line or "", re.I).group(1)
        if re.search(r"\bZAR\s*([-+]?\d[\d,]*(?:\.\d+)?)", price_line or "", re.I)
        else None
    )
    delivery_value = _label_value(body, "Delivery Day")
    interval_value = _label_value(body, "Delivery Interval Hour")
    portfolio = _label_value(body, "Portfolio")
    interval_match = re.search(r"(\d{1,2})\s*[-–]\s*(\d{1,2})", interval_value or "")

    missing = []
    if quantity is None:
        missing.append("Quantity")
    if price_usd is None:
        missing.append("USD price")
    if delivery_value is None:
        missing.append("Delivery Day")
    if interval_match is None:
        missing.append("Delivery Interval Hour")
    if missing:
        raise RuntimeError(
            f"IDM trade email is missing required fields: {', '.join(missing)}"
        )

    delivery_date = _parse_date(delivery_value)
    interval_start = int(interval_match.group(1))
    interval_end = int(interval_match.group(2))
    if interval_start < 0 or interval_start > 23 or interval_end != interval_start + 1:
        raise RuntimeError(f"Invalid IDM delivery interval: {interval_value!r}")
    hour = interval_start + 1

    trade_type = trade_match.group(1).upper()
    amount_usd = quantity * price_usd
    trade_id = hashlib.sha256(
        f"{uid}|{delivery_date.isoformat()}|{hour}|{trade_type}|{portfolio or ''}".encode()
    ).hexdigest()[:24]
    return {
        "timestamp": datetime.combine(delivery_date, datetime.min.time())
        + timedelta(hours=hour - 1),
        "delivery_date": delivery_date.isoformat(),
        "market": "idm",
        "hour": hour,
        "hour_label": f"{interval_start:02d}-{interval_end:02d}",
        "price_usd_per_mwh": price_usd,
        "price_zar_per_mwh": price_zar,
        "traded_purchases_mwh": quantity if trade_type == "BUY" else None,
        "traded_sales_mwh": quantity if trade_type == "SELL" else None,
        "purchase_turnover_usd": amount_usd if trade_type == "BUY" else None,
        "sale_turnover_usd": -amount_usd if trade_type == "SELL" else None,
        "portfolio": portfolio,
        "trade_type": trade_type,
        "trade_id": trade_id,
        "data_source": IDM_TRADE_DATA_SOURCE,
        "metadata": {
            "data_source": IDM_TRADE_DATA_SOURCE,
            "source_uid": str(uid),
            "portfolio": portfolio,
            "trade_type": trade_type,
            "price_zar_per_mwh": price_zar,
            "trade_id": trade_id,
        },
        "source_file": f"email:{uid}:{subject}",
    }


def _store_idm_trade(record: dict) -> dict:
    now = datetime.now(timezone.utc)
    collection = get_db()[TRADING_INVOICE_HOURLY_COLLECTION]
    unique_filter = {
        "delivery_date": record["delivery_date"],
        "market": "idm",
        "hour": record["hour"],
    }
    existing = collection.find_one(unique_filter)
    existing_metadata = existing.get("metadata", {}) if existing else {}
    existing_trade_ids = list(existing.get("trade_ids", [])) if existing else []
    if existing and existing.get("trade_id"):
        existing_trade_ids.append(existing["trade_id"])
    existing_trade_ids = list(dict.fromkeys(existing_trade_ids))
    if record["trade_id"] in existing_trade_ids:
        return {
            "job": IDM_TRADE_JOB,
            "status": "already_imported",
            "delivery_date": record["delivery_date"],
            "market": "idm",
            "hour": record["hour"],
            "trade_type": record["trade_type"],
            "imported": 0,
            "updated": 0,
            "source_file": record["source_file"],
        }

    direct_idm_record = bool(
        existing
        and (
            existing_metadata.get("data_source") == IDM_TRADE_DATA_SOURCE
            or existing_trade_ids
        )
    )
    if direct_idm_record:
        previous_purchase_mwh = existing.get("traded_purchases_mwh") or 0.0
        previous_sale_mwh = existing.get("traded_sales_mwh") or 0.0
        purchase_mwh = previous_purchase_mwh + (record.get("traded_purchases_mwh") or 0.0)
        sale_mwh = previous_sale_mwh + (record.get("traded_sales_mwh") or 0.0)
        previous_purchase_amount = existing.get("purchase_turnover_usd") or 0.0
        previous_sale_amount = existing.get("sale_turnover_usd") or 0.0
        purchase_amount = previous_purchase_amount + (record.get("purchase_turnover_usd") or 0.0)
        sale_amount = previous_sale_amount + (record.get("sale_turnover_usd") or 0.0)
        previous_quantity = previous_purchase_mwh + previous_sale_mwh
        new_quantity = (record.get("traded_purchases_mwh") or 0.0) + (
            record.get("traded_sales_mwh") or 0.0
        )
        total_quantity = purchase_mwh + sale_mwh
        weighted_usd = (
            (existing.get("price_usd_per_mwh") or 0.0) * previous_quantity
            + (record.get("price_usd_per_mwh") or 0.0) * new_quantity
        )
        weighted_zar = (
            (existing.get("price_zar_per_mwh") or 0.0) * previous_quantity
            + (record.get("price_zar_per_mwh") or 0.0) * new_quantity
        )
        trade_type = (
            "MIXED"
            if purchase_mwh and sale_mwh
            else "BUY"
            if purchase_mwh
            else "SELL"
        )
        source_files = [
            value.strip()
            for value in str(existing.get("source_file", "")).split(" + ")
            if value.strip()
        ]
        source_files.append(record["source_file"])
        record = {
            **record,
            "price_usd_per_mwh": weighted_usd / total_quantity if total_quantity else None,
            "price_zar_per_mwh": weighted_zar / total_quantity if total_quantity else None,
            "traded_purchases_mwh": purchase_mwh or None,
            "traded_sales_mwh": sale_mwh or None,
            "purchase_turnover_usd": purchase_amount or None,
            "sale_turnover_usd": sale_amount or None,
            "trade_type": trade_type,
            "trade_ids": existing_trade_ids + [record["trade_id"]],
            "source_file": " + ".join(dict.fromkeys(source_files)),
            "metadata": {
                **existing_metadata,
                **record["metadata"],
                "idm_trade_ids": existing_trade_ids + [record["trade_id"]],
            },
        }
    else:
        record["trade_ids"] = [record["trade_id"]]
        record["metadata"] = {
            **record["metadata"],
            "idm_trade_ids": [record["trade_id"]],
        }

    result = collection.update_one(
        unique_filter,
        {
            "$set": {**record, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {
        "job": IDM_TRADE_JOB,
        "status": "success",
        "delivery_date": record["delivery_date"],
        "market": "idm",
        "hour": record["hour"],
        "trade_type": record["trade_type"],
        "imported": 1 if result.upserted_id is not None else 0,
        "updated": result.matched_count,
        "source_file": record["source_file"],
    }


def _job_for_attachment(filename: str):
    """Identify supported SAPP workbook types using the attachment filename."""
    normalized = re.sub(r"[^a-z0-9]+", "", filename.lower())
    if "tradinginvoicecreditnote" in normalized:
        return TRADING_INVOICE_RESULTS_JOB
    if "participant" in normalized and "portfolio" in normalized and "fpmw" in normalized:
        return PARTICIPANT_PORTFOLIO_FPM_W_RESULTS_JOB
    if "participant" in normalized and "portfolio" in normalized and "fpmm" in normalized:
        return PARTICIPANT_PORTFOLIO_FPM_M_RESULTS_JOB
    if "participant" in normalized and "portfolio" in normalized and "dam" in normalized:
        return PARTICIPANT_PORTFOLIO_RESULTS_JOB
    if "fpmw" in normalized and "unconstrainedresults" in normalized:
        return FPM_W_UNCONSTRAINED_AREA_RESULTS_JOB
    if "fpmw" in normalized and "constrainedarearesults" in normalized:
        return FPM_W_CONSTRAINED_AREA_RESULTS_JOB
    if "fpmm" in normalized and "unconstrainedresults" in normalized:
        return FPM_M_UNCONSTRAINED_AREA_RESULTS_JOB
    if "fpmm" in normalized and "constrainedarearesults" in normalized:
        return FPM_M_CONSTRAINED_AREA_RESULTS_JOB
    if "unconstrainedresults" in normalized:
        return UNCONSTRAINED_AREA_RESULTS_JOB
    if "constrainedarearesults" in normalized:
        return CONSTRAINED_AREA_RESULTS_JOB
    return None


def _state_path() -> Path:
    return Path(os.getenv("SAPP_EMAIL_CREDIT_NOTE_STATE_FILE", ".credit_note_email_state.json"))


def _load_state(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("state_version") != STATE_VERSION:
            LOGGER.info(
                "🔄 State file %s needs migration; performing one rescan",
                path,
            )
            return set()
        return {
            str(uid)
            for uid in value.get("checked_uids", [])
        }
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, AttributeError):
        LOGGER.warning("⚠️ Invalid state file %s; starting with an empty state", path)
        return set()


def _save_state(path: Path, processed_uids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "state_version": STATE_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "checked_uids": sorted(
                    processed_uids,
                    key=lambda value: int(value) if value.isdigit() else value,
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_names(records: Iterable[dict]) -> set[str]:
    names: set[str] = set()
    for record in records:
        source_file = record.get("source_file")
        if source_file:
            names.update(
                _canonical_source_name(Path(item.strip()).name)
                for item in str(source_file).split(" + ")
            )
    return names


def _canonical_source_name(filename: str) -> str:
    """Treat Selenium/browser download copies as the same attachment."""
    return DOWNLOAD_COPY_PATTERN.sub("", filename)


def _load_existing_invoice_records(delivery_date: date) -> list[dict]:
    db = get_db()
    date_value = delivery_date.isoformat()
    records = []
    summary = db[TRADING_INVOICE_RESULTS_JOB.collection_name].find_one(
        {"delivery_date": date_value}, {"_id": 0}
    )
    if summary:
        # These fields are managed by store_records_in_database. Keeping them
        # in an existing record conflicts with its $setOnInsert clause.
        summary.pop("created_at", None)
        summary.pop("updated_at", None)
        summary[INTERNAL_COLLECTION_FIELD] = TRADING_INVOICE_RESULTS_JOB.collection_name
        summary[INTERNAL_UNIQUE_KEY_FIELDS_FIELD] = TRADING_INVOICE_RESULTS_JOB.unique_key_fields
        records.append(summary)

    for hourly in db[TRADING_INVOICE_HOURLY_COLLECTION].find(
        {"delivery_date": date_value}, {"_id": 0}
    ):
        hourly.pop("created_at", None)
        hourly.pop("updated_at", None)
        hourly[INTERNAL_COLLECTION_FIELD] = TRADING_INVOICE_HOURLY_COLLECTION
        hourly[INTERNAL_UNIQUE_KEY_FIELDS_FIELD] = ("delivery_date", "market", "hour")
        records.append(hourly)
    return records


def _merge_and_store(records: list[dict], delivery_date: date) -> dict:
    if not records:
        raise RuntimeError("No workbook records were extracted")

    existing_records = _load_existing_invoice_records(delivery_date)
    existing_sources = _source_names(existing_records)
    new_records = [
        record
        for record in records
        if _canonical_source_name(Path(str(record.get("source_file", ""))).name)
        not in existing_sources
    ]
    if not new_records:
        return {
            "delivery_date": delivery_date.isoformat(),
            "status": "already_imported",
            "imported": 0,
            "updated": 0,
        }

    merged = merge_trading_invoice_records([*existing_records, *new_records])
    result = store_records_in_database(merged, TRADING_INVOICE_RESULTS_JOB)
    result["status"] = "success"
    result["source_files"] = sorted(_source_names(new_records))
    return result


class CreditNoteEmailImporter:
    def __init__(self, *, unseen_only: bool = False, since: str | None = None) -> None:
        self.host = os.getenv("SAPP_EMAIL_IMAP_HOST", "imap.gmail.com")
        self.port = int(os.getenv("SAPP_EMAIL_IMAP_PORT", "993"))
        self.username = os.getenv("SAPP_EMAIL_USERNAME") or os.getenv("IMAP_USERNAME")
        self.password = os.getenv("SAPP_EMAIL_PASSWORD") or os.getenv("IMAP_PASSWORD")
        self.folder = os.getenv("SAPP_EMAIL_IMAP_FOLDER", "INBOX")
        configured_subjects = os.getenv("SAPP_EMAIL_SUBJECT_SEARCH_TERMS")
        self.subject_search_terms = tuple(
            item.strip()
            for item in (configured_subjects.split(",") if configured_subjects else DEFAULT_SUBJECT_SEARCH_TERMS)
            if item.strip()
        )
        self.mark_seen = _env_bool("SAPP_EMAIL_MARK_SEEN", False)
        self.unseen_only = unseen_only
        self.since = since
        self.state_file = _state_path()
        if not self.username or not self.password:
            raise RuntimeError(
                "Set SAPP_EMAIL_USERNAME and SAPP_EMAIL_PASSWORD in .env "
                "(an app password is recommended)."
            )
        LOGGER.info(
            "Configured subject filters: %s",
            " | ".join(self.subject_search_terms),
        )

    def _search_uids(self, mailbox: imaplib.IMAP4_SSL) -> list[str]:
        criteria: list[str] = ["UNSEEN"] if self.unseen_only else ["ALL"]
        if self.since:
            since_date = _parse_date(self.since)
            criteria.extend(["SINCE", since_date.strftime("%d-%b-%Y")])
        if not self.subject_search_terms:
            raise RuntimeError("At least one email subject search term must be configured")
        if len(self.subject_search_terms) == 1:
            criteria.extend(["SUBJECT", f'"{self.subject_search_terms[0]}"'])
        else:
            # IMAP OR accepts two search keys. Build nested OR expressions so
            # the search remains correct if more supported document types are
            # added later.
            subject_search = ["SUBJECT", f'"{self.subject_search_terms[-1]}"']
            for term in reversed(self.subject_search_terms[:-1]):
                subject_search = [
                    "OR",
                    "SUBJECT",
                    f'"{term}"',
                    *subject_search,
                ]
            criteria.extend(subject_search)
        status, data = mailbox.uid("search", None, *criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {data!r}")
        raw = data[0] if data else b""
        uids = raw.decode("ascii", errors="ignore").split()
        LOGGER.debug("📨 Search returned %d relevant message(s)", len(uids))
        return uids

    @staticmethod
    def _fetch_message(mailbox: imaplib.IMAP4_SSL, uid: str) -> Message:
        status, data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(f"IMAP fetch failed for UID {uid}: {data!r}")
        raw_message = next(
            (item[1] for item in data if isinstance(item, tuple) and len(item) > 1), None
        )
        if not raw_message:
            raise RuntimeError(f"IMAP returned no message body for UID {uid}")
        return email.message_from_bytes(raw_message, policy=policy.default)

    def _process_message(self, uid: str, message: Message) -> dict:
        subject = str(message.get("subject", ""))
        LOGGER.debug("UID %s | subject=%r", uid, subject)
        idm_trade = _extract_idm_trade(message, uid)
        if idm_trade is not None:
            LOGGER.info(
                "[IDM] UID %s | %s | delivery=%s | hour=%s | quantity=%s MW | price=%s USD/MWh",
                uid,
                idm_trade["trade_type"],
                idm_trade["delivery_date"],
                idm_trade["hour"],
                idm_trade["traded_purchases_mwh"] or idm_trade["traded_sales_mwh"],
                idm_trade["price_usd_per_mwh"],
            )
            result = _store_idm_trade(idm_trade)
            if result.get("status") != "already_imported":
                result["run_id"] = f"email:{uid}:{IDM_TRADE_JOB}"
                result["notification"] = record_external_run(
                    run_id=result["run_id"],
                    dataset_id="idm_trades",
                    job=IDM_TRADE_JOB,
                    status="success",
                    start_date=idm_trade["delivery_date"],
                    end_date=idm_trade["delivery_date"],
                    result=result,
                    source="email_importer",
                )
                LOGGER.info(
                    "[IDM webhook] UID %s | status=%s%s",
                    uid,
                    result["notification"].get("status"),
                    (
                        f" | http={result['notification'].get('http_status')}"
                        if result["notification"].get("http_status") is not None
                        else ""
                    ),
                )
            else:
                LOGGER.info("UID %s | IDM trade already imported; webhook skipped", uid)
            return {
                "uid": uid,
                "subject": subject,
                "status": "success",
                "jobs": [result],
                "delivery_dates": [idm_trade["delivery_date"]],
            }
        with tempfile.TemporaryDirectory(prefix="email-credit-note-") as directory:
            download_dir = Path(directory)
            attachments = _attachment_parts(message)
            if not attachments:
                raise RuntimeError("no .xlsx attachment")
            grouped_attachments = {}
            unsupported = []
            for filename, payload in attachments:
                job = _job_for_attachment(filename)
                if job is None:
                    unsupported.append(filename)
                    continue
                grouped_attachments.setdefault(job.name, (job, []))[1].append((filename, payload))
            if unsupported:
                LOGGER.debug("UID %s: unsupported attachment(s): %s", uid, ", ".join(unsupported))
            if not grouped_attachments:
                raise RuntimeError("no supported SAPP workbook")
            LOGGER.info(
                "📎 UID %s | %d supported attachment(s): %s",
                uid,
                sum(len(item[1]) for item in grouped_attachments.values()),
                ", ".join(
                    f"{filename} ({len(payload)} bytes)"
                    for _, attachments_for_job in grouped_attachments.values()
                    for filename, payload in attachments_for_job
                ),
            )
            job_results = []
            for job, job_attachments in grouped_attachments.values():
                delivery_date = _message_date(subject, [name for name, _ in job_attachments])
                LOGGER.info(
                    "📅 UID %s | %s | delivery=%s",
                    uid,
                    job.name,
                    delivery_date.isoformat(),
                )
                records = []
                for filename, payload in job_attachments:
                    file_path = download_dir / filename
                    file_path.write_bytes(payload)
                    LOGGER.debug("UID %s | extracting %s", uid, filename)
                    records.extend(job.extractor(file_path, job))
                LOGGER.info(
                    "💾 UID %s | %s | extracted=%d | storing",
                    uid,
                    job.name,
                    len(records),
                )
                if job.name == TRADING_INVOICE_RESULTS_JOB.name:
                    result = _merge_and_store(records, delivery_date)
                else:
                    result = store_records_in_database(records, job)
                    result["status"] = "success"
                result["job"] = job.name
                if result.get("status") != "already_imported":
                    result["run_id"] = f"email:{uid}:{job.name}"
                    result["notification"] = record_external_run(
                        run_id=result["run_id"],
                        dataset_id={
                            TRADING_INVOICE_RESULTS_JOB.name: "credit_notes",
                            PARTICIPANT_PORTFOLIO_RESULTS_JOB.name: "portfolio_dam",
                            PARTICIPANT_PORTFOLIO_FPM_W_RESULTS_JOB.name: "portfolio_fpm_w",
                            PARTICIPANT_PORTFOLIO_FPM_M_RESULTS_JOB.name: "portfolio_fpm_m",
                            CONSTRAINED_AREA_RESULTS_JOB.name: "dam_constrained_area_results",
                            UNCONSTRAINED_AREA_RESULTS_JOB.name: "dam_unconstrained_area_results",
                            FPM_W_CONSTRAINED_AREA_RESULTS_JOB.name: "fpm_w_constrained_area_results",
                            FPM_W_UNCONSTRAINED_AREA_RESULTS_JOB.name: "fpm_w_unconstrained_area_results",
                            FPM_M_CONSTRAINED_AREA_RESULTS_JOB.name: "fpm_m_constrained_area_results",
                            FPM_M_UNCONSTRAINED_AREA_RESULTS_JOB.name: "fpm_m_unconstrained_area_results",
                        }.get(job.name, job.name),
                        job=job.name,
                        status="success",
                        start_date=delivery_date.isoformat(),
                        end_date=delivery_date.isoformat(),
                        result=result,
                        source="email_importer",
                    )
                    LOGGER.info(
                        "🔔 UID %s | webhook=%s%s",
                        uid,
                        result["notification"].get("status"),
                        (
                            f" | http={result['notification'].get('http_status')}"
                            if result["notification"].get("http_status") is not None
                            else ""
                        ),
                    )
                job_results.append(result)

            return {
                "uid": uid,
                "subject": subject,
                "status": "success",
                "jobs": job_results,
                "delivery_dates": sorted(
                    {result.get("delivery_date") for result in job_results if result.get("delivery_date")}
                ),
            }

    def scan_once(self) -> list[dict]:
        started = time.monotonic()
        processed = _load_state(self.state_file)
        results = []
        LOGGER.debug(
            "🔎 Scan started | folder=%s | unseen_only=%s | since=%s | checked=%d",
            self.folder,
            self.unseen_only,
            self.since or "none",
            len(processed),
        )
        LOGGER.debug("🎯 Subjects: %s", " | ".join(self.subject_search_terms))
        mailbox = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            LOGGER.debug("📬 Connecting to %s:%d", self.host, self.port)
            mailbox.login(self.username, self.password)
            LOGGER.debug("✅ Mailbox authenticated")
            status, _ = mailbox.select(self.folder, readonly=not self.mark_seen)
            if status != "OK":
                raise RuntimeError(f"Could not select IMAP folder {self.folder!r}")
            LOGGER.debug("📂 Folder selected: %s", self.folder)

            matching_uids = self._search_uids(mailbox)
            skipped = 0
            for uid in matching_uids:
                if uid in processed:
                    skipped += 1
                    LOGGER.debug("UID %s: already processed; skipping", uid)
                    continue
                LOGGER.info("📨 UID %s | new message", uid)
                subject = ""
                processed_successfully = False
                try:
                    message = self._fetch_message(mailbox, uid)
                    subject = str(message.get("subject", ""))
                    result = self._process_message(uid, message)
                    if self.mark_seen:
                        mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")
                    results.append(result)
                    processed_successfully = True
                    LOGGER.info(
                        "✅ UID %s | status=%s | jobs=%s | dates=%s",
                        uid,
                        result.get("status"),
                        ", ".join(job.get("job", "unknown") for job in result.get("jobs", [])),
                        ", ".join(result.get("delivery_dates", [])),
                    )
                except Exception as exc:
                    result = {"uid": uid, "subject": subject, "status": "failed", "error": str(exc)}
                    results.append(result)
                    LOGGER.error("❌ UID %s | %s", uid, exc)
                    LOGGER.debug("UID %s failure details", uid, exc_info=True)
                finally:
                    # Only successful messages are permanently acknowledged.
                    # Failed extraction or storage can be transient, so leave
                    # those UIDs eligible for retry.
                    if processed_successfully:
                        processed.add(uid)
                    else:
                        LOGGER.warning(
                            "UID %s was not marked processed; it will be retried",
                            uid,
                        )
                    _save_state(self.state_file, processed)
            LOGGER.info(
                "📊 Scan complete | found=%d | skipped=%d | handled=%d | elapsed=%.2fs",
                len(matching_uids),
                skipped,
                len(results),
                time.monotonic() - started,
            )
        finally:
            try:
                mailbox.close()
            except imaplib.IMAP4.error:
                pass
            mailbox.logout()
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Import supported SAPP Excel documents from IMAP.")
    parser.add_argument("--once", action="store_true", help="Scan once and exit instead of polling.")
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("SAPP_EMAIL_POLL_SECONDS", "120")))
    parser.add_argument("--unseen-only", action="store_true", help="Only search unread messages.")
    parser.add_argument("--since", help="Only search messages received on/after YYYY-MM-DD.")
    parser.add_argument(
        "--log-level",
        default=os.getenv("SAPP_EMAIL_LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("SAPP_EMAIL_LOG_FILE"),
        help="Optional file that receives the same logs as the terminal.",
    )
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [email-credit-notes] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    importer = CreditNoteEmailImporter(unseen_only=args.unseen_only, since=args.since)
    LOGGER.info(
        "🚀 SAPP email importer | mode=%s | interval=%ds | level=%s",
        "once" if args.once else "continuous",
        max(10, args.poll_seconds),
        args.log_level,
    )
    while True:
        try:
            importer.scan_once()
        except Exception as exc:
            LOGGER.error("Mailbox scan failed; retrying: %s", exc)
            LOGGER.error("❌ Mailbox scan failed; retrying")
            LOGGER.debug("Mailbox scan failure details", exc_info=True)
        if args.once:
            LOGGER.info("🏁 One-shot scan finished")
            return
        delay = max(10, args.poll_seconds)
        LOGGER.debug("💤 Next scan in %d seconds", delay)
        time.sleep(delay)


if __name__ == "__main__":
    main()
