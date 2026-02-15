"""
st_thomas_energy_optimization.py — Main Orchestration Engine

Coordinates all optimization modules for the St. Thomas municipal energy system:
  1. Loads verified municipal data from config
  2. Builds the energy model (facilities, solar, battery)
  3. Generates load forecasts via Veyn operators
  4. Computes κ-coherence scores
  5. Runs optimization policy to produce dispatch decisions
  6. Optimizes battery dispatch via LP
  7. Outputs comprehensive analysis with ROI projections

Data lineage:
  - All baseline consumption data from City of St. Thomas CDM Plan 2025-2029
  - Electricity rates from Ontario Energy Board (via Entegrus)
  - Carbon intensity from CER Provincial Profile & GTHA Carbon Inventory
  - ENTSO-E integration for European market reference (optional)
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Optional

from config import (
    MunicipalEnergyProfile,
    OntarioElectricityRates,
    CarbonIntensity,
    VeynOperatorConfig,
    OptimizationTargets,
    ENTSOE_API_KEY,
    LOG_LEVEL,
    LOG_FORMAT,
)
from municipal_energy_model import (
    MunicipalEnergyModel,
    BatteryStorage,
    SolarArray,
    generate_hourly_load_profile,
)
from forecasting_engine import (
    ForecastingEngine,
    VeynOperator,
    KappaCoherenceScorer,
    ENTSOECoherenceAnalyzer,
)
from optimization_policy import OptimizationPolicy
from battery_dispatch import BatteryDispatchOptimizer, EVFleetSchedule


# Configure logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)
logger = logging.getLogger("st_thomas_optimization")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

class StThomasEnergyOptimizer:
    """
    Top-level orchestrator for the St. Thomas Municipal Energy Optimization System.

    Usage:
        optimizer = StThomasEnergyOptimizer()
        report = optimizer.run_full_analysis()
        optimizer.print_report(report)
    """

    def __init__(
        self,
        veyn_config: Optional[VeynOperatorConfig] = None,
        optimization_targets: Optional[OptimizationTargets] = None,
        battery_capacity_kwh: float = 200.0,
        battery_power_kw: float = 50.0,
        solar_capacity_kw: float = 100.0,
    ):
        self.veyn_config = veyn_config or VeynOperatorConfig()
        self.targets = optimization_targets or OptimizationTargets()

        # Initialize subsystems
        logger.info("Initializing St. Thomas Energy Optimization System")

        self.model = MunicipalEnergyModel()
        self.forecaster = ForecastingEngine(self.veyn_config)
        self.policy = OptimizationPolicy(self.veyn_config, self.targets)
        self.kappa_scorer = KappaCoherenceScorer(self.veyn_config)

        # Add solar and battery to model
        self.solar = self.model.add_solar(solar_capacity_kw)
        self.battery_storage = BatteryStorage(
            capacity_kwh=battery_capacity_kwh,
            max_charge_kw=battery_power_kw,
            max_discharge_kw=battery_power_kw,
        )
        self.battery_optimizer = BatteryDispatchOptimizer(self.battery_storage)

        logger.info(
            f"System initialized: {self.model.profile.total_properties} properties, "
            f"{self.model.total_annual_electricity_kwh:,.0f} kWh annual load, "
            f"{solar_capacity_kw} kW solar, {battery_capacity_kwh} kWh storage"
        )

    def generate_annual_load_profiles(self, year: int = 2024) -> Dict[str, pd.Series]:
        """Generate hourly load profiles for all facility categories."""
        profiles = {}
        for category, kwh in self.model.profile.electricity_by_category.items():
            profiles[category] = generate_hourly_load_profile(
                annual_kwh=kwh,
                category=category,
                year=year,
            )
        return profiles

    def generate_aggregate_load(self, year: int = 2024) -> pd.Series:
        """Generate aggregate hourly load for all municipal facilities."""
        profiles = self.generate_annual_load_profiles(year)
        aggregate = sum(profiles.values())
        aggregate.name = "total_load_kwh"
        return aggregate

    def run_coherence_analysis(
        self, load: Optional[pd.Series] = None
    ) -> Dict[str, float]:
        """
        Run κ-coherence analysis on the municipal load profile.
        """
        if load is None:
            load = self.generate_aggregate_load()

        report = self.forecaster.compute_grid_coherence_report(load)

        # Add facility-level coherence scores
        profiles = self.generate_annual_load_profiles()
        facility_kappas = {}
        for category, profile in profiles.items():
            cat_report = self.forecaster.compute_grid_coherence_report(profile)
            facility_kappas[category] = cat_report["mean_kappa"]

        report["facility_kappas"] = facility_kappas
        return report

    def run_optimization_simulation(
        self,
        simulation_days: int = 7,
        start_date: str = "2024-07-01",
    ) -> Dict:
        """
        Simulate the optimization system over a period.
        """
        load = self.generate_aggregate_load()
        solar_gen = self.solar.hourly_generation()

        # Select simulation period
        end_date = pd.Timestamp(start_date) + pd.Timedelta(days=simulation_days)
        sim_load = load[start_date:str(end_date)]
        sim_solar = solar_gen[start_date:str(end_date)]

        if len(sim_load) == 0:
            logger.warning("No load data for simulation period, using full year")
            sim_load = load.head(simulation_days * 24)
            sim_solar = solar_gen.head(simulation_days * 24)

        # Run battery optimization
        battery_schedule = self.battery_optimizer.optimize(
            load_forecast=sim_load,
            solar_forecast=sim_solar,
        )

        # Compute κ-scores over simulation
        kappa = self.kappa_scorer.compute_kappa(sim_load)

        # Run policy decisions (sample at 6-hour intervals)
        decisions = []
        for i in range(0, len(sim_load), 6):
            ts = sim_load.index[i]
            history = load[:ts].tail(168)
            if len(history) < 25:
                continue

            solar_kw = float(sim_solar.iloc[i]) if i < len(sim_solar) else 0
            soc = float(battery_schedule.soc[min(i, len(battery_schedule.soc) - 1)])

            batch = self.policy.evaluate(
                timestamp=ts,
                load_history=history,
                solar_available_kw=solar_kw,
                battery_soc=soc,
                battery_capacity_kwh=self.battery_storage.capacity_kwh,
            )
            decisions.extend(batch)

        return {
            "period": f"{start_date} to {end_date}",
            "load_kwh": float(sim_load.sum()),
            "solar_kwh": float(sim_solar.sum()),
            "battery_savings_cad": battery_schedule.savings,
            "battery_baseline_cost_cad": battery_schedule.baseline_cost,
            "mean_kappa": float(kappa.mean()),
            "min_kappa": float(kappa.min()),
            "max_kappa": float(kappa.max()),
            "n_decisions": len(decisions),
            "decisions_by_type": self._count_decisions(decisions),
            "kappa_series": kappa,
            "load_series": sim_load,
            "solar_series": sim_solar,
            "battery_schedule": battery_schedule,
        }

    def _count_decisions(self, decisions) -> Dict[str, int]:
        counts = {}
        for d in decisions:
            action = d.action.value
            counts[action] = counts.get(action, 0) + 1
        return counts

    def run_full_analysis(self) -> Dict:
        """
        Execute complete analysis pipeline and generate comprehensive report.
        """
        logger.info("Starting full analysis pipeline")

        # 1. Baseline computation
        baseline = self.model.compute_baseline_costs()
        logger.info(f"Baseline: {baseline['total_energy_cost_cad']:,.2f} CAD/yr")

        # 2. Optimization projection
        optimized = self.model.compute_optimized_projection()
        logger.info(f"Projected savings: {optimized['total_cost_savings_cad']:,.2f} CAD/yr")

        # 3. Coherence analysis
        coherence = self.run_coherence_analysis()
        logger.info(f"Mean κ-coherence: {coherence['mean_kappa']:.4f}")

        # 4. Battery economics
        battery_econ = self.battery_optimizer.annual_savings_estimate(
            self.model.total_annual_electricity_kwh
        )

        # 5. Solar economics
        solar_gen_kwh = self.solar.annual_generation_kwh
        solar_value = solar_gen_kwh * self.model.rates.blended_average_rate
        solar_cost = self.solar.capacity_kw * 2000  # ~$2000/kW installed
        solar_payback = solar_cost / solar_value if solar_value > 0 else float("inf")

        # 6. EV Fleet
        ev_fleet = EVFleetSchedule()

        # 7. Simulation (1-week sample)
        simulation = self.run_optimization_simulation(simulation_days=7)

        # 8. ROI Summary
        total_annual_savings = (
            optimized["total_cost_savings_cad"] +
            battery_econ["annual_net_savings_cad"] +
            solar_value
        )
        total_investment = solar_cost + battery_econ["battery_installed_cost_cad"]

        return {
            "timestamp": datetime.now().isoformat(),
            "city": self.model.profile.city_name,
            "baseline": baseline,
            "optimized_projection": optimized,
            "coherence_analysis": coherence,
            "battery_economics": battery_econ,
            "solar": {
                "capacity_kw": self.solar.capacity_kw,
                "annual_generation_kwh": solar_gen_kwh,
                "annual_value_cad": solar_value,
                "installed_cost_cad": solar_cost,
                "payback_years": solar_payback,
            },
            "ev_fleet": {
                "current_evs": ev_fleet.n_evs,
                "daily_charge_kwh": ev_fleet.daily_fleet_charge_kwh,
                "annual_charge_kwh": ev_fleet.annual_fleet_charge_kwh,
                "optimal_window": ev_fleet.optimal_charging_window(),
            },
            "simulation_sample": {
                "period": simulation["period"],
                "mean_kappa": simulation["mean_kappa"],
                "battery_savings_cad": simulation["battery_savings_cad"],
                "n_decisions": simulation["n_decisions"],
                "decisions_by_type": simulation["decisions_by_type"],
            },
            "roi_summary": {
                "total_annual_savings_cad": total_annual_savings,
                "total_investment_cad": total_investment,
                "simple_payback_years": total_investment / total_annual_savings if total_annual_savings > 0 else float("inf"),
                "co2e_reduction_tonnes_per_year": optimized["co2e_reduction_tonnes"],
            },
        }

    def print_report(self, report: Dict) -> str:
        """Format and print the full analysis report."""
        b = report["baseline"]
        o = report["optimized_projection"]
        c = report["coherence_analysis"]
        bat = report["battery_economics"]
        sol = report["solar"]
        ev = report["ev_fleet"]
        roi = report["roi_summary"]

        output = f"""
