"""
municipal_energy_model.py — Mathematical Model of St. Thomas Energy Infrastructure

Represents the City's 51 properties, solar potential, battery storage options,
and grid connections. All baseline consumption data sourced from the official
2025-2029 CDM Plan.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from config import (
    MunicipalEnergyProfile,
    OntarioElectricityRates,
    CarbonIntensity,
    OptimizationTargets,
    FLEET_BREAKDOWN,
    MUNICIPAL_PROPERTIES,
)


# ─────────────────────────────────────────────────────────────────────────────
# FACILITY MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Facility:
    """Single municipal facility with energy characteristics."""
    name: str
    address: str
    category: str
    annual_electricity_kwh: float
    annual_gas_m3: float
    gross_area_sqft: float = 0.0
    has_solar: bool = False
    solar_capacity_kw: float = 0.0
    has_battery: bool = False
    battery_capacity_kwh: float = 0.0

    @property
    def electricity_intensity_kwh_per_sqft(self) -> float:
        if self.gross_area_sqft > 0:
            return self.annual_electricity_kwh / self.gross_area_sqft
        return 0.0

    @property
    def monthly_electricity_kwh(self) -> float:
        return self.annual_electricity_kwh / 12.0

    @property
    def estimated_peak_kw(self) -> float:
        """
        [MODELED] Estimate peak demand from annual consumption.
        Assumes ~2,500 equivalent full-load hours for municipal buildings
        in Ontario (typical for mixed commercial/institutional use).
        """
        return self.annual_electricity_kwh / 2_500


# ─────────────────────────────────────────────────────────────────────────────
# BUILDING AREA BY CATEGORY (from CDM Plan Section 4.1)
# ─────────────────────────────────────────────────────────────────────────────

BUILDING_AREAS_SQFT: Dict[str, float] = {
    "Administration":                          40_000,
    "Airport Facilities":                      37_010,
    "Community Centres":                      193_430,
    "Museums and Heritage Sites":               4_946,
    "Parks and Recreation Facilities":         28_745,
    "Fire Stations":                           31_709,
    "Long Term Care Facilities":               85_000,
    "Police Stations":                         22_875,
    "Public Library":                          10_900,
    "Municipal Operations and Env Services":   37_135,
    "Community Support Services":              46_328,
    "Supportive Housing and Mixed-Use":        31_526,
    "Public Housing Apartment Buildings":     240_985,
}


# ─────────────────────────────────────────────────────────────────────────────
# LOAD PROFILE GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def generate_hourly_load_profile(
    annual_kwh: float,
    category: str = "general",
    year: int = 2024,
) -> pd.Series:
    """
    Generate a synthetic hourly load profile for a municipal facility.

    Uses typical Ontario municipal load shapes:
      - Offices/admin: weekday 8am-6pm peak, low overnight
      - Community centres: evening and weekend peaks
      - Long-term care: flat 24/7 with slight daytime elevation
      - Public housing: morning and evening peaks
      - Operations/water treatment: relatively flat with process loads

    Returns a pandas Series indexed by hourly timestamps for the given year.
    """
    hours = pd.date_range(
        start=f"{year}-01-01", end=f"{year}-12-31 23:00", freq="h"
    )
    n_hours = len(hours)
    base_load = annual_kwh / n_hours  # average hourly load

    # Hour-of-day and month-of-year multipliers
    hour_of_day = hours.hour
    month = hours.month
    day_of_week = hours.dayofweek  # 0=Monday, 6=Sunday
    is_weekend = day_of_week >= 5

    # Monthly seasonality (Ontario: higher in summer for cooling, winter for heating pumps)
    monthly_factors = np.array([
        1.08, 1.05, 0.98, 0.92, 0.88, 0.95,
        1.10, 1.12, 0.95, 0.92, 1.00, 1.05
    ])  # Jan-Dec
    seasonal = np.array([monthly_factors[m - 1] for m in month])

    # Category-specific hourly profiles
    if category in ("Administration", "Police Stations", "Public Library"):
        # Office-type: 8am-6pm weekday peak
        hourly_shape = np.where(
            (~is_weekend) & (hour_of_day >= 8) & (hour_of_day < 18),
            1.6,  # daytime occupied
            np.where(
                (~is_weekend) & ((hour_of_day >= 6) & (hour_of_day < 8) |
                                  (hour_of_day >= 18) & (hour_of_day < 21)),
                1.0,  # shoulder hours
                0.5   # overnight / weekend base
            )
        )
    elif category == "Community Centres":
        # Evening and weekend heavy
        hourly_shape = np.where(
            (hour_of_day >= 16) & (hour_of_day < 22),
            1.5,
            np.where(
                is_weekend & (hour_of_day >= 9) & (hour_of_day < 22),
                1.4,
                np.where(
                    (hour_of_day >= 9) & (hour_of_day < 16),
                    1.1,
                    0.4
                )
            )
        )
    elif category == "Long Term Care Facilities":
        # Near-flat 24/7 with slight daytime rise
        hourly_shape = np.where(
            (hour_of_day >= 7) & (hour_of_day < 22),
            1.15,
            0.85
        )
    elif category == "Public Housing Apartment Buildings":
        # Residential: morning + evening peaks
        hourly_shape = np.where(
            (hour_of_day >= 7) & (hour_of_day < 9),
            1.4,  # morning peak
            np.where(
                (hour_of_day >= 17) & (hour_of_day < 22),
                1.5,  # evening peak
                np.where(
                    (hour_of_day >= 9) & (hour_of_day < 17),
                    0.8,  # midday low
                    0.5   # overnight
                )
            )
        )
    elif category == "Municipal Operations and Env Services":
        # Process loads — relatively flat with work-hour bump
        hourly_shape = np.where(
            (hour_of_day >= 6) & (hour_of_day < 18),
            1.2,
            0.8
        )
    else:
        # Default general profile
        hourly_shape = np.where(
            (hour_of_day >= 8) & (hour_of_day < 20),
            1.3,
            0.7
        )

    # Combine and normalize
    raw_profile = base_load * hourly_shape * seasonal
    scaling_factor = annual_kwh / raw_profile.sum()
    normalized_profile = raw_profile * scaling_factor

    return pd.Series(normalized_profile, index=hours, name=f"load_kwh")


# ─────────────────────────────────────────────────────────────────────────────
# SOLAR GENERATION MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SolarArray:
    """
    Rooftop solar PV model for St. Thomas, Ontario.

    St. Thomas coordinates: 42.7743°N, -81.1823°W
    Ontario average solar capacity factor: ~14% (NRCan)
    Average peak sun hours: ~3.4 hrs/day (London, ON nearby station)
    """
    capacity_kw: float
    tilt_deg: float = 35.0        # Optimal for ~43°N latitude
    azimuth_deg: float = 180.0    # South-facing
    capacity_factor: float = 0.14  # Ontario average
    degradation_rate_per_year: float = 0.005  # 0.5% annual panel degradation
    system_losses_pct: float = 0.14  # inverter, wiring, soiling

    @property
    def annual_generation_kwh(self) -> float:
        return self.capacity_kw * self.capacity_factor * 8_760

    def hourly_generation(self, year: int = 2024) -> pd.Series:
        """
        [MODELED] Generate synthetic hourly solar output.
        Uses simplified clear-sky model with seasonal and diurnal patterns.
        """
        hours = pd.date_range(
            start=f"{year}-01-01", end=f"{year}-12-31 23:00", freq="h"
        )
        hour_of_day = hours.hour
        day_of_year = hours.dayofyear

        # Seasonal daylight variation at 42.77°N
        # Solar noon ≈ hour 12-13, sunrise/sunset varies 6-9am / 5-9pm
        declination = 23.45 * np.sin(np.radians((360 / 365) * (day_of_year - 81)))
        day_length_hours = 2 * np.degrees(np.arccos(
            -np.tan(np.radians(42.77)) * np.tan(np.radians(declination))
        )) / 15.0

        sunrise = 12.0 - day_length_hours / 2.0
        sunset = 12.0 + day_length_hours / 2.0

        # Solar intensity (simplified cosine model)
        solar_hour_angle = (hour_of_day - 12.0) * 15.0  # degrees
        is_daylight = (hour_of_day >= sunrise) & (hour_of_day < sunset)

        # Relative intensity (peak at solar noon)
        relative_intensity = np.maximum(
            0, np.cos(np.radians(solar_hour_angle)) *
            np.cos(np.radians(42.77 - declination))
        )
        relative_intensity = np.where(is_daylight, relative_intensity, 0.0)

        # Monthly cloud cover factors for Southwestern Ontario
        cloud_factors = np.array([
            0.55, 0.60, 0.65, 0.70, 0.75, 0.80,
            0.82, 0.80, 0.75, 0.65, 0.55, 0.50
        ])
        monthly_cloud = np.array([cloud_factors[m - 1] for m in hours.month])

        # Scale to match annual generation target
        raw_output = self.capacity_kw * relative_intensity * monthly_cloud
        raw_output *= (1.0 - self.system_losses_pct)

        if raw_output.sum() > 0:
            scaling = self.annual_generation_kwh / raw_output.sum()
            raw_output *= scaling

        return pd.Series(raw_output, index=hours, name="solar_kwh")


# ─────────────────────────────────────────────────────────────────────────────
# BATTERY STORAGE MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BatteryStorage:
    """
    Battery energy storage system (BESS) model.
    Typical municipal-scale Li-ion system.
    """
    capacity_kwh: float           # Total energy capacity
    max_charge_kw: float          # Maximum charge rate
    max_discharge_kw: float       # Maximum discharge rate
    round_trip_efficiency: float = 0.90
    min_soc: float = 0.10        # Minimum state of charge (10%)
    max_soc: float = 0.95        # Maximum state of charge (95%)
    initial_soc: float = 0.50    # Starting state of charge
    cycle_degradation: float = 0.0001  # Capacity loss per full cycle

    @property
    def usable_capacity_kwh(self) -> float:
        return self.capacity_kwh * (self.max_soc - self.min_soc)


# ─────────────────────────────────────────────────────────────────────────────
# FULL MUNICIPAL ENERGY MODEL
# ─────────────────────────────────────────────────────────────────────────────

class MunicipalEnergyModel:
    """
    Complete energy model for the City of St. Thomas.
    Integrates facility loads, solar generation, battery storage,
    and grid connection.
    """

    def __init__(self):
        self.profile = MunicipalEnergyProfile()
        self.rates = OntarioElectricityRates()
        self.carbon = CarbonIntensity()
        self.targets = OptimizationTargets()
        self.facilities: List[Facility] = self._build_facilities()
        self.solar_arrays: List[SolarArray] = []
        self.batteries: List[BatteryStorage] = []

    def _build_facilities(self) -> List[Facility]:
        """Build facility objects from verified CDM data."""
        facilities = []
        for category, elec_kwh in self.profile.electricity_by_category.items():
            gas_m3 = self.profile.natural_gas_by_category.get(category, 0)
            area = BUILDING_AREAS_SQFT.get(category, 0)
            facilities.append(Facility(
                name=category,
                address="(see property listing)",
                category=category,
                annual_electricity_kwh=elec_kwh,
                annual_gas_m3=gas_m3,
                gross_area_sqft=area,
            ))
        return facilities

    @property
    def total_annual_electricity_kwh(self) -> float:
        return sum(f.annual_electricity_kwh for f in self.facilities)

    @property
    def total_estimated_peak_kw(self) -> float:
        """[MODELED] Sum of estimated facility peaks (non-coincident)."""
        return sum(f.estimated_peak_kw for f in self.facilities)

    @property
    def coincident_peak_kw(self) -> float:
        """
        [MODELED] Coincident peak is typically 60-70% of non-coincident sum
        for diversified municipal loads.
        """
        return self.total_estimated_peak_kw * 0.65

    @property
    def annual_electricity_cost(self) -> float:
        """Estimated annual electricity cost using blended rate."""
        return self.total_annual_electricity_kwh * self.rates.blended_average_rate

    @property
    def annual_carbon_tonnes(self) -> float:
        """Annual CO2e from electricity (using 2024 Ontario grid intensity)."""
        return (self.total_annual_electricity_kwh *
                self.carbon.grid_current / 1_000_000)  # g → tonnes

    def add_solar(self, capacity_kw: float) -> SolarArray:
        """Add a solar array to the model."""
        array = SolarArray(capacity_kw=capacity_kw)
        self.solar_arrays.append(array)
        return array

    def add_battery(
        self, capacity_kwh: float, max_power_kw: float
    ) -> BatteryStorage:
        """Add battery storage to the model."""
        battery = BatteryStorage(
            capacity_kwh=capacity_kwh,
            max_charge_kw=max_power_kw,
            max_discharge_kw=max_power_kw,
        )
        self.batteries.append(battery)
        return battery

    def compute_baseline_costs(self) -> Dict[str, float]:
        """Compute baseline annual costs and emissions."""
        elec_cost = self.annual_electricity_cost
        gas_cost_per_m3 = 0.35  # [MODELED] ~$0.35/m³ approximate Ontario gas rate
        gas_total = self.profile.total_natural_gas_m3 * gas_cost_per_m3

        elec_carbon = self.annual_carbon_tonnes
        gas_carbon = (self.profile.total_natural_gas_m3 *
                      self.carbon.natural_gas_kg_co2e_per_m3 / 1_000)

        return {
            "electricity_cost_cad": elec_cost,
            "natural_gas_cost_cad": gas_total,
            "total_energy_cost_cad": elec_cost + gas_total,
            "electricity_co2e_tonnes": elec_carbon,
            "natural_gas_co2e_tonnes": gas_carbon,
            "total_co2e_tonnes": elec_carbon + gas_carbon,
        }

    def compute_optimized_projection(self) -> Dict[str, float]:
        """
        [MODELED] Project costs/emissions after κ-coherence optimization.
        Applies target reduction percentages to baseline.
        """
        baseline = self.compute_baseline_costs()
        reduction = self.targets.consumption_reduction_pct  # 12%

        optimized_elec_kwh = self.total_annual_electricity_kwh * (1 - reduction)
        optimized_elec_cost = optimized_elec_kwh * self.rates.blended_average_rate
        optimized_elec_carbon = optimized_elec_kwh * self.carbon.grid_current / 1e6

        # Gas savings from better HVAC scheduling (conservative 8%)
        gas_reduction = 0.08
        optimized_gas_m3 = self.profile.total_natural_gas_m3 * (1 - gas_reduction)
        optimized_gas_cost = optimized_gas_m3 * 0.35
        optimized_gas_carbon = optimized_gas_m3 * self.carbon.natural_gas_kg_co2e_per_m3 / 1e3

        return {
            "optimized_electricity_kwh": optimized_elec_kwh,
            "electricity_savings_kwh": self.total_annual_electricity_kwh * reduction,
            "electricity_cost_savings_cad": baseline["electricity_cost_cad"] - optimized_elec_cost,
            "gas_cost_savings_cad": baseline["natural_gas_cost_cad"] - optimized_gas_cost,
            "total_cost_savings_cad": (
                (baseline["electricity_cost_cad"] - optimized_elec_cost) +
                (baseline["natural_gas_cost_cad"] - optimized_gas_cost)
            ),
            "co2e_reduction_tonnes": (
                baseline["total_co2e_tonnes"] -
                (optimized_elec_carbon + optimized_gas_carbon)
            ),
            "peak_demand_reduction_kw": self.coincident_peak_kw * self.targets.peak_demand_reduction_pct,
        }

    def summary(self) -> str:
        """Print a human-readable model summary."""
        baseline = self.compute_baseline_costs()
        optimized = self.compute_optimized_projection()

        return f"""
