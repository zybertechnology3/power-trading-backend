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
import imaplib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from email import policy
from email.message import Message
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

from app.db.database import get_db
from app.services.sapp_sync_notifier import record_external_run
from sapp_scraper import (
    CONSTRAINED_AREA_RESULTS_JOB,
    INTERNAL_COLLECTION_FIELD,
    INTERNAL_UNIQUE_KEY_FIELDS_FIELD,
    PARTICIPANT_PORTFOLIO_RESULTS_JOB,
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
DEFAULT_SUBJECT_SEARCH_TERMS = (
    "MTP - Trading Invoice / Credit Note",
    "MTP - DAM - Participant Portfolio Results",
    "MTP - DAM - Unconstrained Results",
    "MTP - DAM - Constrained Area Results",
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


def _job_for_attachment(filename: str):
    """Identify supported SAPP workbook types using the attachment filename."""
    normalized = re.sub(r"[^a-z0-9]+", "", filename.lower())
    if "tradinginvoicecreditnote" in normalized:
        return TRADING_INVOICE_RESULTS_JOB
    if "unconstrainedresults" in normalized:
        return UNCONSTRAINED_AREA_RESULTS_JOB
    if "constrainedarearesults" in normalized:
        return CONSTRAINED_AREA_RESULTS_JOB
    if "participant" in normalized and "portfolio" in normalized and "dam" in normalized:
        return PARTICIPANT_PORTFOLIO_RESULTS_JOB
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
                            CONSTRAINED_AREA_RESULTS_JOB.name: "dam",
                            UNCONSTRAINED_AREA_RESULTS_JOB.name: "dam",
                        }.get(job.name, job.name),
                        job=job.name,
                        status="success",
                        start_date=delivery_date.isoformat(),
                        end_date=delivery_date.isoformat(),
                        result=result,
                        source="email_importer",
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
                try:
                    message = self._fetch_message(mailbox, uid)
                    subject = str(message.get("subject", ""))
                    result = self._process_message(uid, message)
                    if self.mark_seen:
                        mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")
                    results.append(result)
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
                    # A message is considered checked even when it has no
                    # Excel attachment or contains an invalid workbook.
                    # This prevents every polling cycle from retrying it.
                    processed.add(uid)
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
        except Exception:
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
