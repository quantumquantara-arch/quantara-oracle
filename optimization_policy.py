"""
optimization_policy.py — Decision Engine for Municipal Energy Dispatch

Applies κ-coherence scoring and Veyn operators to make real-time dispatch
decisions across St. Thomas municipal facilities. Determines when to:
  - Shift loads to off-peak periods
  - Charge/discharge battery storage
  - Curtail non-essential loads during grid stress
  - Maximize self-consumption of solar generation

Decision Framework:
  κ ≥ 0.7  (COHERENT)      → Normal operations, pre-charge batteries
  0.3 < κ < 0.7  (TRANSITIONAL) → Active load optimization, price-responsive
  κ ≤ 0.3  (STRESSED)      → Emergency demand response, shed non-critical loads
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from config import (
    VeynOperatorConfig,
    OntarioElectricityRates,
    OptimizationTargets,
    MunicipalEnergyProfile,
)
from forecasting_engine import ForecastingEngine, KappaCoherenceScorer


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH ACTIONS
# ─────────────────────────────────────────────────────────────────────────────

class DispatchAction(Enum):
    """Possible dispatch actions the optimization policy can take."""
    NORMAL = "normal"                    # Business as usual
    SHIFT_LOAD = "shift_load"            # Move deferrable loads to off-peak
    CHARGE_BATTERY = "charge_battery"    # Store energy during low-price periods
    DISCHARGE_BATTERY = "discharge_battery"  # Release stored energy during peaks
    CURTAIL_HVAC = "curtail_hvac"        # Reduce HVAC setpoints
    CURTAIL_LIGHTING = "curtail_lighting"  # Dim non-essential lighting
    SOLAR_PRIORITY = "solar_priority"    # Maximize solar self-consumption
    EMERGENCY_SHED = "emergency_shed"    # Shed non-critical loads


@dataclass
class DispatchDecision:
    """A single dispatch decision with rationale."""
    timestamp: pd.Timestamp
    action: DispatchAction
    target_facility: str
    magnitude_kw: float
    kappa_score: float
    coherence_state: str
    estimated_savings_kwh: float
    rationale: str


# ─────────────────────────────────────────────────────────────────────────────
# LOAD CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

# Classifies municipal loads by deferability
# Based on facility categories from St. Thomas CDM Plan

LOAD_CLASSIFICATION: Dict[str, Dict] = {
    "Administration": {
        "critical_pct": 0.30,     # IT, security, emergency systems
        "deferrable_pct": 0.25,   # HVAC setpoint adjustment, lighting dimming
        "sheddable_pct": 0.10,    # Non-essential peripherals, decorative lighting
    },
    "Community Centres": {
        "critical_pct": 0.20,
        "deferrable_pct": 0.35,   # Ice rink scheduling, pool pumps off-peak
        "sheddable_pct": 0.15,
    },
    "Long Term Care Facilities": {
        "critical_pct": 0.70,     # Life safety, medical equipment, heating
        "deferrable_pct": 0.10,   # Laundry timing
        "sheddable_pct": 0.02,    # Minimal shedding allowed
    },
    "Police Stations": {
        "critical_pct": 0.60,
        "deferrable_pct": 0.15,
        "sheddable_pct": 0.05,
    },
    "Fire Stations": {
        "critical_pct": 0.55,
        "deferrable_pct": 0.15,
        "sheddable_pct": 0.05,
    },
    "Public Library": {
        "critical_pct": 0.25,
        "deferrable_pct": 0.30,
        "sheddable_pct": 0.15,
    },
    "Municipal Operations and Env Services": {
        "critical_pct": 0.50,     # Water treatment, sewage must run
        "deferrable_pct": 0.20,   # Pump scheduling optimization
        "sheddable_pct": 0.05,
    },
    "Community Support Services": {
        "critical_pct": 0.35,
        "deferrable_pct": 0.25,
        "sheddable_pct": 0.10,
    },
    "Public Housing Apartment Buildings": {
        "critical_pct": 0.40,     # Essential services, common areas
        "deferrable_pct": 0.20,   # Common area HVAC scheduling
        "sheddable_pct": 0.05,    # Limited due to tenant impacts
    },
    "Parks and Recreation Facilities": {
        "critical_pct": 0.15,
        "deferrable_pct": 0.40,   # Field lighting, irrigation timing
        "sheddable_pct": 0.25,
    },
    "Supportive Housing and Mixed-Use": {
        "critical_pct": 0.45,
        "deferrable_pct": 0.20,
        "sheddable_pct": 0.05,
    },
    "Museums and Heritage Sites": {
        "critical_pct": 0.20,
        "deferrable_pct": 0.30,
        "sheddable_pct": 0.30,
    },
    "Airport Facilities": {
        "critical_pct": 0.25,
        "deferrable_pct": 0.30,
        "sheddable_pct": 0.20,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# TOU PRICE SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

def get_tou_period(timestamp: pd.Timestamp, is_summer: bool = False) -> Tuple[str, float]:
    """
    Determine Ontario TOU period and rate for a given timestamp.

    Ontario TOU periods (Ontario Energy Board):
      Winter (Nov 1 - Apr 30):
        Off-Peak:  7pm-7am weekdays + weekends/holidays  → $0.076/kWh
        Mid-Peak:  11am-5pm weekdays                      → $0.122/kWh
        On-Peak:   7am-11am, 5pm-7pm weekdays             → $0.158/kWh
      Summer (May 1 - Oct 31):
        Off-Peak:  7pm-7am weekdays + weekends/holidays  → $0.076/kWh
        Mid-Peak:  7am-11am, 5pm-7pm weekdays            → $0.122/kWh
        On-Peak:   11am-5pm weekdays                      → $0.158/kWh
    """
    rates = OntarioElectricityRates()
    hour = timestamp.hour
    is_weekend = timestamp.dayofweek >= 5

    if is_weekend or (hour >= 19 or hour < 7):
        return "off_peak", rates.winter_off_peak

    if is_summer:
        # Summer: on-peak 11am-5pm, mid-peak 7am-11am & 5pm-7pm
        if 11 <= hour < 17:
            return "on_peak", rates.summer_on_peak
        else:
            return "mid_peak", rates.summer_mid_peak
    else:
        # Winter: on-peak 7am-11am & 5pm-7pm, mid-peak 11am-5pm
        if (7 <= hour < 11) or (17 <= hour < 19):
            return "on_peak", rates.winter_on_peak
        else:
            return "mid_peak", rates.winter_mid_peak


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMIZATION POLICY
# ─────────────────────────────────────────────────────────────────────────────

class OptimizationPolicy:
    """
    Decision engine that combines κ-coherence states with TOU price signals
    and facility load classifications to produce optimal dispatch decisions.
    """

    def __init__(
        self,
        config: Optional[VeynOperatorConfig] = None,
        targets: Optional[OptimizationTargets] = None,
    ):
        self.config = config or VeynOperatorConfig()
        self.targets = targets or OptimizationTargets()
        self.scorer = KappaCoherenceScorer(config)
        self.forecaster = ForecastingEngine(config)
        self.decisions_log: List[DispatchDecision] = []

    def evaluate(
        self,
        timestamp: pd.Timestamp,
        load_history: pd.Series,
        solar_available_kw: float = 0.0,
        battery_soc: float = 0.5,
        battery_capacity_kwh: float = 0.0,
    ) -> List[DispatchDecision]:
        """
        Evaluate current conditions and produce dispatch decisions.

        Parameters:
            timestamp: Current time
            load_history: Recent hourly load data (at least 24 hours)
            solar_available_kw: Current solar generation (kW)
            battery_soc: Battery state of charge (0-1)
            battery_capacity_kwh: Total battery capacity

        Returns:
            List of DispatchDecision objects
        """
        decisions = []

        # Compute current κ-score
        kappa_series = self.scorer.compute_kappa(load_history)
        current_kappa = float(kappa_series.iloc[-1])
        state = self.scorer.classify_state(current_kappa)

        # Get TOU period
        is_summer = timestamp.month in range(5, 11)
        tou_period, tou_rate = get_tou_period(timestamp, is_summer)

        # Current load
        current_load = float(load_history.iloc[-1])

        # ── COHERENT STATE (κ ≥ 0.7) ──
        if state == "COHERENT":
            # Grid is stable — focus on cost optimization
            if tou_period == "off_peak" and battery_soc < 0.8:
                decisions.append(DispatchDecision(
                    timestamp=timestamp,
                    action=DispatchAction.CHARGE_BATTERY,
                    target_facility="System-wide",
                    magnitude_kw=min(battery_capacity_kwh * 0.25, current_load * 0.3),
                    kappa_score=current_kappa,
                    coherence_state=state,
                    estimated_savings_kwh=0,  # Savings realized on discharge
                    rationale=f"κ={current_kappa:.2f} COHERENT + off-peak: "
                              f"pre-charging battery at ${tou_rate}/kWh",
                ))

            if solar_available_kw > current_load * 0.1:
                decisions.append(DispatchDecision(
                    timestamp=timestamp,
                    action=DispatchAction.SOLAR_PRIORITY,
                    target_facility="System-wide",
                    magnitude_kw=solar_available_kw,
                    kappa_score=current_kappa,
                    coherence_state=state,
                    estimated_savings_kwh=solar_available_kw,
                    rationale=f"Solar producing {solar_available_kw:.1f} kW — "
                              f"maximizing self-consumption",
                ))

        # ── TRANSITIONAL STATE (0.3 < κ < 0.7) ──
        elif state == "TRANSITIONAL":
            # Active optimization — respond to price signals
            if tou_period == "on_peak":
                # Shift deferrable loads
                for category, classification in LOAD_CLASSIFICATION.items():
                    deferrable = classification["deferrable_pct"]
                    if deferrable > 0:
                        decisions.append(DispatchDecision(
                            timestamp=timestamp,
                            action=DispatchAction.SHIFT_LOAD,
                            target_facility=category,
                            magnitude_kw=current_load * deferrable * 0.3,
                            kappa_score=current_kappa,
                            coherence_state=state,
                            estimated_savings_kwh=current_load * deferrable * 0.3,
                            rationale=f"κ={current_kappa:.2f} TRANSITIONAL + "
                                      f"on-peak ${tou_rate}/kWh: deferring "
                                      f"{deferrable*100:.0f}% of {category} load",
                        ))
                        break  # One representative decision per evaluation

                # Discharge battery during on-peak if available
                if battery_soc > 0.3 and battery_capacity_kwh > 0:
                    discharge_kw = min(
                        battery_capacity_kwh * 0.2,
                        current_load * 0.15
                    )
                    savings = discharge_kw * (tou_rate - 0.076)  # vs off-peak rate
                    decisions.append(DispatchDecision(
                        timestamp=timestamp,
                        action=DispatchAction.DISCHARGE_BATTERY,
                        target_facility="System-wide",
                        magnitude_kw=discharge_kw,
                        kappa_score=current_kappa,
                        coherence_state=state,
                        estimated_savings_kwh=discharge_kw,
                        rationale=f"Discharging {discharge_kw:.1f} kW from battery "
                                  f"(SOC={battery_soc:.0%}) during on-peak "
                                  f"to save ${savings:.2f}/hr",
                    ))

        # ── STRESSED STATE (κ ≤ 0.3) ──
        elif state == "STRESSED":
            # Emergency response — shed non-critical loads
            for category, classification in LOAD_CLASSIFICATION.items():
                sheddable = classification["sheddable_pct"]
                if sheddable >= 0.10:  # Only shed categories with ≥10%
                    decisions.append(DispatchDecision(
                        timestamp=timestamp,
                        action=DispatchAction.EMERGENCY_SHED,
                        target_facility=category,
                        magnitude_kw=current_load * sheddable,
                        kappa_score=current_kappa,
                        coherence_state=state,
                        estimated_savings_kwh=current_load * sheddable,
                        rationale=f"κ={current_kappa:.2f} STRESSED: shedding "
                                  f"{sheddable*100:.0f}% of {category} "
                                  f"non-critical load",
                    ))

            # HVAC curtailment across all facilities
            decisions.append(DispatchDecision(
                timestamp=timestamp,
                action=DispatchAction.CURTAIL_HVAC,
                target_facility="All facilities",
                magnitude_kw=current_load * 0.08,
                kappa_score=current_kappa,
                coherence_state=state,
                estimated_savings_kwh=current_load * 0.08,
                rationale=f"κ={current_kappa:.2f} STRESSED: adjusting HVAC "
                          f"setpoints ±2°C across all facilities",
            ))

            # Full battery discharge if available
            if battery_soc > 0.15 and battery_capacity_kwh > 0:
                decisions.append(DispatchDecision(
                    timestamp=timestamp,
                    action=DispatchAction.DISCHARGE_BATTERY,
                    target_facility="System-wide",
                    magnitude_kw=battery_capacity_kwh * 0.3,
                    kappa_score=current_kappa,
                    coherence_state=state,
                    estimated_savings_kwh=battery_capacity_kwh * 0.3,
                    rationale=f"Emergency battery discharge during grid stress",
                ))

        self.decisions_log.extend(decisions)
        return decisions

    def compute_annual_savings_estimate(
        self,
        load_profile: pd.Series,
    ) -> Dict[str, float]:
        """
        [MODELED] Estimate annual savings from applying the optimization policy
        to a full year of load data.
        """
        rates = OntarioElectricityRates()
        profile = MunicipalEnergyProfile()

        total_kwh = profile.total_electricity_kwh
        baseline_cost = total_kwh * rates.blended_average_rate

        # Savings breakdown
        tou_shift_savings_pct = 0.06   # 6% from TOU shifting
        hvac_savings_pct = 0.04        # 4% from HVAC optimization
        lighting_savings_pct = 0.02    # 2% from lighting controls

        total_savings_pct = (tou_shift_savings_pct +
                            hvac_savings_pct +
                            lighting_savings_pct)

        return {
            "baseline_annual_cost_cad": baseline_cost,
            "tou_shift_savings_cad": baseline_cost * tou_shift_savings_pct,
            "hvac_optimization_savings_cad": baseline_cost * hvac_savings_pct,
            "lighting_control_savings_cad": baseline_cost * lighting_savings_pct,
            "total_annual_savings_cad": baseline_cost * total_savings_pct,
            "total_savings_pct": total_savings_pct,
            "kwh_saved": total_kwh * total_savings_pct,
        }


if __name__ == "__main__":
    from municipal_energy_model import generate_hourly_load_profile

    profile = MunicipalEnergyProfile()

    # Generate test load
    load = generate_hourly_load_profile(
        annual_kwh=profile.total_electricity_kwh,
        category="general",
        year=2024,
    )

    policy = OptimizationPolicy()

    # Simulate decisions for a sample day
    print("=" * 70)
    print("  Optimization Policy — Sample Day Dispatch")
    print("=" * 70)

    sample_day = load["2024-06-15":"2024-06-15"]
    for i in range(0, min(24, len(sample_day)), 6):  # Every 6 hours
        ts = sample_day.index[i]
        history = load[:ts]
        if len(history) < 25:
            continue

        decisions = policy.evaluate(
            timestamp=ts,
            load_history=history.tail(168),  # Last 7 days
            solar_available_kw=50 if 9 <= ts.hour <= 17 else 0,
            battery_soc=0.6,
            battery_capacity_kwh=200,
        )

        if decisions:
            print(f"\n  {ts} — κ={decisions[0].kappa_score:.3f} [{decisions[0].coherence_state}]")
            for d in decisions:
                print(f"    → {d.action.value}: {d.target_facility} "
                      f"({d.magnitude_kw:.1f} kW)")
                print(f"      {d.rationale}")
        else:
            print(f"\n  {ts} — No dispatch actions needed")

    # Annual savings estimate
    print("\n" + "=" * 70)
    savings = policy.compute_annual_savings_estimate(load)
    print(f"  Estimated Annual Savings:")
    for k, v in savings.items():
        if "cad" in k:
            print(f"    {k}: ${v:,.2f}")
        elif "pct" in k:
            print(f"    {k}: {v:.1%}")
        else:
            print(f"    {k}: {v:,.0f}")
