"""Read MTP email ATC data without changing the existing BM scraper API."""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.database import get_db

router = APIRouter(prefix="/sapp", tags=["sapp"])


class AtcRecord(BaseModel):
    delivery_date: date
    market: Literal["BM_ATC"]
    source_market: Literal["DAM"]
    hour: Optional[int] = Field(None, ge=1, le=24)
    hour_label: str
    product: str
    row_index: int = Field(ge=1, le=28)
    is_summary: bool
    columns: list[str]
    values: dict[str, Optional[float]]
    source_file: str


class AtcResponse(BaseModel):
    dataset_id: str = "mtp_atc"
    start_date: date
    end_date: date
    total: int
    records: list[AtcRecord]


def _read_atc(start: date, end: date, include_summaries: bool):
    if end < start:
        raise HTTPException(400, "end_date must be greater than or equal to start_date")
    filters = {"delivery_date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
               "market": "BM_ATC"}
    if not include_summaries:
        filters["is_summary"] = False
    records = list(get_db()["sapp_bm_atc_records"].find(filters, {"_id": 0}).sort(
        [("delivery_date", 1), ("row_index", 1)]))
    return AtcResponse(start_date=start, end_date=end, total=len(records), records=records)


@router.get("/atc", response_model=AtcResponse)
def get_atc(
    delivery_date: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    include_summaries: bool = True,
):
    """Return dynamic ATC rows in display order; blanks remain null, not zero."""
    if delivery_date is not None:
        if start_date is not None or end_date is not None:
            raise HTTPException(400, "Use delivery_date or start_date/end_date, not both")
        start = end = delivery_date
    else:
        start, end = start_date or end_date, end_date or start_date
        if start is None:
            raise HTTPException(400, "Provide delivery_date or start_date/end_date")
    return _read_atc(start, end, include_summaries)


@router.get("/atc/{delivery_date}", response_model=AtcResponse)
def get_atc_for_day(delivery_date: date, include_summaries: bool = True):
    return _read_atc(delivery_date, delivery_date, include_summaries)
