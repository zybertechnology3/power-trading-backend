"""
Energy scheduling calculations.

The yearly budget schedule mirrors the Power Sources table from the revenue
projection workbook. The calculations stay in memory so the UI can calculate or
save a full yearly schedule with one request.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.core.dam_calculations import calculate_equivalent_water_volume_from_energy_gwh

MONTH_KEYS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)

HOURS_PER_MONTH = 720


@dataclass(frozen=True)
class BudgetRowDefinition:
    """Power sources table row metadata."""

    code: str
    label: str
    unit: str
    category: str
    summary_type: str
    source: str


POWER_SOURCE_ROW_DEFINITIONS = (
    BudgetRowDefinition("mps_mw", "MPS - MW", "MW", "capacity", "average", "input"),
    BudgetRowDefinition("lps_mw", "LPS - MW", "MW", "capacity", "average", "input"),
    BudgetRowDefinition("solar_mw", "Solar", "MW", "capacity", "average", "fixed"),
    BudgetRowDefinition("total_hydro_mw", "Total Hydro", "MW", "capacity", "average", "computed"),
    BudgetRowDefinition("lps_solar_mw", "LPS -Sol - MW", "MW", "capacity", "average", "input"),
    BudgetRowDefinition("sapp_purchase_mw", "SAPP Purch. MW", "MW", "capacity", "average", "input"),
    BudgetRowDefinition("total_mw", "Total, MW", "MW", "capacity", "average", "computed"),
    BudgetRowDefinition("mps_gwh", "MPS - MWh", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("lps_gwh", "LPS - MWh", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("total_hydro_gwh", "Total Hydro", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("lps_solar_gwh", "LPS - Sol -MWh", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("sapp_purchase_gwh", "SAPP Purch. MWh", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("total_generation_gwh", "Total Gen.MWh", "GWh", "energy", "sum", "computed"),
    BudgetRowDefinition("lps_equivalent_water_volume_mm3", "Equ. Volume of Water LPS", "Mm3", "water_volume", "sum", "computed"),
    BudgetRowDefinition("mps_equivalent_water_volume_mm3", "Equ. Volume of Water MPS", "Mm3", "water_volume", "sum", "computed"),
)

DEFAULT_YEARLY_BUDGET_INPUTS = {
    "name": "Revenue Projection Power Sources Budget",
    "year": 2026,
    "target_year": 2026,
    "comparison_year": 2025,
    "mps_mw": [24.074074074074073] * 12,
    "lps_mw": [18.0, 20.0, 20.0, 18.0, 17.0, 13.0, 13.0, 13.0, 13.0, 13.0, 12.0, 0.0],
    "solar_mw": [4.5] * 12,
    "lps_solar_mw": [None, None, None, None, None, 5.4, 5.4, 5.4, 5.4, 5.4, 5.4, 5.4],
    "sapp_purchase_mw": [
        6.944444444,
        6.944444444,
        6.944444444,
        6.944444444,
        6.944444444,
        6.944444444,
        6.944444444,
        6.944444444,
        None,
        None,
        None,
        6.944444444,
    ],
    "sapp_purchase_gwh": [
        5.208333333,
        5.208333333,
        5.208333333,
        1.388888889,
        6.597222222,
        6.597222222,
        6.597222222,
        6.597222222,
        None,
        None,
        None,
        1.388888889,
    ],
    "january_total_hydro_mw_override": 56.0,
    "prior_year": {
        "mps_mw": 17.4641717520496,
        "lps_mw": 9.0,
        "total_hydro_mw": 26.4641717520496,
        "lps_solar_mw": 0.0,
        "sapp_purchase_mw": 7.650219907407407,
        "total_mw": 34.11439165945701,
        "mps_gwh": 154.66180000000003,
        "lps_gwh": 78.149907,
        "total_hydro_gwh": 232.811707,
        "lps_solar_gwh": 0.0,
        "sapp_purchase_gwh": 66.0979,
        "total_generation_gwh": 298.909607,
    },
}

SAPP_PURCHASE_GWH_FACTORS = [
    0.75,
    0.75,
    0.75,
    0.2,
    0.95,
    0.95,
    0.95,
    0.95,
    None,
    None,
    None,
    0.2,
]


def default_yearly_budget_payload(year: int = 2026) -> dict:
    """Return workbook-derived default inputs for a yearly power source budget."""
    payload = deepcopy(DEFAULT_YEARLY_BUDGET_INPUTS)
    payload["year"] = year
    payload["target_year"] = year
    payload["comparison_year"] = year - 1
    return payload


def month_start_dates(year: int) -> list[date]:
    """Return first day of each budget month."""
    return [date(year, month_number, 1) for month_number in range(1, 13)]


def _number_or_none(value) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _zero(value) -> float:
    return 0.0 if value is None else float(value)


def _sum(values: list[Optional[float]]) -> float:
    return sum(_zero(value) for value in values)


def _average_excel(values: list[Optional[float]]) -> Optional[float]:
    numeric_values = [float(value) for value in values if value is not None]
    if not numeric_values:
        return None
    return sum(numeric_values) / len(numeric_values)


def _variance(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    if current is None or prior in (None, 0):
        return None
    return (current - prior) / prior


def _annualized_gwh_from_average_mw(average_mw: Optional[float]) -> Optional[float]:
    if average_mw is None:
        return None
    return average_mw * 12 * HOURS_PER_MONTH / 1000


def _monthly_energy_gwh_from_mw(value: Optional[float]) -> float:
    return _zero(value) * HOURS_PER_MONTH / 1000


def _row(
    definition: BudgetRowDefinition,
    months: list[Optional[float]],
    prior_year: dict,
    summary_value: Optional[float] = None,
    annualized_gwh: Optional[float] = None,
    formula: Optional[str] = None,
) -> dict:
    if summary_value is None:
        summary_value = (
            _average_excel(months)
            if definition.summary_type == "average"
            else _sum(months)
        )
    prior_year_value = prior_year.get(definition.code)
    return {
        "code": definition.code,
        "label": definition.label,
        "unit": definition.unit,
        "category": definition.category,
        "source": definition.source,
        "summary_type": definition.summary_type,
        "months": {
            month_key: _number_or_none(months[index])
            for index, month_key in enumerate(MONTH_KEYS)
        },
        "summary_value": summary_value,
        "annualized_gwh": annualized_gwh,
        "prior_year_value": prior_year_value,
        "variance": _variance(summary_value, prior_year_value),
        "formula": formula,
    }


def calculate_yearly_power_sources_budget(payload: dict) -> dict:
    """Calculate the yearly Power Sources budget table."""
    has_explicit_sapp_purchase_gwh = "sapp_purchase_gwh" in payload
    source = {**default_yearly_budget_payload(payload.get("year", 2026)), **payload}
    year = int(source["year"])
    prior_year = source.get("prior_year") or {}

    mps_mw = source["mps_mw"]
    lps_mw = source["lps_mw"]
    solar_mw = source.get("solar_mw") or DEFAULT_YEARLY_BUDGET_INPUTS["solar_mw"]
    lps_solar_mw = source["lps_solar_mw"]
    sapp_purchase_mw = source["sapp_purchase_mw"]
    sapp_purchase_gwh = source.get("sapp_purchase_gwh") if has_explicit_sapp_purchase_gwh else None
    if sapp_purchase_gwh is None:
        sapp_purchase_gwh = [
            (
                None
                if sapp_purchase_mw[index] is None or SAPP_PURCHASE_GWH_FACTORS[index] is None
                else sapp_purchase_mw[index] * SAPP_PURCHASE_GWH_FACTORS[index]
            )
            for index in range(12)
        ]

    total_hydro_mw = []
    for index in range(12):
        if index == 0 and source.get("january_total_hydro_mw_override") is not None:
            total_hydro_mw.append(float(source["january_total_hydro_mw_override"]))
        else:
            total_hydro_mw.append(_zero(mps_mw[index]) + _zero(lps_mw[index]))

    mps_mw_average = _average_excel(mps_mw)
    lps_mw_average = _average_excel(lps_mw)
    total_hydro_mw_summary = _zero(mps_mw_average) + _zero(lps_mw_average)
    total_hydro_mw_annualized_gwh = _annualized_gwh_from_average_mw(total_hydro_mw_summary)
    lps_solar_mw_average = _average_excel(lps_solar_mw)
    lps_solar_mw_annualized_gwh = _annualized_gwh_from_average_mw(lps_solar_mw_average)
    sapp_purchase_mw_average = _average_excel(sapp_purchase_mw)
    sapp_purchase_mw_annualized_gwh = _annualized_gwh_from_average_mw(sapp_purchase_mw_average)

    total_mw = [
        total_hydro_mw[index] + _zero(lps_solar_mw[index]) + _zero(sapp_purchase_mw[index])
        for index in range(12)
    ]

    mps_gwh = [_monthly_energy_gwh_from_mw(value) for value in mps_mw]
    lps_solar_gwh = [_monthly_energy_gwh_from_mw(value) for value in lps_solar_mw]
    lps_gwh = [
        (_zero(lps_mw[index]) * HOURS_PER_MONTH - _zero(lps_solar_gwh[index])) / 1000
        for index in range(12)
    ]
    total_hydro_gwh = [
        mps_gwh[index] + lps_gwh[index]
        for index in range(12)
    ]

    total_generation_gwh = [
        lps_gwh[index] + mps_gwh[index] + lps_solar_gwh[index]
        + (_zero(sapp_purchase_gwh[index]) if index == 0 else 0)
        for index in range(12)
    ]

    definitions = {definition.code: definition for definition in POWER_SOURCE_ROW_DEFINITIONS}
    rows = [
        _row(
            definitions["mps_mw"],
            mps_mw,
            prior_year,
            annualized_gwh=_annualized_gwh_from_average_mw(mps_mw_average),
            formula="AVERAGE(monthly MPS MW); annualized = average * 12 * 720 / 1000",
        ),
        _row(
            definitions["lps_mw"],
            lps_mw,
            prior_year,
            annualized_gwh=_annualized_gwh_from_average_mw(lps_mw_average),
            formula="AVERAGE(monthly LPS MW); annualized = average * 12 * 720 / 1000",
        ),
        _row(definitions["solar_mw"], solar_mw, prior_year, formula="Workbook input row"),
        _row(
            definitions["total_hydro_mw"],
            total_hydro_mw,
            prior_year,
            summary_value=total_hydro_mw_summary,
            annualized_gwh=total_hydro_mw_annualized_gwh,
            formula="Monthly Jan may use override; other months MPS MW + LPS MW. Summary = MPS avg + LPS avg.",
        ),
        _row(
            definitions["lps_solar_mw"],
            lps_solar_mw,
            prior_year,
            annualized_gwh=lps_solar_mw_annualized_gwh,
            formula="AVERAGE(non-blank monthly LPS solar MW); annualized = average * 12 * 720 / 1000",
        ),
        _row(
            definitions["sapp_purchase_mw"],
            sapp_purchase_mw,
            prior_year,
            annualized_gwh=sapp_purchase_mw_annualized_gwh,
            formula="AVERAGE(non-blank monthly SAPP purchase MW); annualized = average * 12 * 720 / 1000",
        ),
        _row(
            definitions["total_mw"],
            total_mw,
            prior_year,
            summary_value=(
                total_hydro_mw_summary
                + _zero(lps_solar_mw_average)
                + _zero(sapp_purchase_mw_average)
            ),
            annualized_gwh=(
                (total_hydro_mw_annualized_gwh or 0)
                + (lps_solar_mw_annualized_gwh or 0)
                + (sapp_purchase_mw_annualized_gwh or 0)
            ),
            formula="Monthly Total Hydro MW + LPS Solar MW + SAPP Purchase MW.",
        ),
        _row(definitions["mps_gwh"], mps_gwh, prior_year, annualized_gwh=_sum(mps_gwh), formula="MPS MW * 720 / 1000"),
        _row(
            definitions["lps_gwh"],
            lps_gwh,
            prior_year,
            formula="(LPS MW * 720 - LPS Solar GWh) / 1000, matching workbook units",
        ),
        _row(definitions["total_hydro_gwh"], total_hydro_gwh, prior_year, formula="MPS GWh + LPS GWh"),
        _row(definitions["lps_solar_gwh"], lps_solar_gwh, prior_year, formula="LPS Solar MW * 720 / 1000"),
        _row(definitions["sapp_purchase_gwh"], sapp_purchase_gwh, prior_year, formula="Workbook SAPP purchase energy row"),
        _row(
            definitions["total_generation_gwh"],
            total_generation_gwh,
            prior_year,
            summary_value=_sum(lps_gwh) + _sum(mps_gwh) + _sum(lps_solar_gwh) + _sum(sapp_purchase_gwh),
            formula="Monthly rows match workbook; yearly summary includes all SAPP purchase energy.",
        ),
    ]

    equivalent_water_volume = {
        "mps": {
            month_key: calculate_equivalent_water_volume_from_energy_gwh(
                "mulungushi",
                mps_gwh[index],
            )
            for index, month_key in enumerate(MONTH_KEYS)
        },
        "lps": {
            month_key: calculate_equivalent_water_volume_from_energy_gwh(
                "mita_hills",
                lps_gwh[index],
            )
            for index, month_key in enumerate(MONTH_KEYS)
        },
    }
    mps_equivalent_water_volume_mm3 = [
        equivalent_water_volume["mps"][month_key]["water_volume_mm3"]
        for month_key in MONTH_KEYS
    ]
    lps_equivalent_water_volume_mm3 = [
        equivalent_water_volume["lps"][month_key]["water_volume_mm3"]
        for month_key in MONTH_KEYS
    ]
    rows.extend(
        [
            _row(
                definitions["lps_equivalent_water_volume_mm3"],
                lps_equivalent_water_volume_mm3,
                prior_year,
                formula="LPS GWh * 1,000,000 kWh/GWh * 4.71 m3/kWh / 1,000,000",
            ),
            _row(
                definitions["mps_equivalent_water_volume_mm3"],
                mps_equivalent_water_volume_mm3,
                prior_year,
                formula="MPS GWh * 1,000,000 kWh/GWh * 1.35 m3/kWh / 1,000,000",
            ),
        ]
    )

    return {
        "year": year,
        "target_year": int(source.get("target_year") or year),
        "comparison_year": int(source.get("comparison_year") or year - 1),
        "name": source.get("name"),
        "months": [
            {
                "key": month_key,
                "month": index + 1,
                "date": month_start_dates(year)[index],
            }
            for index, month_key in enumerate(MONTH_KEYS)
        ],
        "inputs": {
            "mps_mw": mps_mw,
            "lps_mw": lps_mw,
            "lps_solar_mw": lps_solar_mw,
            "sapp_purchase_mw": sapp_purchase_mw,
        },
        "rows": rows,
        "equivalent_water_volume": equivalent_water_volume,
    }
