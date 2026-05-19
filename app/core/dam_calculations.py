"""
Dam volume and generation projection calculations.

The lookup tables and formulas are based on the DAM VOLUMES COMPUTATIONS
workbook. Keep this module independent from FastAPI so the same calculations can
be reused by future jobs, imports, or forecasting workflows.
"""

from dataclasses import dataclass
from typing import Literal


DamCalculationCode = Literal["mita_hills", "mulungushi"]


@dataclass(frozen=True)
class DamLookupRange:
    """Level/volume interpolation range."""

    lower_level_ft: float
    upper_level_ft: float
    lower_volume_m3: float
    upper_volume_m3: float


@dataclass(frozen=True)
class DamCalculationConfig:
    """Static configuration for a dam calculation."""

    code: DamCalculationCode
    name: str
    lookup_table: tuple[DamLookupRange, ...]
    dead_storage_volume_m3: float
    fill_reference_volume_m3: float
    energy_m3_per_kwh: float
    generation_factor: float
    default_current_level_ft: float
    default_evaporation_rate: float
    default_production_rate_mw: float

    @property
    def min_level_ft(self) -> float:
        return self.lookup_table[0].lower_level_ft

    @property
    def max_level_ft(self) -> float:
        return self.lookup_table[-1].upper_level_ft


DAM_CALCULATION_CONFIGS: dict[DamCalculationCode, DamCalculationConfig] = {
    "mita_hills": DamCalculationConfig(
        code="mita_hills",
        name="Mita Hills Dam",
        dead_storage_volume_m3=23872960.0,
        fill_reference_volume_m3=680082550.0,
        energy_m3_per_kwh=4.71,
        generation_factor=18 / 2.15,
        default_current_level_ft=216.95,
        default_evaporation_rate=0.042,
        default_production_rate_mw=23.24,
        lookup_table=(
            DamLookupRange(140.0, 150.0, 23872960.0, 44467010.0),
            DamLookupRange(150.0, 160.0, 44467010.0, 75824690.0),
            DamLookupRange(160.0, 170.0, 75824690.0, 121340200.0),
            DamLookupRange(170.0, 180.0, 121340200.0, 184407750.0),
            DamLookupRange(180.0, 190.0, 184407750.0, 268421550.0),
            DamLookupRange(190.0, 200.0, 268421550.0, 376775810.0),
            DamLookupRange(200.0, 210.0, 376775810.0, 512864740.0),
            DamLookupRange(210.0, 220.0, 512864740.0, 680082550.0),
            DamLookupRange(220.0, 221.0, 680082550.0, 698647090.0),
            DamLookupRange(221.0, 222.0, 698647090.0, 717560270.0),
            DamLookupRange(222.0, 223.0, 717560270.0, 736825460.0),
            DamLookupRange(223.0, 224.0, 736825460.0, 756446060.0),
            DamLookupRange(224.0, 225.0, 756446060.0, 776425470.0),
            DamLookupRange(225.0, 226.0, 776425470.0, 796767090.0),
            DamLookupRange(226.0, 227.0, 796767090.0, 817474300.0),
            DamLookupRange(227.0, 228.0, 817474300.0, 838550500.0),
            DamLookupRange(228.0, 229.0, 838550500.0, 859999080.0),
            DamLookupRange(229.0, 230.0, 859999080.0, 881823440.0),
            DamLookupRange(230.0, 231.0, 881823440.0, 904026980.0),
            DamLookupRange(231.0, 232.0, 904026980.0, 926613090.0),
            DamLookupRange(232.0, 233.0, 926613090.0, 949585150.0),
        ),
    ),
    "mulungushi": DamCalculationConfig(
        code="mulungushi",
        name="Mulungushi Dam",
        dead_storage_volume_m3=21360000.0,
        fill_reference_volume_m3=270784100.0,
        energy_m3_per_kwh=1.35,
        generation_factor=20 / 0.52,
        default_current_level_ft=630.29,
        default_evaporation_rate=0.0382,
        default_production_rate_mw=21.5,
        lookup_table=(
            DamLookupRange(570.0, 580.0, 21360000.0, 34361000.0),
            DamLookupRange(580.0, 590.0, 34361000.0, 53798000.0),
            DamLookupRange(590.0, 600.0, 53798000.0, 83280000.0),
            DamLookupRange(600.0, 610.0, 83280000.0, 126412000.0),
            DamLookupRange(610.0, 620.0, 126412000.0, 186803000.0),
            DamLookupRange(620.0, 625.0, 186803000.0, 224597000.0),
            DamLookupRange(625.0, 628.5, 224597000.0, 254399000.0),
            DamLookupRange(628.5, 629.5, 254399000.0, 263445000.0),
            DamLookupRange(629.5, 630.5, 263445000.0, 272735000.0),
            DamLookupRange(630.5, 631.5, 272735000.0, 282271000.0),
            DamLookupRange(631.5, 632.5, 282271000.0, 292057000.0),
            DamLookupRange(632.5, 633.5, 292057000.0, 302096000.0),
            DamLookupRange(633.5, 634.5, 302096000.0, 312393000.0),
            DamLookupRange(634.5, 635.5, 312393000.0, 322952000.0),
            DamLookupRange(635.5, 636.5, 322952000.0, 333774000.0),
            DamLookupRange(636.5, 637.5, 333774000.0, 344865000.0),
            DamLookupRange(637.5, 638.5, 344865000.0, 356228000.0),
            DamLookupRange(638.5, 639.5, 356228000.0, 367866000.0),
            DamLookupRange(639.5, 640.5, 367866000.0, 379783000.0),
            DamLookupRange(640.5, 641.5, 379783000.0, 391982000.0),
        ),
    ),
}