╔══════════════════════════════════════════════════════════════════════╗
║         QUANTARA ORACLE — ST. THOMAS ENERGY OPTIMIZATION            ║
║         Comprehensive Analysis Report                                ║
║         Generated: {report['timestamp'][:19]}                              ║
╠══════════════════════════════════════════════════════════════════════╣

  DATA SOURCES (Verified):
  • City of St. Thomas CDM Plan 2025-2029 (official municipal report)
  • Ontario Energy Board TOU Rates (effective Nov 1, 2024)
  • CER Provincial Profile — Ontario Grid Carbon Intensity
  • GTHA Carbon Emissions Inventory 2024
  • Statistics Canada Census 2021

══════════════════════════════════════════════════════════════════════
  1. BASELINE ENERGY PROFILE (2024)
══════════════════════════════════════════════════════════════════════

  Total Electricity:       {self.model.total_annual_electricity_kwh:>14,.0f} kWh
  Total Natural Gas:       {self.model.profile.total_natural_gas_m3:>14,} m³
  Municipal Properties:    {self.model.profile.total_properties:>14}
  Building Area:           {self.model.profile.total_gross_building_area_sqft:>14,} sq ft
  Fleet Vehicles:          {self.model.profile.total_fleet_vehicles:>14}
  Streetlights:            {self.model.profile.total_streetlights:>14,}

  Est. Coincident Peak:    {self.model.coincident_peak_kw:>14,.1f} kW
  Annual Electricity Cost: ${b['electricity_cost_cad']:>13,.2f} CAD
  Annual Gas Cost:         ${b['natural_gas_cost_cad']:>13,.2f} CAD
  Total Energy Cost:       ${b['total_energy_cost_cad']:>13,.2f} CAD
  Annual CO₂e Emissions:   {b['total_co2e_tonnes']:>14,.1f} tonnes

