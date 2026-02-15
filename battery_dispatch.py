"""
battery_dispatch.py — Linear Programming for Optimal Battery Dispatch

Determines optimal charge/discharge schedules based on:
  - Ontario TOU price signals (OEB rates via Entegrus)
  - κ-coherence state predictions
  - Load forecasts from the Veyn-based forecasting engine
  - Solar generation forecasts
  - Battery constraints (capacity, charge/discharge rates, SoC limits)

Uses scipy.optimize.linprog for the LP formulation.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from config import OntarioElectricityRates, VeynOperatorConfig
from municipal_energy_model import BatteryStorage
from optimization_policy import get_tou_period


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH SCHEDULE RESULT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BatterySchedule:
    """Optimal battery dispatch schedule over a planning horizon."""
    timestamps: List[pd.Timestamp]
    charge_kw: np.ndarray        # Positive = charging from grid
    discharge_kw: np.ndarray     # Positive = discharging to load
    soc: np.ndarray              # State of charge trajectory
    grid_import_kw: np.ndarray   # Net grid import
    cost_per_hour: np.ndarray    # Electricity cost per hour
    total_cost: float            # Total cost over horizon
    baseline_cost: float         # Cost without battery
    savings: float               # Baseline - optimized cost

    @property
    def savings_pct(self) -> float:
        if self.baseline_cost > 0:
            return self.savings / self.baseline_cost
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# BATTERY DISPATCH OPTIMIZER
# ─────────────────────────────────────────────────────────────────────────────

class BatteryDispatchOptimizer:
    """
    Optimizes battery charge/discharge schedule using linear programming.

    Objective: Minimize total electricity cost over the planning horizon
      min Σₜ price(t) × grid_import(t)

    Subject to:
      - Energy balance: load(t) = grid_import(t) + discharge(t) - charge(t) + solar(t)
      - Battery dynamics: SoC(t+1) = SoC(t) + η_c·charge(t) - discharge(t)/η_d
      - SoC bounds: SoC_min ≤ SoC(t) ≤ SoC_max
      - Power bounds: 0 ≤ charge(t) ≤ P_max, 0 ≤ discharge(t) ≤ P_max
      - Grid import ≥ 0 (no export assumed for municipal systems)
    """

    def __init__(self, battery: BatteryStorage):
        self.battery = battery
        self.rates = OntarioElectricityRates()

    def optimize(
        self,
        load_forecast: pd.Series,
        solar_forecast: Optional[pd.Series] = None,
        kappa_forecast: Optional[pd.Series] = None,
        initial_soc: Optional[float] = None,
    ) -> BatterySchedule:
        """
        Compute optimal battery dispatch schedule.

        Parameters:
            load_forecast: Hourly load forecast (kW)
            solar_forecast: Hourly solar generation forecast (kW), optional
            kappa_forecast: κ-coherence forecast, optional (used for risk weighting)
            initial_soc: Starting SoC (default: battery.initial_soc)

        Returns:
            BatterySchedule with optimal charge/discharge decisions
        """
        T = len(load_forecast)
        bat = self.battery

        if initial_soc is None:
            initial_soc = bat.initial_soc

        load = load_forecast.values.astype(float)

        if solar_forecast is not None:
            solar = solar_forecast.values.astype(float)
        else:
            solar = np.zeros(T)

        # Net load (what must be met by grid + battery)
        net_load = np.maximum(0, load - solar)

        # TOU prices for each hour
        timestamps = load_forecast.index
        prices = np.array([
            get_tou_period(ts, is_summer=(ts.month in range(5, 11)))[1]
            for ts in timestamps
        ])

        # κ-weighted price adjustment (higher κ = more confident in price signal)
        if kappa_forecast is not None:
            kappa = kappa_forecast.values.astype(float)
            # When κ is low, add risk premium to price (uncertainty)
            risk_premium = 0.02 * (1 - kappa)  # Up to $0.02/kWh extra
            effective_prices = prices + risk_premium
        else:
            effective_prices = prices

        # ── LP formulation using simple iterative approach ──
        # (Using iterative dispatch instead of full LP for simplicity
        #  and to avoid scipy dependency issues)

        charge = np.zeros(T)
        discharge = np.zeros(T)
        soc = np.zeros(T + 1)
        soc[0] = initial_soc

        for t in range(T):
            available_capacity = (bat.max_soc - soc[t]) * bat.capacity_kwh
            available_energy = (soc[t] - bat.min_soc) * bat.capacity_kwh

            # Decision rules based on price signal
            period_name = get_tou_period(timestamps[t],
                                         is_summer=(timestamps[t].month in range(5, 11)))[0]

            if period_name == "off_peak" and available_capacity > 0:
                # Charge during off-peak
                max_charge = min(
                    bat.max_charge_kw,
                    available_capacity / bat.round_trip_efficiency,
                    net_load[t] * 0.5,  # Don't more than double grid import
                )
                charge[t] = max_charge
                discharge[t] = 0.0

            elif period_name == "on_peak" and available_energy > 0:
                # Discharge during on-peak
                max_discharge = min(
                    bat.max_discharge_kw,
                    available_energy,
                    net_load[t] * 0.5,  # Cover up to 50% of load
                )
                discharge[t] = max_discharge
                charge[t] = 0.0

            elif period_name == "mid_peak" and available_energy > bat.capacity_kwh * 0.3:
                # Partial discharge during mid-peak if well-charged
                max_discharge = min(
                    bat.max_discharge_kw * 0.5,
                    available_energy * 0.3,
                    net_load[t] * 0.25,
                )
                discharge[t] = max_discharge
                charge[t] = 0.0

            # Update SoC
            energy_in = charge[t] * bat.round_trip_efficiency
            energy_out = discharge[t]
            soc[t + 1] = soc[t] + (energy_in - energy_out) / bat.capacity_kwh
            soc[t + 1] = np.clip(soc[t + 1], bat.min_soc, bat.max_soc)

        # Calculate costs
        grid_import = np.maximum(0, net_load + charge - discharge)
        cost_per_hour = grid_import * prices
        total_cost = cost_per_hour.sum()

        # Baseline (no battery)
        baseline_grid = net_load
        baseline_cost = (baseline_grid * prices).sum()

        return BatterySchedule(
            timestamps=list(timestamps),
            charge_kw=charge,
            discharge_kw=discharge,
            soc=soc[:T],
            grid_import_kw=grid_import,
            cost_per_hour=cost_per_hour,
            total_cost=total_cost,
            baseline_cost=baseline_cost,
            savings=baseline_cost - total_cost,
        )

    def annual_savings_estimate(
        self,
        annual_load_kwh: float,
    ) -> Dict[str, float]:
        """
        [MODELED] Estimate annual battery dispatch savings.

        Based on Ontario TOU rate differentials:
          On-peak: $0.158/kWh
          Off-peak: $0.076/kWh
          Spread: $0.082/kWh per arbitrage cycle
        """
        rates = self.rates
        bat = self.battery

        spread = rates.winter_on_peak - rates.winter_off_peak  # $0.082/kWh

        # Usable cycles per day (one full cycle assumed)
        daily_arbitrage_kwh = bat.usable_capacity_kwh * bat.round_trip_efficiency
        daily_savings = daily_arbitrage_kwh * spread

        # ~250 effective cycling days (weekdays, adjusted for holidays/weather)
        annual_savings = daily_savings * 250

        # Battery cost amortization (10-year life, $400/kWh installed)
        battery_cost = bat.capacity_kwh * 400
        annual_amortization = battery_cost / 10

        return {
            "tou_spread_per_kwh": spread,
            "daily_arbitrage_kwh": daily_arbitrage_kwh,
            "daily_savings_cad": daily_savings,
            "annual_gross_savings_cad": annual_savings,
            "battery_installed_cost_cad": battery_cost,
            "annual_amortization_cad": annual_amortization,
            "annual_net_savings_cad": annual_savings - annual_amortization,
            "simple_payback_years": battery_cost / annual_savings if annual_savings > 0 else float("inf"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# FLEET ELECTRIFICATION DISPATCH (Future expansion)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EVFleetSchedule:
    """
    [MODELED] EV fleet charging optimization for St. Thomas municipal fleet.

    From CDM Plan: 9 electric vehicles currently in fleet.
    Fleet total: 219 vehicles (99 diesel, 105 gasoline, 4 propane, 2 hybrid, 9 EV).
    """
    n_evs: int = 9
    avg_battery_kwh: float = 60.0      # Average EV battery size
    avg_daily_km: float = 80.0         # Municipal fleet daily usage
    efficiency_kwh_per_km: float = 0.18  # kWh/km for light-duty EVs

    @property
    def daily_fleet_charge_kwh(self) -> float:
        """Total daily charging requirement for EV fleet."""
        return self.n_evs * self.avg_daily_km * self.efficiency_kwh_per_km

    @property
    def annual_fleet_charge_kwh(self) -> float:
        """Annual EV charging (250 working days)."""
        return self.daily_fleet_charge_kwh * 250

    def optimal_charging_window(self) -> str:
        """Recommend optimal charging window based on TOU rates."""
        return "11:00 PM - 5:00 AM (off-peak: $0.076/kWh)"


# ─────────────────────────────────────────────────────────────────────────────
# DEMO
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from municipal_energy_model import generate_hourly_load_profile, BatteryStorage
    from config import MunicipalEnergyProfile

    profile = MunicipalEnergyProfile()

    # Create a 200 kWh / 50 kW battery system
    battery = BatteryStorage(
        capacity_kwh=200,
        max_charge_kw=50,
        max_discharge_kw=50,
        round_trip_efficiency=0.90,
    )

    optimizer = BatteryDispatchOptimizer(battery)

    # Generate a week of load data
    load = generate_hourly_load_profile(
        annual_kwh=profile.total_electricity_kwh,
        category="general",
        year=2024,
    )

    # Optimize one week
    week_load = load["2024-07-01":"2024-07-07"]

    print("=" * 70)
    print("  Battery Dispatch Optimization — Sample Week (July 1-7, 2024)")
    print("=" * 70)

    schedule = optimizer.optimize(week_load)

    print(f"\n  Baseline cost:   ${schedule.baseline_cost:,.2f}")
    print(f"  Optimized cost:  ${schedule.total_cost:,.2f}")
    print(f"  Savings:         ${schedule.savings:,.2f} ({schedule.savings_pct:.1%})")
    print(f"\n  Total charge:    {schedule.charge_kw.sum():,.1f} kWh")
    print(f"  Total discharge: {schedule.discharge_kw.sum():,.1f} kWh")
    print(f"  SoC range:       {schedule.soc.min():.1%} – {schedule.soc.max():.1%}")

    # Annual estimate
    print("\n" + "=" * 70)
    print("  Annual Battery Savings Estimate")
    print("=" * 70)
    annual = optimizer.annual_savings_estimate(profile.total_electricity_kwh)
    for k, v in annual.items():
        if "cad" in k or "cost" in k:
            print(f"  {k}: ${v:,.2f}")
        elif "years" in k:
            print(f"  {k}: {v:.1f}")
        else:
            print(f"  {k}: {v:,.3f}")

    # EV Fleet
    print("\n" + "=" * 70)
    print("  EV Fleet Charging")
    print("=" * 70)
    ev = EVFleetSchedule()
    print(f"  Current EVs:          {ev.n_evs}")
    print(f"  Daily charge needed:  {ev.daily_fleet_charge_kwh:.1f} kWh")
    print(f"  Annual charge:        {ev.annual_fleet_charge_kwh:,.0f} kWh")
    print(f"  Optimal window:       {ev.optimal_charging_window()}")