def list_dam_calculation_configs() -> list[dict]:
    """Return lightweight dam calculation configuration for frontend setup."""
    return [
        {
            "code": config.code,
            "name": config.name,
            "min_level_ft": config.min_level_ft,
            "max_level_ft": config.max_level_ft,
            "default_current_level_ft": config.default_current_level_ft,
            "default_evaporation_rate": config.default_evaporation_rate,
            "default_production_rate_mw": config.default_production_rate_mw,
        }
        for config in DAM_CALCULATION_CONFIGS.values()
    ]


def get_dam_calculation_config(code: DamCalculationCode) -> DamCalculationConfig:
    """Return one dam calculation configuration."""
    return DAM_CALCULATION_CONFIGS[code]


def find_lookup_range(
    config: DamCalculationConfig,
    current_level_ft: float,
) -> DamLookupRange | None:
    """Find the interpolation range matching the spreadsheet's lookup logic."""
    for lookup_range in config.lookup_table:
        if (
            lookup_range.lower_level_ft <= current_level_ft < lookup_range.upper_level_ft
            or current_level_ft == lookup_range.upper_level_ft == config.max_level_ft
        ):
            return lookup_range
    return None


def interpolate_volume_m3(lookup_range: DamLookupRange, current_level_ft: float) -> float:
    """Linearly interpolate dam volume for the current level."""
    slope = (
        (lookup_range.upper_volume_m3 - lookup_range.lower_volume_m3)
        / (lookup_range.upper_level_ft - lookup_range.lower_level_ft)
    )
    return slope * current_level_ft + (
        lookup_range.upper_volume_m3 - slope * lookup_range.upper_level_ft
    )


def calculate_dam_projection(
    dam: DamCalculationCode,
    current_level_ft: float,
    evaporation_rate: float,
    production_rate_mw: float,
) -> dict:
    """Calculate dam volume and projected generation duration."""
    config = get_dam_calculation_config(dam)
    lookup_range = find_lookup_range(config, current_level_ft)
    if lookup_range is None:
        return {
            "dam": config.code,
            "dam_name": config.name,
            "is_off_range": True,
            "message": (
                f"Current dam level must be between "
                f"{config.min_level_ft:g} ft and {config.max_level_ft:g} ft"
            ),
            "input": {
                "current_level_ft": current_level_ft,
                "evaporation_rate": evaporation_rate,
                "production_rate_mw": production_rate_mw,
            },
            "lookup_range": None,
            "calculated_dam_volume_m3": None,
            "useful_dam_volume_m3": None,
            "percentage_fill": None,
            "equivalent_energy_kwh": None,
            "equivalent_energy_gwh": None,
            "projected_generation_days": None,
            "projected_generation_months": None,
        }

    calculated_volume_m3 = interpolate_volume_m3(lookup_range, current_level_ft)
    useful_volume_m3 = calculated_volume_m3 - config.dead_storage_volume_m3
    equivalent_energy_kwh = useful_volume_m3 / config.energy_m3_per_kwh
    projected_days = (
        config.generation_factor
        / production_rate_mw
        * useful_volume_m3
        / 1_000_000
        * (1 - evaporation_rate)
    )

    return {
        "dam": config.code,
        "dam_name": config.name,
        "is_off_range": False,
        "message": None,
        "input": {
            "current_level_ft": current_level_ft,
            "evaporation_rate": evaporation_rate,
            "production_rate_mw": production_rate_mw,
        },
        "lookup_range": {
            "lower_level_ft": lookup_range.lower_level_ft,
            "upper_level_ft": lookup_range.upper_level_ft,
            "lower_volume_m3": lookup_range.lower_volume_m3,
            "upper_volume_m3": lookup_range.upper_volume_m3,
        },
        "calculated_dam_volume_m3": calculated_volume_m3,
        "useful_dam_volume_m3": useful_volume_m3,
        "percentage_fill": calculated_volume_m3 / config.fill_reference_volume_m3 * 100,
        "equivalent_energy_kwh": equivalent_energy_kwh,
        "equivalent_energy_gwh": equivalent_energy_kwh / 1_000_000,
        "projected_generation_days": projected_days,
        "projected_generation_months": projected_days / 30,
    }
