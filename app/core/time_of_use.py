"""
Time-of-use period rules for energy trading.
"""

from datetime import date, timedelta
from typing import Iterable, Literal


TimeOfUsePeriod = Literal["off_peak", "standard", "peak"]

WEEKDAY_PERIODS: tuple[TimeOfUsePeriod, ...] = (
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "standard",
    "peak",
    "peak",
    "peak",
    "standard",
    "standard",
    "standard",
    "standard",
    "standard",
    "standard",
    "standard",
    "standard",
    "peak",
    "peak",
    "standard",
    "standard",
    "off_peak",
    "off_peak",
)

SATURDAY_PERIODS: tuple[TimeOfUsePeriod, ...] = (
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "standard",
    "standard",
    "standard",
    "standard",
    "standard",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
    "standard",
    "standard",
    "off_peak",
    "off_peak",
    "off_peak",
    "off_peak",
)

SUNDAY_PERIODS: tuple[TimeOfUsePeriod, ...] = ("off_peak",) * 24


def _normalize_holiday_dates(
    holiday_dates: Iterable[date] | None,
) -> set[date]:
    normalized_dates: set[date] = set()
    for value in holiday_dates or []:
        if isinstance(value, date):
            normalized_dates.add(value)
        else:
            normalized_dates.add(date.fromisoformat(str(value)))
    return normalized_dates


def get_time_of_use_period(
    delivery_date: date,
    hour: int,
    holiday_dates: Iterable[date] | None = None,
) -> TimeOfUsePeriod:
    """Return the time-of-use period for an hour where 1 means 00:00-01:00."""
    if hour < 1 or hour > 24:
        raise ValueError("hour must be between 1 and 24")

    if delivery_date in _normalize_holiday_dates(holiday_dates):
        return "off_peak"

    weekday = delivery_date.weekday()
    if weekday <= 4:
        periods = WEEKDAY_PERIODS
    elif weekday == 5:
        periods = SATURDAY_PERIODS
    else:
        periods = SUNDAY_PERIODS

    return periods[hour - 1]


def build_time_of_use_schedule(
    delivery_date: date,
    holiday_dates: Iterable[date] | None = None,
) -> list[dict]:
    """Return all 24 hourly periods for one delivery date."""
    return [
        {
            "date": delivery_date.isoformat(),
            "hour": hour,
            "hour_label": f"{hour - 1:02d}-{hour:02d}" if hour < 24 else "23-24",
            "product": get_time_of_use_period(delivery_date, hour, holiday_dates),
        }
        for hour in range(1, 25)
    ]


def count_time_of_use_hours(
    start_date: date,
    end_date: date,
    holiday_dates: Iterable[date] | None = None,
) -> dict[str, int]:
    """Count off-peak, standard, and peak hours over an inclusive date range."""
    if end_date < start_date:
        raise ValueError("end_date must be greater than or equal to start_date")

    counts = {"off_peak": 0, "standard": 0, "peak": 0}
    holiday_date_set = _normalize_holiday_dates(holiday_dates)
    current_date = start_date
    while current_date <= end_date:
        for hour in range(1, 25):
            counts[get_time_of_use_period(current_date, hour, holiday_date_set)] += 1
        current_date += timedelta(days=1)

    return counts
