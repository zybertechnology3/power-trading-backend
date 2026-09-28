"""Backend-owned SAPP scraping scheduler.

The scheduler is intentionally small and process-local. MongoDB stores run claims
and status so a container restart cannot duplicate a scheduled run for the same
local calendar slot. Selenium jobs are serialized because each job owns Firefox.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo.errors import DuplicateKeyError

from app.db.database import get_db
from area_results_test_scraper import run as run_dam_standalone
from bm_atc_test_scraper import run as run_bm_atc_standalone
from fpm_m_test import run as run_fpm_m_standalone
from fpm_w_test import run as run_fpm_w_standalone
from sapp_scraper import (
    get_extraction_job,
    run_extraction_job_for_date_range,
    run_portfolio_extraction_bundle_for_date_range,
)

load_dotenv()

RUNS_COLLECTION = "sapp_auto_scraper_runs"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _parse_time(name: str, default: str) -> tuple[int, int]:
    value = os.getenv(name, default).strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour, minute
    except (TypeError, ValueError):
        print(f"[scheduler] Invalid {name}={value!r}; using {default}")
        hour_text, minute_text = default.split(":")
        return int(hour_text), int(minute_text)


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    label: str
    schedule_env: str
    default_time: str
    frequency: str
    weekday: Optional[int] = None
    last_weekday: Optional[int] = None


JOB_DEFINITIONS = (
    ScheduledJob("portfolio_dam", "DAM participant portfolio", "SAPP_SCHEDULE_PORTFOLIO", "12:00", "daily"),
    ScheduledJob("portfolio_fpm_w", "FPM-W participant portfolio", "SAPP_SCHEDULE_PORTFOLIO", "12:00", "weekly", weekday=0),
    ScheduledJob("portfolio_fpm_m", "FPM-M participant portfolio", "SAPP_SCHEDULE_PORTFOLIO", "12:00", "monthly", last_weekday=2),
    ScheduledJob("credit_notes", "Trading invoice credit notes", "SAPP_SCHEDULE_CREDIT_NOTES", "12:00", "daily"),
    ScheduledJob("dam_area_results", "DAM area prices", "SAPP_SCHEDULE_DAM", "12:00", "daily"),
    ScheduledJob("fpm_w_area_results", "FPM-W area prices", "SAPP_SCHEDULE_FPM_W", "15:00", "weekly", weekday=3),
    ScheduledJob("fpm_m_area_results", "FPM-M area prices", "SAPP_SCHEDULE_FPM_M", "15:00", "monthly", last_weekday=2),
    ScheduledJob("bm_atc", "BM ATC", "SAPP_SCHEDULE_BM_ATC", "12:00", "daily"),
)

DATASET_IDS = {
    "portfolio_dam": "portfolio_dam",
    "portfolio_fpm_w": "portfolio_fpm_w",
    "portfolio_fpm_m": "portfolio_fpm_m",
    "credit_notes": "credit_notes",
    "dam_area_results": "dam",
    "fpm_w_area_results": "fpm_w",
    "fpm_m_area_results": "fpm_m",
    "bm_atc": "bm_atc",
}


def _local_now() -> datetime:
    timezone_name = os.getenv("SAPP_SCHEDULE_TIMEZONE", "Africa/Johannesburg")
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except Exception:
        print(f"[scheduler] Invalid timezone {timezone_name!r}; using UTC")
        return datetime.now(ZoneInfo("UTC"))


def _is_last_weekday(current_date: date, weekday: int) -> bool:
    return current_date.weekday() == weekday and (current_date + timedelta(days=7)).month != current_date.month


def _scheduled_key(job: ScheduledJob, current: datetime) -> str:
    if job.frequency == "weekly":
        return f"{job.name}:{current.date() - timedelta(days=current.weekday())}"
    if job.frequency == "monthly":
        return f"{job.name}:{current.strftime('%Y-%m')}"
    return f"{job.name}:{current.date().isoformat()}"


def _is_due(job: ScheduledJob, current: datetime) -> bool:
    hour, minute = _parse_time(job.schedule_env, job.default_time)
    if (current.hour, current.minute) < (hour, minute):
        return False
    if job.frequency == "weekly":
        return current.weekday() == job.weekday
    if job.frequency == "monthly":
        return _is_last_weekday(current.date(), job.last_weekday or 0)
    return True


def _next_due(job: ScheduledJob, current: datetime) -> datetime:
    hour, minute = _parse_time(job.schedule_env, job.default_time)
    candidate_date = current.date()
    for _ in range(370):
        candidate = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            hour,
            minute,
            tzinfo=current.tzinfo,
        )
        if candidate >= current:
            if job.frequency == "daily":
                return candidate
            if job.frequency == "weekly" and candidate_date.weekday() == job.weekday:
                return candidate
            if job.frequency == "monthly" and _is_last_weekday(candidate_date, job.last_weekday or 0):
                return candidate
        candidate_date += timedelta(days=1)
    return current


def _date_range_for_job(job_name: str, current_date: date) -> tuple[date, date]:
    if job_name == "fpm_w_area_results" or job_name == "portfolio_fpm_w":
        monday = current_date - timedelta(days=current_date.weekday())
        return monday, monday
    if job_name == "fpm_m_area_results" or job_name == "portfolio_fpm_m":
        month_start = current_date.replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return month_start, next_month - timedelta(days=1)
    return current_date, current_date


def _latest_portfolio_date(market: str, fallback: date) -> date:
    record = get_db()["sapp_participant_portfolio_results"].find_one(
        {"market": market, "delivery_date": {"$exists": True}},
        sort=[("delivery_date", -1)],
        projection={"delivery_date": 1},
    )
    if record:
        try:
            return date.fromisoformat(str(record["delivery_date"])[:10])
        except (KeyError, TypeError, ValueError):
            pass
    return fallback


def _portfolio_lookahead_range(job_name: str, current_date: date) -> tuple[date, date]:
    if job_name == "portfolio_dam":
        start = _latest_portfolio_date("dam", current_date)
        return start, start + timedelta(days=7)
    if job_name == "portfolio_fpm_w":
        fallback = current_date - timedelta(days=current_date.weekday())
        start = _latest_portfolio_date("fpm_w", fallback)
        start -= timedelta(days=start.weekday())
        return start, start + timedelta(days=7)
    if job_name == "portfolio_fpm_m":
        fallback = current_date.replace(day=1)
        start = _latest_portfolio_date("fpm_m", fallback).replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_after_next = (next_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, month_after_next - timedelta(days=1)
    return _date_range_for_job(job_name, current_date)


def _run_job(job_name: str, current_date: date) -> dict[str, Any]:
    timeout = _env_int("SAPP_SCRAPER_TIMEOUT", 20)
    observe_seconds = _env_int("SAPP_SCRAPER_OBSERVE_SECONDS", 1) - 1
    headless = None
    start_date, end_date = _portfolio_lookahead_range(job_name, current_date)

    if job_name == "dam_area_results":
        return run_dam_standalone(start_date, end_date, timeout, headless, observe_seconds, "prices")
    if job_name == "fpm_w_area_results":
        return run_fpm_w_standalone(start_date, end_date, timeout, headless, observe_seconds)
    if job_name == "fpm_m_area_results":
        return run_fpm_m_standalone(start_date, end_date, timeout, headless, observe_seconds)
    if job_name == "bm_atc":
        return run_bm_atc_standalone(None, start_date, end_date, "All Areas", timeout, headless, observe_seconds)
    if job_name == "credit_notes":
        job = get_extraction_job("trading_invoice_credit_note")
        return run_extraction_job_for_date_range(job, start_date, end_date, continue_on_error=True, headless=headless)
    if job_name == "portfolio_dam":
        return run_portfolio_extraction_bundle_for_date_range(
            start_date, end_date, market="dam", headless=headless
        )
    if job_name == "portfolio_fpm_w":
        return run_portfolio_extraction_bundle_for_date_range(
            start_date, end_date, market="fpm_w", headless=headless
        )
    if job_name == "portfolio_fpm_m":
        return run_portfolio_extraction_bundle_for_date_range(
            start_date, end_date, market="fpm_m", headless=headless
        )
    raise ValueError(f"Unknown scheduled job: {job_name}")


def _summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _summary(item) for key, item in value.items() if key not in {"results", "related_results"}}
    if isinstance(value, list):
        return {"count": len(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


class SappAutoScraper:
    def __init__(self) -> None:
        self.enabled = _env_bool("SAPP_AUTO_SCRAPE_ENABLED", False)
        self.poll_seconds = _env_int("SAPP_SCHEDULER_POLL_SECONDS", 30)
        self.task: Optional[asyncio.Task] = None
        self.job_lock = asyncio.Lock()
        self.busy = False

    def configure_database(self) -> None:
        collection = get_db()[RUNS_COLLECTION]
        collection.create_index("scheduler_key", unique=True)
        collection.create_index([("started_at", -1)])

    async def start(self) -> None:
        self.configure_database()
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._loop(), name="sapp-auto-scraper")
            print("[scheduler] Automatic SAPP scraping enabled")
        else:
            print("[scheduler] Automatic SAPP scraping disabled")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def _claim(self, job: ScheduledJob, scheduler_key: str, scheduled_for: datetime) -> Optional[str]:
        run_id = str(uuid.uuid4())
        try:
            get_db()[RUNS_COLLECTION].insert_one(
                {
                    "run_id": run_id,
                    "scheduler_key": scheduler_key,
                    "job": job.name,
                    "dataset_id": DATASET_IDS.get(job.name, job.name),
                    "label": job.label,
                    "status": "running",
                    "scheduled_for": scheduled_for,
                    "started_at": datetime.utcnow(),
                }
            )
            return run_id
        except DuplicateKeyError:
            return None

    def _finish(self, run_id: str, status: str, result: Any = None, error: Optional[str] = None) -> None:
        update = {"status": status, "finished_at": datetime.utcnow()}
        if result is not None:
            update["result"] = _summary(result)
        if error:
            update["error"] = error
        get_db()[RUNS_COLLECTION].update_one({"run_id": run_id}, {"$set": update})

    async def _execute(self, job: ScheduledJob, scheduler_key: str, scheduled_for: datetime) -> dict[str, Any]:
        async with self.job_lock:
            self.busy = True
            run_id = self._claim(job, scheduler_key, scheduled_for)
            if run_id is None:
                self.busy = False
                return {"status": "already_claimed", "job": job.name}
            try:
                result = await asyncio.to_thread(_run_job, job.name, scheduled_for.date())
                self._finish(run_id, "success", result=result)
                return {"status": "success", "job": job.name, "run_id": run_id}
            except Exception as exc:
                self._finish(run_id, "failed", error=str(exc))
                print(f"[scheduler] {job.name} failed: {exc}")
                return {"status": "failed", "job": job.name, "run_id": run_id, "error": str(exc)}
            finally:
                self.busy = False

    async def _loop(self) -> None:
        while True:
            current = _local_now()
            for job in JOB_DEFINITIONS:
                if _is_due(job, current):
                    await self._execute(job, _scheduled_key(job, current), current)
            await asyncio.sleep(self.poll_seconds)

    async def trigger(self, job_name: str) -> dict[str, Any]:
        job = next((item for item in JOB_DEFINITIONS if item.name == job_name), None)
        if job is None:
            raise ValueError(f"Unknown scheduled job: {job_name}")
        if self.busy or self.job_lock.locked():
            raise RuntimeError("A scrape is already in progress")
        current = _local_now()
        self.busy = True
        asyncio.create_task(self._execute(job, f"manual:{uuid.uuid4()}", current))
        return {"status": "accepted", "job": job.name, "scheduled_for": current.isoformat()}

    def status(self) -> dict[str, Any]:
        current = _local_now()
        jobs = []
        collection = get_db()[RUNS_COLLECTION]
        for job in JOB_DEFINITIONS:
            latest = collection.find_one({"job": job.name}, sort=[("started_at", -1)])
            jobs.append(
                {
                    "job": job.name,
                    "dataset_id": DATASET_IDS.get(job.name, job.name),
                    "label": job.label,
                    "enabled": self.enabled,
                    "frequency": job.frequency,
                    "schedule_time": os.getenv(job.schedule_env, job.default_time),
                    "timezone": str(current.tzinfo),
                    "next_due": _next_due(job, current).isoformat(),
                    "last_run": _summary(latest) if latest else None,
                }
            )
        return {"enabled": self.enabled, "busy": self.busy, "timezone": str(current.tzinfo), "jobs": jobs}

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [_summary(item) for item in get_db()[RUNS_COLLECTION].find().sort("started_at", -1).limit(limit)]


sapp_auto_scraper = SappAutoScraper()
