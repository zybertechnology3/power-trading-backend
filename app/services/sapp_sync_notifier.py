"""Signed notifications and durable completion records for frontend sync."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.db.database import get_db

LOGGER = logging.getLogger("sapp.sync")
RUNS_COLLECTION = "sapp_auto_scraper_runs"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("SAPP_SYNC_NOTIFY_TIMEOUT_SECONDS", "10")))
    except ValueError:
        return 10.0


def _notification_configured() -> bool:
    return bool(
        os.getenv("SAPP_SYNC_NOTIFY_URL", "").strip()
        and os.getenv("SAPP_SYNC_NOTIFY_SECRET", "").strip()
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def sign_payload(payload: dict[str, Any], secret: Optional[str] = None) -> str:
    signing_secret = secret or os.getenv("SAPP_SYNC_NOTIFY_SECRET", "")
    if not signing_secret:
        raise RuntimeError("SAPP_SYNC_NOTIFY_SECRET is not configured")
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        _json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def send_ready_notification(payload: dict[str, Any]) -> dict[str, Any]:
    """Send a best-effort signed webhook without failing the scrape itself."""
    url = os.getenv("SAPP_SYNC_NOTIFY_URL", "").strip()
    secret = os.getenv("SAPP_SYNC_NOTIFY_SECRET", "").strip()
    if not url or not secret:
        return {"status": "disabled", "reason": "notify URL or secret is not configured"}

    body = _json_bytes(payload)
    signature = sign_payload(payload, secret)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "power-trading-backend-sapp-sync/1",
        "X-SAPP-Event-Id": str(payload.get("event_id", "")),
        "X-SAPP-Signature": signature,
    }
    try:
        response = httpx.post(
            url,
            content=body,
            headers=headers,
            timeout=_timeout_seconds(),
        )
        response.raise_for_status()
        return {"status": "sent", "http_status": response.status_code}
    except Exception as exc:
        LOGGER.warning("SAPP sync webhook failed: %s", exc)
        return {"status": "failed", "error": str(exc)}


def build_ready_payload(
    *,
    run_id: str,
    dataset_id: str,
    job: str,
    status: str,
    started_at: Any,
    finished_at: Any,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    result: Any = None,
    source: str = "backend",
) -> dict[str, Any]:
    return {
        "event": "sapp.dataset.ready" if status == "success" else "sapp.dataset.failed",
        "event_id": run_id,
        "run_id": run_id,
        "dataset_id": dataset_id,
        "job": job,
        "status": status,
        "source": source,
        "started_at": started_at,
        "finished_at": finished_at,
        "date_range": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "result": result or {},
    }


def record_external_run(
    *,
    run_id: str,
    dataset_id: str,
    job: str,
    status: str,
    start_date: Optional[str],
    end_date: Optional[str],
    result: Any = None,
    error: Optional[str] = None,
    source: str = "external_importer",
) -> dict[str, Any]:
    """Record an importer completion and notify the frontend."""
    finished_at = utc_now()
    payload = build_ready_payload(
        run_id=run_id,
        dataset_id=dataset_id,
        job=job,
        status=status,
        started_at=finished_at,
        finished_at=finished_at,
        start_date=start_date,
        end_date=end_date,
        result=result,
        source=source,
    )
    document = {
        "run_id": run_id,
        "scheduler_key": f"external:{run_id}",
        "job": job,
        "dataset_id": dataset_id,
        "status": status,
        "source": source,
        "started_at": finished_at,
        "finished_at": finished_at,
        "start_date": start_date,
        "end_date": end_date,
        "result": result or {},
    }
    if error:
        document["error"] = error
    get_db()[RUNS_COLLECTION].update_one(
        {"run_id": run_id},
        {"$setOnInsert": document},
        upsert=True,
    )
    notification = send_ready_notification(payload)
    get_db()[RUNS_COLLECTION].update_one(
        {"run_id": run_id},
        {"$set": {"notification": notification}},
    )
    return notification