══════════════════════════════════════════════════════════════════════
  2. κ-COHERENCE ANALYSIS (Veyn Operator)
══════════════════════════════════════════════════════════════════════

  Mean κ-Score:            {c['mean_kappa']:>14.4f}
  Min κ-Score:             {c['min_kappa']:>14.4f}
  Max κ-Score:             {c['max_kappa']:>14.4f}
  Current State:           {c['state']:>14}
  Circular Coherence:      {c['circular_coherence']:>14.4f}
  Signal-to-Noise:         {c['signal_to_noise']:>14.4f}

  Interpretation:
    κ ≥ 0.7  COHERENT     — Stable grid, predictable load
    0.3–0.7  TRANSITIONAL — Active optimization recommended
    κ ≤ 0.3  STRESSED     — Demand response needed

══════════════════════════════════════════════════════════════════════
  3. OPTIMIZATION PROJECTION [MODELED]
══════════════════════════════════════════════════════════════════════

  Electricity Savings:     {o['electricity_savings_kwh']:>14,.0f} kWh ({self.targets.consumption_reduction_pct*100:.0f}%)
  Cost Savings:            ${o['total_cost_savings_cad']:>13,.2f} CAD/yr
  CO₂e Reduction:          {o['co2e_reduction_tonnes']:>14,.1f} tonnes/yr
  Peak Demand Reduction:   {o['peak_demand_reduction_kw']:>14,.1f} kW ({self.targets.peak_demand_reduction_pct*100:.0f}%)

