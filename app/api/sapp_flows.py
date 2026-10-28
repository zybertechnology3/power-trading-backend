"""Read APIs consumed by the frontend after an mtp_flows ready notification."""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.database import get_db

router = APIRouter(prefix="/sapp", tags=["sapp"])
FlowProduct = Literal["DAM", "FPM_W", "FPM_M", "BILATERAL", "NET_TOTAL"]


class FlowRecord(BaseModel):
    delivery_date: date
    hour: int = Field(ge=1, le=24)
    hour_label: str
    product: FlowProduct
    interface_id: str
    from_area: str
    to_area: str
    flow_mw: float
    batch_id: str
    source_file: str


class FlowsResponse(BaseModel):
    dataset_id: str = "mtp_flows"
    start_date: date
    end_date: date
    total: int
    records: list[FlowRecord]


def _read_flows(start: date, end: date, product, interface_id, from_area, to_area):
    if end < start:
        raise HTTPException(400, "end_date must be greater than or equal to start_date")
    filters = {"delivery_date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    for key, value in (("product", product), ("interface_id", interface_id),
                       ("from_area", from_area), ("to_area", to_area)):
        if value is not None:
            filters[key] = value
    records = list(get_db()["sapp_flows"].find(filters, {"_id": 0}).sort(
        [("delivery_date", 1), ("hour", 1), ("product", 1), ("interface_id", 1)]))
    return FlowsResponse(start_date=start, end_date=end, total=len(records), records=records)


@router.get("/flows", response_model=FlowsResponse)
def get_flows(
    delivery_date: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    product: Optional[FlowProduct] = None,
    interface_id: Optional[str] = Query(None, description="Source corridor code, e.g. BOT_TO_RSAN"),
    from_area: Optional[str] = None,
    to_area: Optional[str] = None,
):
    """Return signed scheduled flows; net totals and product sheets are separate series."""
    if delivery_date is not None:
        if start_date is not None or end_date is not None:
            raise HTTPException(400, "Use delivery_date or start_date/end_date, not both")
        start = end = delivery_date
    else:
        start, end = start_date or end_date, end_date or start_date
        if start is None:
            raise HTTPException(400, "Provide delivery_date or start_date/end_date")
    return _read_flows(start, end, product, interface_id, from_area, to_area)


@router.get("/flows/{delivery_date}", response_model=FlowsResponse)
def get_flows_for_day(
    delivery_date: date,
    product: Optional[FlowProduct] = None,
    interface_id: Optional[str] = None,
    from_area: Optional[str] = None,
    to_area: Optional[str] = None,
):
    return _read_flows(delivery_date, delivery_date, product, interface_id, from_area, to_area)