╔══════════════════════════════════════════════════════════════════╗
║  St. Thomas Municipal Energy Model — Summary                    ║
╠══════════════════════════════════════════════════════════════════╣
║  BASELINE (2024 Verified Data)                                   ║
║  Total Electricity:    {self.total_annual_electricity_kwh:>12,.0f} kWh                 ║
║  Total Natural Gas:    {self.profile.total_natural_gas_m3:>12,} m³                  ║
║  Est. Peak Demand:     {self.coincident_peak_kw:>12,.1f} kW (coincident)       ║
║  Annual Elec Cost:   $ {baseline['electricity_cost_cad']:>12,.2f} CAD                ║
║  Annual Gas Cost:    $ {baseline['natural_gas_cost_cad']:>12,.2f} CAD                ║
║  Annual CO₂e:          {baseline['total_co2e_tonnes']:>12,.1f} tonnes              ║
╠══════════════════════════════════════════════════════════════════╣
║  OPTIMIZED PROJECTION [MODELED]                                  ║
║  Elec Savings:         {optimized['electricity_savings_kwh']:>12,.0f} kWh ({self.targets.consumption_reduction_pct*100:.0f}%)        ║
║  Cost Savings:       $ {optimized['total_cost_savings_cad']:>12,.2f} CAD/yr             ║
║  CO₂e Reduction:       {optimized['co2e_reduction_tonnes']:>12,.1f} tonnes/yr          ║
║  Peak Reduction:       {optimized['peak_demand_reduction_kw']:>12,.1f} kW               ║
╚══════════════════════════════════════════════════════════════════╝
"""


if __name__ == "__main__":
    model = MunicipalEnergyModel()
    print(model.summary())