══════════════════════════════════════════════════════════════════════
  4. SOLAR + BATTERY ECONOMICS
══════════════════════════════════════════════════════════════════════

  Solar Array:             {sol['capacity_kw']:>14,.0f} kW
  Annual Generation:       {sol['annual_generation_kwh']:>14,.0f} kWh
  Annual Value:            ${sol['annual_value_cad']:>13,.2f} CAD
  Solar Payback:           {sol['payback_years']:>14.1f} years

  Battery System:          {self.battery_storage.capacity_kwh:>14,.0f} kWh
  TOU Arbitrage Spread:    ${bat['tou_spread_per_kwh']:>13.3f} /kWh
  Annual Gross Savings:    ${bat['annual_gross_savings_cad']:>13,.2f} CAD
  Battery Payback:         {bat['simple_payback_years']:>14.1f} years

══════════════════════════════════════════════════════════════════════
  5. EV FLEET STATUS
══════════════════════════════════════════════════════════════════════

  Current EVs in Fleet:    {ev['current_evs']:>14}
  Daily Charge Need:       {ev['daily_charge_kwh']:>14.1f} kWh
  Annual Charge:           {ev['annual_charge_kwh']:>14,.0f} kWh
  Optimal Charging:        {ev['optimal_window']}

══════════════════════════════════════════════════════════════════════
  6. ROI SUMMARY
══════════════════════════════════════════════════════════════════════

  Total Annual Savings:    ${roi['total_annual_savings_cad']:>13,.2f} CAD
  Total Investment:        ${roi['total_investment_cad']:>13,.2f} CAD
  Simple Payback:          {roi['simple_payback_years']:>14.1f} years
  CO₂e Reduction:          {roi['co2e_reduction_tonnes_per_year']:>14,.1f} tonnes/yr

╚══════════════════════════════════════════════════════════════════════╝
"""
        print(output)
        return output


# ─────────────────────────────────────────────────────────────────────────────
# ENTSO-E EUROPEAN MARKET CALIBRATION (Optional)
# ─────────────────────────────────────────────────────────────────────────────

def run_entsoe_calibration(api_key: str = "", country: str = "DE_LU", days: int = 7):
    """
    Run ENTSO-E market analysis for Veyn operator calibration.
    Requires a valid ENTSO-E API key.
    """
    if not api_key:
        api_key = ENTSOE_API_KEY
    if not api_key:
        print("ENTSO-E API key not configured. Set ENTSOE_API_KEY environment variable.")
        print("Register at https://transparency.entsoe.eu/ and email")
        print("transparency@entsoe.eu with subject 'Restful API access'")
        return None

    analyzer = ENTSOECoherenceAnalyzer(api_key)
    report = analyzer.compute_market_kappa(country_code=country, days=days)

    print(f"\nENTSO-E Market Coherence — {country} ({days} days)")
    print(f"  Mean Price:      €{report['price_mean_eur_mwh']:.2f}/MWh")
    print(f"  Price Std Dev:   €{report['price_std_eur_mwh']:.2f}/MWh")
    print(f"  κ-Coherence:     {report['mean_kappa']:.4f}")
    print(f"  State:           {report['state']}")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="St. Thomas Municipal Energy Optimization System"
    )
    parser.add_argument(
        "--solar-kw", type=float, default=100.0,
        help="Solar array capacity in kW (default: 100)"
    )
    parser.add_argument(
        "--battery-kwh", type=float, default=200.0,
        help="Battery capacity in kWh (default: 200)"
    )
    parser.add_argument(
        "--battery-kw", type=float, default=50.0,
        help="Battery charge/discharge power in kW (default: 50)"
    )
    parser.add_argument(
        "--entsoe-key", type=str, default="",
        help="ENTSO-E API key for European market calibration"
    )

    args = parser.parse_args()

    # Run main analysis
    optimizer = StThomasEnergyOptimizer(
        solar_capacity_kw=args.solar_kw,
        battery_capacity_kwh=args.battery_kwh,
        battery_power_kw=args.battery_kw,
    )

    report = optimizer.run_full_analysis()
    optimizer.print_report(report)

    # Optional ENTSO-E calibration
    if args.entsoe_key:
        run_entsoe_calibration(args.entsoe_key)
