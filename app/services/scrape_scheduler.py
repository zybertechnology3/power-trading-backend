"""In-process scrape scheduling and job tracking."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.db.database import get_db
from area_results_test_scraper import run as run_dam_standalone
from bm_atc_test_scraper import run as run_bm_atc_standalone
from fpm_m_test import run as run_fpm_m_standalone
from fpm_w_test import run as run_fpm_w_standalone
from sapp_scraper import (
    PARTICIPANT_PORTFOLIO_BUNDLE_JOB_NAMES,
    get_extraction_job,
    run_extraction_job,
    run_extraction_job_for_date_range,
    run_portfolio_extraction_bundle,
    run_portfolio_extraction_bundle_for_date_range,
)


@dataclass(frozen=True)
class ScrapeJobDefinition:
    name: str
    description: str
    runner: Callable[[dict[str, Any], datetime], dict[str, Any]]
    default_params: dict[str, Any] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_in_tz(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def _coerce_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"Invalid date value: {value!r}")


def _resolve_relative_date(mode: Any, now: datetime) -> date:
    if isinstance(mode, date):
        return mode
    if isinstance(mode, datetime):
        return mode.date()
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("Date mode must be a non-empty string")

    normalized = mode.strip().lower()
    if normalized == "today":
        return now.date()
    if normalized == "yesterday":
        return (now - timedelta(days=1)).date()
    if normalized == "week_start":
        return now.date() - timedelta(days=now.weekday())
    if normalized == "month_start":
        return date(now.year, now.month, 1)
    if normalized.startswith("offset:"):
        return (now.date() + timedelta(days=int(normalized.split(":", 1)[1])) )
    if normalized.startswith("date:"):
        return date.fromisoformat(normalized.split(":", 1)[1])
    return date.fromisoformat(normalized)


def _resolve_date_param(params: dict[str, Any], key: str, now: datetime, default: Optional[date] = None) -> Optional[date]:
    if key in params and params[key] not in (None, ""):
        return _coerce_date(params[key])
    mode_key = f"{key}_mode"
    if mode_key in params and params[mode_key] not in (None, ""):
        return _resolve_relative_date(params[mode_key], now)
    return default


def _coerce_week_of_month(value: Any) -> Optional[int | str]:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "last":
            return "last"
        if normalized.isdigit():
            return int(normalized)
    raise ValueError("week_of_month must be one of: 1, 2, 3, 4, 5, or 'last'")


def _week_of_month(day: date) -> int:
    return ((day.day - 1) // 7) + 1


def _is_last_weekday_of_month(day: date, weekday: int) -> bool:
    if day.weekday() != weekday:
        return False
    return (day + timedelta(days=7)).month != day.month


def _matches_schedule_date(entry: dict[str, Any], day: date) -> bool:
    days_of_week = entry.get("days_of_week")
    if days_of_week is not None and day.weekday() not in set(days_of_week):
        return False

    days_of_month = entry.get("days_of_month")
    if days_of_month is not None and day.day not in set(days_of_month):
        return False

    week_of_month = entry.get("week_of_month")
    if week_of_month is None:
        return True

    if week_of_month == "last":
        if not days_of_week:
            return False
        return any(_is_last_weekday_of_month(day, weekday) for weekday in set(days_of_week))

    return _week_of_month(day) == int(week_of_month)


def _result_summary(result: Any) -> Any:
    if isinstance(result, dict):
        summary: dict[str, Any] = {}
        for key, value in result.items():
            if key in {"results", "days", "related_results", "storage_jobs", "chunk_results"}:
                summary[key] = {"count": len(value)} if isinstance(value, list) else value
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
            elif isinstance(value, dict):
                summary[key] = _result_summary(value)
        return summary
    if isinstance(result, list):
        return {"count": len(result)}
    return result


def _run_bm_atc(params: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = dict(params or {})
    delivery_date = _resolve_date_param(payload, "delivery_date", now, default=now.date())
    start_date = _resolve_date_param(payload, "start_date", now)
    end_date = _resolve_date_param(payload, "end_date", now)
    if start_date is None and end_date is None:
        start_date = delivery_date
        end_date = delivery_date
    return run_bm_atc_standalone(
        delivery_date=delivery_date,
        start_date=start_date,
        end_date=end_date,
        area=payload.get("area", "All Areas"),
        timeout=int(payload.get("timeout", 20)),
        headless=True,
        observe_seconds=0,
        debug=bool(payload.get("debug", False)),
    )


def _run_dam(params: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = dict(params or {})
    delivery_date = _resolve_date_param(payload, "delivery_date", now, default=now.date())
    start_date = _resolve_date_param(payload, "start_date", now, default=delivery_date)
    end_date = _resolve_date_param(payload, "end_date", now, default=delivery_date)
    return run_dam_standalone(
        start_date=start_date or delivery_date,
        end_date=end_date or delivery_date,
        timeout=int(payload.get("timeout", 20)),
        headless=True,
        observe_seconds=0,
        scrape_scope=str(payload.get("scrape_scope", "all")),
    )


def _run_fpm_w(params: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = dict(params or {})
    delivery_date = _resolve_date_param(payload, "delivery_date", now, default=now.date())
    start_date = _resolve_date_param(payload, "start_date", now, default=delivery_date)
    end_date = _resolve_date_param(payload, "end_date", now, default=delivery_date)
    return run_fpm_w_standalone(
        start_date=start_date or delivery_date,
        end_date=end_date or delivery_date,
        timeout=int(payload.get("timeout", 20)),
        headless=True,
        observe_seconds=0,
    )


def _run_fpm_m(params: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = dict(params or {})
    delivery_date = _resolve_date_param(payload, "delivery_date", now, default=now.date())
    start_date = _resolve_date_param(payload, "start_date", now, default=delivery_date)
    end_date = _resolve_date_param(payload, "end_date", now, default=delivery_date)
    return run_fpm_m_standalone(
        start_date=start_date or delivery_date,
        end_date=end_date or delivery_date,
        timeout=int(payload.get("timeout", 20)),
        headless=True,
        observe_seconds=0,
    )


def _run_core_sapp_job(job_name: str, params: dict[str, Any], now: datetime) -> dict[str, Any]:
    payload = dict(params or {})
    delivery_date = _resolve_date_param(payload, "delivery_date", now, default=now.date())
    start_date = _resolve_date_param(payload, "start_date", now)
    end_date = _resolve_date_param(payload, "end_date", now)
    page_start = int(payload.get("page_start", 1))

    if job_name == "participant_portfolio_results":
        if start_date and end_date:
            return run_portfolio_extraction_bundle_for_date_range(
                start_date=start_date,
                end_date=end_date,
                continue_on_error=bool(payload.get("continue_on_error", True)),
                page_start=page_start,
                headless=True,
            )
        return run_portfolio_extraction_bundle(
            delivery_date=delivery_date,
            page_start=page_start,
            headless=True,
        )

    job = get_extraction_job(job_name)
    if start_date and end_date:
        return run_extraction_job_for_date_range(
            job,
            start_date=start_date,
            end_date=end_date,
            continue_on_error=bool(payload.get("continue_on_error", True)),
            page_start=page_start,
            headless=True,
        )
    return run_extraction_job(
        job,
        delivery_date=delivery_date,
        page_start=page_start,
        headless=True,
    )


JOB_DEFINITIONS: dict[str, ScrapeJobDefinition] = {
    "bm_atc": ScrapeJobDefinition(
        name="bm_atc",
        description="BM ATC standalone scraper",
        runner=_run_bm_atc,
        default_params={"area": "All Areas", "timeout": 20, "debug": False},
    ),
    "dam_area_results": ScrapeJobDefinition(
        name="dam_area_results",
        description="DAM constrained/unconstrained standalone scraper",
        runner=_run_dam,
        default_params={"scrape_scope": "all", "timeout": 20},
    ),
    "fpm_w": ScrapeJobDefinition(
        name="fpm_w",
        description="FPM-W standalone scraper",
        runner=_run_fpm_w,
        default_params={"timeout": 20},
    ),
    "fpm_m": ScrapeJobDefinition(
        name="fpm_m",
        description="FPM-M standalone scraper",
        runner=_run_fpm_m,
        default_params={"timeout": 20},
    ),
}

for job_name in (
    "constrained_area_results",
    "unconstrained_area_results",
    "trading_invoice_credit_note",
    *PARTICIPANT_PORTFOLIO_BUNDLE_JOB_NAMES,
):
    JOB_DEFINITIONS[job_name] = ScrapeJobDefinition(
        name=job_name,
        description=f"SAPP scraper job {job_name}",
        runner=lambda params, now, job_name=job_name: _run_core_sapp_job(job_name, params, now),
        default_params={"timeout": 20},
    )


def _parse_schedule_entries(raw_value: str) -> list[dict[str, Any]]:
    if not raw_value.strip():
        return []
    parsed = json.loads(raw_value)
    if isinstance(parsed, dict):
        parsed = parsed.get("jobs", [])
    if not isinstance(parsed, list):
        raise ValueError("SCRAPE_SCHEDULES_JSON must be a JSON list or an object with a jobs list")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Schedule entry {index} must be an object")
        job_name = str(item.get("job_name", "")).strip()
        time_value = str(item.get("time", "")).strip()
        if not job_name:
            raise ValueError(f"Schedule entry {index} is missing job_name")
        if job_name not in JOB_DEFINITIONS:
            raise ValueError(
                f"Schedule entry {index} references unknown job_name '{job_name}'. "
                f"Available jobs: {', '.join(sorted(JOB_DEFINITIONS))}"
            )
        if not time_value:
            raise ValueError(f"Schedule entry {index} is missing time")
        entries.append(
            {
                "job_name": job_name,
                "time": time.fromisoformat(time_value),
                "days_of_week": item.get("days_of_week"),
                "days_of_month": item.get("days_of_month"),
                "week_of_month": _coerce_week_of_month(item.get("week_of_month")),
                "enabled": bool(item.get("enabled", True)),
                "params": dict(item.get("params") or {}),
            }
        )
    return entries


class ScrapeJobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrape-job")
        self._schedule_entries: list[dict[str, Any]] = []
        try:
            self._tz = ZoneInfo(settings.SCRAPE_SCHEDULER_TIMEZONE)
        except ZoneInfoNotFoundError:
            self._tz = ZoneInfo("UTC")

    def reload_schedule(self) -> list[dict[str, Any]]:
        entries = _parse_schedule_entries(settings.SCRAPE_SCHEDULES_JSON)
        with self._lock:
            self._schedule_entries = entries
        return entries

    def start(self) -> None:
        if not settings.SCRAPE_SCHEDULER_ENABLED:
            self.reload_schedule()
            return
        self.reload_schedule()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="scrape-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=5)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _scheduler_loop(self) -> None:
        while not self._stop_event.wait(settings.SCRAPE_SCHEDULER_POLL_SECONDS):
            try:
                self._dispatch_due_jobs()
            except Exception:
                continue

    def _dispatch_due_jobs(self) -> None:
        now = datetime.now(self._tz)
        with self._lock:
            entries = list(self._schedule_entries)

        for entry in entries:
            if not entry.get("enabled", True):
                continue
            if not self._schedule_entry_due(entry, now):
                continue
            schedule_key = self._schedule_key(entry, now)
            if self._run_exists(schedule_key):
                continue
            self.trigger(
                entry["job_name"],
                params=entry.get("params") or {},
                trigger_source="schedule",
                scheduled_for=datetime.combine(now.date(), entry["time"], tzinfo=self._tz),
                schedule_key=schedule_key,
            )

    def _schedule_entry_due(self, entry: dict[str, Any], now: datetime) -> bool:
        if now.time() < entry["time"]:
            return False
        return _matches_schedule_date(entry, now.date())

    def _schedule_key(self, entry: dict[str, Any], now: datetime) -> str:
        return f"{entry['job_name']}:{now.date().isoformat()}:{entry['time'].isoformat()}"

    def _next_run_for_entry(self, entry: dict[str, Any], now: datetime) -> Optional[datetime]:
        candidate_day = now.date()
        for _ in range(370):
            if _matches_schedule_date(entry, candidate_day):
                candidate_run = datetime.combine(candidate_day, entry["time"], tzinfo=self._tz)
                if candidate_run >= now:
                    return candidate_run
            candidate_day = candidate_day + timedelta(days=1)
        return None

    def _run_exists(self, schedule_key: str) -> bool:
        db = get_db()
        return db["sapp_scrape_job_runs"].count_documents({"schedule_key": schedule_key}) > 0

    def trigger(
        self,
        job_name: str,
        params: Optional[dict[str, Any]] = None,
        trigger_source: str = "manual",
        scheduled_for: Optional[datetime] = None,
        schedule_key: Optional[str] = None,
    ) -> dict[str, Any]:
        if job_name not in JOB_DEFINITIONS:
            raise ValueError(
                f"Unknown scrape job '{job_name}'. Available jobs: {', '.join(sorted(JOB_DEFINITIONS))}"
            )

        run_id = uuid.uuid4().hex
        now = _utcnow()
        db = get_db()
        run_document = {
            "run_id": run_id,
            "job_name": job_name,
            "status": "queued",
            "trigger_source": trigger_source,
            "scheduled_for": scheduled_for,
            "params": dict(params or {}),
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "result_summary": None,
            "error": None,
        }
        if schedule_key is not None:
            run_document["schedule_key"] = schedule_key
        db["sapp_scrape_job_runs"].insert_one(run_document)

        self._executor.submit(
            self._execute_run,
            run_id,
            job_name,
            dict(params or {}),
        )

        return self.get_run(run_id)

    def _execute_run(self, run_id: str, job_name: str, params: dict[str, Any]) -> None:
        db = get_db()
        now = _utcnow()
        db["sapp_scrape_job_runs"].update_one(
            {"run_id": run_id},
            {"$set": {"status": "running", "started_at": now, "updated_at": now}},
        )
        try:
            result = JOB_DEFINITIONS[job_name].runner(params, datetime.now(self._tz))
            finished_at = _utcnow()
            db["sapp_scrape_job_runs"].update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "status": "succeeded",
                        "finished_at": finished_at,
                        "updated_at": finished_at,
                        "result_summary": _result_summary(result),
                        "error": None,
                    }
                },
            )
        except Exception as exc:
            finished_at = _utcnow()
            db["sapp_scrape_job_runs"].update_one(
                {"run_id": run_id},
                {
                    "$set": {
                        "status": "failed",
                        "finished_at": finished_at,
                        "updated_at": finished_at,
                        "error": str(exc),
                    }
                },
            )

    def list_runs(
        self,
        job_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = get_db()
        query: dict[str, Any] = {}
        if job_name:
            query["job_name"] = job_name
        if status:
            query["status"] = status
        records = list(
            db["sapp_scrape_job_runs"]
            .find(query)
            .sort([("created_at", -1)])
            .limit(limit)
        )
        return [self._serialize_run(record) for record in records]

    def get_run(self, run_id: str) -> dict[str, Any]:
        db = get_db()
        record = db["sapp_scrape_job_runs"].find_one({"run_id": run_id})
        if not record:
            raise KeyError(run_id)
        return self._serialize_run(record)

    def status(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._schedule_entries)
        now = datetime.now(self._tz)
        upcoming = []
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            scheduled_for = self._next_run_for_entry(entry, now)
            if scheduled_for is None:
                continue
            upcoming.append(
                {
                    "job_name": entry["job_name"],
                    "time": entry["time"].isoformat(),
                    "days_of_week": entry.get("days_of_week"),
                    "days_of_month": entry.get("days_of_month"),
                    "week_of_month": entry.get("week_of_month"),
                    "enabled": entry.get("enabled", True),
                    "next_run_at": scheduled_for.isoformat(),
                    "params": entry.get("params") or {},
                }
            )
        return {
            "enabled": settings.SCRAPE_SCHEDULER_ENABLED,
            "timezone": settings.SCRAPE_SCHEDULER_TIMEZONE,
            "poll_seconds": settings.SCRAPE_SCHEDULER_POLL_SECONDS,
            "job_count": len(entries),
            "jobs": upcoming,
            "supported_jobs": self.supported_jobs(),
            "recent_runs": self.list_runs(limit=10),
            "running_runs": self.list_runs(status="running", limit=10),
            "queued_runs": self.list_runs(status="queued", limit=10),
        }

    @staticmethod
    def _serialize_run(record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload["_id"] = str(payload["_id"])
        for key in ("created_at", "updated_at", "started_at", "finished_at", "scheduled_for"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload

    @staticmethod
    def supported_jobs() -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "default_params": definition.default_params,
            }
            for definition in JOB_DEFINITIONS.values()
        ]


_SCRAPE_JOB_MANAGER = ScrapeJobManager()


def start_scrape_scheduler() -> None:
    _SCRAPE_JOB_MANAGER.start()


def stop_scrape_scheduler() -> None:
    _SCRAPE_JOB_MANAGER.stop()


def reload_scrape_scheduler() -> list[dict[str, Any]]:
    return _SCRAPE_JOB_MANAGER.reload_schedule()


def trigger_scrape_job(
    job_name: str,
    params: Optional[dict[str, Any]] = None,
    trigger_source: str = "manual",
) -> dict[str, Any]:
    return _SCRAPE_JOB_MANAGER.trigger(job_name, params=params, trigger_source=trigger_source)


def get_scrape_scheduler_status() -> dict[str, Any]:
    return _SCRAPE_JOB_MANAGER.status()


def list_scrape_job_runs(
    job_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return _SCRAPE_JOB_MANAGER.list_runs(job_name=job_name, status=status, limit=limit)


def get_scrape_job_run(run_id: str) -> dict[str, Any]:
    return _SCRAPE_JOB_MANAGER.get_run(run_id)


def list_supported_scrape_jobs() -> list[dict[str, Any]]:
    return _SCRAPE_JOB_MANAGER.supported_jobs()
