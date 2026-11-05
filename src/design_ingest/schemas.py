"""Ready-made extraction schemas for ``design_ingest.extract``.

Every numeric field states its unit in the name and description so the LLM
converts to it. Fields are optional: missing in the document -> None, never
guessed. Add your own schemas the same way (any pydantic BaseModel works).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class _Sourced(BaseModel):
    source_pages: list[int] = Field(default_factory=list, description="Pages where the values were found (1-based)")
    notes: str | None = Field(None, description="Ambiguities, conflicting values, assumptions")


class ModuleVariant(BaseModel):
    pmax_w: float = Field(description="Nominal power at STC, W")
    voc_v: float | None = Field(None, description="Open-circuit voltage at STC, V")
    isc_a: float | None = Field(None, description="Short-circuit current at STC, A")
    vmp_v: float | None = Field(None, description="Voltage at MPP at STC, V")
    imp_a: float | None = Field(None, description="Current at MPP at STC, A")
    efficiency_pct: float | None = Field(None, description="Module efficiency at STC, %")


class ModuleDatasheet(_Sourced):
    """PV module datasheet. One entry in `variants` per power class."""

    manufacturer: str | None = None
    model_series: str | None = None
    technology: str | None = Field(None, description="e.g. mono PERC, TOPCon, HJT, bifacial glass-glass")
    variants: list[ModuleVariant] = Field(default_factory=list)
    temp_coeff_pmax_pct_per_c: float | None = Field(None, description="Temperature coefficient of Pmax, %/°C (negative)")
    temp_coeff_voc_pct_per_c: float | None = Field(None, description="Temperature coefficient of Voc, %/°C (negative)")
    temp_coeff_isc_pct_per_c: float | None = Field(None, description="Temperature coefficient of Isc, %/°C")
    noct_c: float | None = Field(None, description="NOCT or NMOT, °C")
    bifaciality_pct: float | None = Field(None, description="Bifaciality factor, %")
    max_system_voltage_v: float | None = Field(None, description="Maximum system voltage, V DC")
    max_series_fuse_a: float | None = Field(None, description="Maximum series fuse rating, A")
    cells: int | None = Field(None, description="Number of cells (count half-cells as written, e.g. 144)")
    length_mm: float | None = None
    width_mm: float | None = None
    thickness_mm: float | None = None
    weight_kg: float | None = None
    product_warranty_years: float | None = None
    performance_warranty_first_year_pct: float | None = Field(None, description="Guaranteed power after year 1, %")
    performance_warranty_annual_degradation_pct: float | None = Field(None, description="Linear annual degradation, %/year")
    performance_warranty_end_pct: float | None = Field(None, description="Guaranteed power at end of warranty, %")
    certifications: list[str] = Field(default_factory=list)


class InverterDatasheet(_Sourced):
    manufacturer: str | None = None
    model: str | None = None
    inverter_type: Literal["string", "central", "hybrid", "microinverter", "other"] | None = None
    ac_nominal_power_kw: float | None = None
    ac_max_power_kva: float | None = None
    ac_nominal_voltage_v: float | None = None
    ac_max_current_a: float | None = None
    grid_frequency_hz: float | None = None
    power_factor_range: str | None = Field(None, description="e.g. 0.8 leading ... 0.8 lagging")
    dc_max_voltage_v: float | None = None
    dc_start_voltage_v: float | None = None
    mppt_voltage_min_v: float | None = None
    mppt_voltage_max_v: float | None = None
    mppt_full_power_voltage_min_v: float | None = None
    mppt_full_power_voltage_max_v: float | None = None
    n_mppt: int | None = None
    strings_per_mppt: int | None = None
    max_input_current_per_mppt_a: float | None = None
    max_short_circuit_current_per_mppt_a: float | None = None
    max_efficiency_pct: float | None = None
    euro_efficiency_pct: float | None = None
    cec_efficiency_pct: float | None = None
    operating_temp_min_c: float | None = None
    operating_temp_max_c: float | None = None
    derating_start_temp_c: float | None = Field(None, description="Ambient temperature where power derating starts, °C")
    ip_rating: str | None = None
    weight_kg: float | None = None
    certifications: list[str] = Field(default_factory=list)


class BatteryDatasheet(_Sourced):
    """BESS container / rack / cabinet datasheet."""

    manufacturer: str | None = None
    model: str | None = None
    product_level: Literal["cell", "module", "rack", "cabinet", "container", "system"] | None = None
    chemistry: str | None = Field(None, description="e.g. LFP, NMC")
    nominal_energy_kwh: float | None = None
    usable_energy_kwh: float | None = None
    nominal_power_kw: float | None = None
    max_c_rate: float | None = None
    nominal_voltage_v: float | None = None
    voltage_range_min_v: float | None = None
    voltage_range_max_v: float | None = None
    round_trip_efficiency_pct: float | None = None
    depth_of_discharge_pct: float | None = None
    cycle_life: int | None = None
    cycle_life_conditions: str | None = Field(None, description="DoD, C-rate, temperature, end-of-life SoH for the cycle number")
    calendar_life_years: float | None = None
    eol_soh_pct: float | None = Field(None, description="State of health at end of life, %")
    cooling: str | None = Field(None, description="liquid / air")
    fire_suppression: str | None = None
    operating_temp_min_c: float | None = None
    operating_temp_max_c: float | None = None
    dimensions_mm: str | None = None
    weight_kg: float | None = None
    ip_rating: str | None = None
    certifications: list[str] = Field(default_factory=list, description="e.g. UL 9540, UL 9540A, IEC 62619")
    warranty_years: float | None = None


class TenderRequirement(BaseModel):
    ref: str | None = Field(None, description="Clause or section number as written, e.g. 4.2.3")
    category: Literal[
        "technical", "commercial", "legal", "schedule", "qualification", "hse", "documentation", "warranty", "other"
    ]
    requirement: str = Field(description="The requirement in one or two sentences, close to the original wording")
    mandatory: bool | None = Field(None, description="True for shall/must, False for should/may/preferred")
    value: str | None = Field(None, description="Numeric limit or value with unit if any, e.g. '>= 90 % RTE'")
    deadline: str | None = Field(None, description="Date or duration if the requirement has one")
    page: int | None = None


class TenderRequirements(BaseModel):
    project_name: str | None = None
    buyer: str | None = None
    submission_deadline: str | None = None
    scope_summary: str | None = Field(None, description="3-5 sentences: what is being procured")
    capacity: str | None = Field(None, description="Power/energy as written, e.g. '50 MW / 100 MWh'")
    requirements: list[TenderRequirement] = Field(default_factory=list)
    evaluation_criteria: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, description="Unclear or contradictory points worth a clarification request")


SCHEMAS: dict[str, type[BaseModel]] = {
    "module": ModuleDatasheet,
    "inverter": InverterDatasheet,
    "battery": BatteryDatasheet,
    "tender": TenderRequirements,
}
