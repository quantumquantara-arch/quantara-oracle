"""
forecasting_engine.py — Time-Series Forecasting with Veyn Operators & κ-Coherence

The Veyn coherence operator smooths temporal noise while preserving underlying
patterns that indicate grid stress or surplus. The κ-score transforms raw market
volatility (or load variability) into a stable predictive signal.

Architecture:
  1. Ingest time-series data (load, price, generation)
  2. Apply Veyn temporal smoothing (exponential decay + φ-harmonic layers)
  3. Compute κ-coherence score via π→φ→e reasoning loop
  4. Output predictive coherence states for grid optimization

Mathematical Foundations:
  - Veyn operator: V(t) = Σᵢ φⁱ · e^(-λ(t-tᵢ)) · x(tᵢ)
  - κ-score: κ(t) = 1 - σ_veyn(t) / σ_raw(t)  ∈ [0, 1]
  - π-sampling: circular coherence across 2π angular domain
  - φ-layers: Fibonacci-scaled smoothing at 1, 1, 2, 3, 5, 8... hour windows
  - e-decay: exponential memory kernel with depth = 24 hours
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config import VeynOperatorConfig, MunicipalEnergyProfile


# ─────────────────────────────────────────────────────────────────────────────
# VEYN TEMPORAL COHERENCE OPERATOR
# ─────────────────────────────────────────────────────────────────────────────

class VeynOperator:
    """
    The Veyn operator smooths temporal signals using exponentially-weighted
    harmonic layers scaled by the golden ratio (φ).

    V(t) = Σᵢ₌₁ⁿ  φ⁻ⁱ · Σⱼ e^(-(t-tⱼ)/τᵢ) · x(tⱼ)

    where:
      φ = 1.618... (golden ratio)
      τᵢ = Fibonacci(i) hours (1, 1, 2, 3, 5, 8, 13, 21)
      λ = config.decay_factor
    """

    # Fibonacci sequence for harmonic layer windows
    FIBONACCI = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55]

    def __init__(self, config: Optional[VeynOperatorConfig] = None):
        self.config = config or VeynOperatorConfig()
        self.phi = self.config.phi_ratio
        self.e = self.config.euler_base

    def apply(self, signal: pd.Series) -> pd.Series:
        """
        Apply the Veyn operator to a time-series signal.

        Parameters:
            signal: pd.Series with DatetimeIndex, values are the raw signal

        Returns:
            pd.Series: Veyn-smoothed signal
        """
        values = signal.values.astype(float)
        n = len(values)
        smoothed = np.zeros(n)

        n_layers = min(self.config.phi_harmonic_layers, len(self.FIBONACCI))

        for layer_idx in range(n_layers):
            # φ-weighted contribution of this layer
            phi_weight = self.phi ** (-layer_idx)
            # Fibonacci-scaled window for this layer
            window_hours = self.FIBONACCI[layer_idx]

            # Exponential weighted moving average with this window
            layer_output = self._exponential_smooth(
                values, window_hours, self.config.decay_factor
            )
            smoothed += phi_weight * layer_output

        # Normalize by sum of phi weights
        total_weight = sum(self.phi ** (-i) for i in range(n_layers))
        smoothed /= total_weight

        return pd.Series(smoothed, index=signal.index, name="veyn_smoothed")

    def _exponential_smooth(
        self, values: np.ndarray, window: int, decay: float
    ) -> np.ndarray:
        """Exponential weighted smoothing with given window and decay."""
        n = len(values)
        result = np.zeros(n)

        for t in range(n):
            start = max(0, t - window)
            weights = np.array([
                decay ** (t - j) for j in range(start, t + 1)
            ])
            segment = values[start:t + 1]
            if weights.sum() > 0:
                result[t] = np.dot(weights, segment) / weights.sum()
            else:
                result[t] = values[t]

        return result

    def compute_volatility(
        self, signal: pd.Series, window: int = 24
    ) -> pd.Series:
        """
        Compute rolling volatility (standard deviation) of a signal.

        Parameters:
            signal: Input time series
            window: Rolling window in samples (hours if hourly data)

        Returns:
            Rolling standard deviation
        """
        return signal.rolling(window=window, min_periods=1).std()


# ─────────────────────────────────────────────────────────────────────────────
# κ-COHERENCE SCORE
# ─────────────────────────────────────────────────────────────────────────────

class KappaCoherenceScorer:
    """
    Computes the κ-coherence score from raw and Veyn-smoothed signals.

    κ(t) = 1 - σ_veyn(t) / σ_raw(t)

    Interpretation:
      κ → 1.0 : High coherence — Veyn smoothing captures the signal well,
                 minimal residual volatility → stable/predictable state
      κ → 0.0 : Low coherence — high residual noise after smoothing,
                 grid is in stressed/volatile state

    The π→φ→e reasoning loop:
      1. π-step: Sample signal at 2π intervals (circular coherence check)
      2. φ-step: Apply Fibonacci-scaled harmonic layers
      3. e-step: Weight by exponential decay kernel
    """

    def __init__(self, config: Optional[VeynOperatorConfig] = None):
        self.config = config or VeynOperatorConfig()
        self.veyn = VeynOperator(config)

    def compute_kappa(
        self,
        raw_signal: pd.Series,
        window: int = 24,
    ) -> pd.Series:
        """
        Compute κ-coherence score for a time series.

        Parameters:
            raw_signal: Raw (unsmoothed) input signal
            window: Rolling volatility window in hours

        Returns:
            κ-score time series ∈ [0, 1]
        """
        # Step 1: Apply Veyn smoothing
        smoothed = self.veyn.apply(raw_signal)

        # Step 2: Compute volatilities
        raw_vol = self.veyn.compute_volatility(raw_signal, window)
        residual = raw_signal - smoothed
        residual_vol = self.veyn.compute_volatility(residual, window)

        # Step 3: κ = 1 - (residual_vol / raw_vol)
        # Clamp to [0, 1], handle division by zero
        with np.errstate(divide="ignore", invalid="ignore"):
            kappa = 1.0 - (residual_vol / raw_vol)

        kappa = kappa.clip(0.0, 1.0)
        kappa = kappa.fillna(0.5)  # Default to mid-range for startup

        return kappa.rename("kappa_score")

    def classify_state(self, kappa: float) -> str:
        """Classify a κ-score into a coherence state."""
        if kappa >= self.config.coherence_threshold:
            return "COHERENT"        # Stable, predictable
        elif kappa <= self.config.stress_threshold:
            return "STRESSED"        # High volatility, grid risk
        else:
            return "TRANSITIONAL"    # Normal operating range

    def pi_circular_coherence(self, signal: pd.Series) -> float:
        """
        π-step: Compute circular coherence by sampling at π-intervals.

        Maps the signal to angular domain [0, 2π] and checks phase
        consistency. High circular coherence indicates periodic regularity
        (e.g., daily load cycles are well-preserved).
        """
        n = len(signal)
        if n < 2:
            return 0.5

        # Map to unit circle
        normalized = (signal - signal.min()) / (signal.max() - signal.min() + 1e-10)
        angles = normalized * 2 * np.pi

        # Circular mean resultant length (R)
        cos_sum = np.cos(angles).mean()
        sin_sum = np.sin(angles).mean()
        R = np.sqrt(cos_sum ** 2 + sin_sum ** 2)

        return float(R)  # R ∈ [0, 1], higher = more coherent


# ─────────────────────────────────────────────────────────────────────────────
# FORECASTING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ForecastingEngine:
    """
    Time-series forecasting engine that combines:
      1. Veyn-smoothed trend extraction
      2. κ-coherence scoring for prediction confidence
      3. Seasonal pattern decomposition
      4. Short-term (24h) and medium-term (7d) forecasts
    """

    def __init__(self, config: Optional[VeynOperatorConfig] = None):
        self.config = config or VeynOperatorConfig()
        self.veyn = VeynOperator(config)
        self.kappa_scorer = KappaCoherenceScorer(config)

    def forecast_load(
        self,
        historical_load: pd.Series,
        horizon_hours: int = 24,
    ) -> Dict[str, pd.Series]:
        """
        Generate a load forecast with coherence confidence.

        Parameters:
            historical_load: Historical hourly load data (kWh)
            horizon_hours: Forecast horizon in hours

        Returns:
            Dictionary with:
              - 'forecast': Predicted load values
              - 'kappa': κ-coherence confidence scores
              - 'upper_bound': 90th percentile upper bound
              - 'lower_bound': 10th percentile lower bound
        """
        # Extract Veyn-smoothed trend
        trend = self.veyn.apply(historical_load)

        # Compute seasonal pattern (24-hour cycle)
        seasonal = self._extract_daily_pattern(historical_load)

        # Compute residuals
        detrended = historical_load - trend
        residual_std = detrended.rolling(24, min_periods=1).std().iloc[-1]

        # κ-score for forecast confidence
        kappa = self.kappa_scorer.compute_kappa(historical_load)
        current_kappa = float(kappa.iloc[-1])

        # Generate forecast timestamps
        last_time = historical_load.index[-1]
        forecast_index = pd.date_range(
            start=last_time + pd.Timedelta(hours=1),
            periods=horizon_hours,
            freq="h",
        )

        # Project trend forward (last Veyn value + small drift)
        last_trend = trend.iloc[-1]
        trend_slope = (trend.iloc[-1] - trend.iloc[-25]) / 24 if len(trend) > 25 else 0
        projected_trend = last_trend + trend_slope * np.arange(1, horizon_hours + 1)

        # Add seasonal pattern
        forecast_values = np.array([
            projected_trend[i] + seasonal.get(forecast_index[i].hour, 0)
            for i in range(horizon_hours)
        ])

        # Confidence bounds (wider when κ is low = less coherent)
        confidence_width = residual_std * (2.0 - current_kappa)  # Widens as κ→0
        upper = forecast_values + 1.645 * confidence_width
        lower = np.maximum(0, forecast_values - 1.645 * confidence_width)

        # κ forecast (decays toward 0.5 over horizon)
        kappa_forecast = current_kappa + (0.5 - current_kappa) * (
            1 - np.exp(-np.arange(horizon_hours) / 12.0)
        )

        return {
            "forecast": pd.Series(forecast_values, index=forecast_index, name="load_forecast_kwh"),
            "kappa": pd.Series(kappa_forecast, index=forecast_index, name="kappa_forecast"),
            "upper_bound": pd.Series(upper, index=forecast_index, name="upper_90"),
            "lower_bound": pd.Series(lower, index=forecast_index, name="lower_10"),
            "current_kappa": current_kappa,
            "state": self.kappa_scorer.classify_state(current_kappa),
        }

    def _extract_daily_pattern(self, signal: pd.Series) -> Dict[int, float]:
        """Extract average hourly pattern (deviation from mean) from signal."""
        if not hasattr(signal.index, 'hour'):
            return {h: 0 for h in range(24)}

        hourly_mean = signal.groupby(signal.index.hour).mean()
        overall_mean = signal.mean()
        return {h: float(hourly_mean.get(h, overall_mean) - overall_mean)
                for h in range(24)}

    def forecast_solar(
        self,
        historical_solar: pd.Series,
        horizon_hours: int = 24,
    ) -> pd.Series:
        """
        Forecast solar generation using Veyn-smoothed historical patterns.
        """
        # Use daily pattern as primary predictor for solar
        pattern = self._extract_daily_pattern(historical_solar)
        mean_level = historical_solar.mean()

        last_time = historical_solar.index[-1]
        forecast_index = pd.date_range(
            start=last_time + pd.Timedelta(hours=1),
            periods=horizon_hours,
            freq="h",
        )

        values = np.array([
            max(0, mean_level + pattern.get(t.hour, 0))
            for t in forecast_index
        ])

        return pd.Series(values, index=forecast_index, name="solar_forecast_kwh")

    def compute_grid_coherence_report(
        self, load_series: pd.Series
    ) -> Dict[str, float]:
        """
        Generate a comprehensive coherence report for the grid signal.
        """
        kappa = self.kappa_scorer.compute_kappa(load_series)
        smoothed = self.veyn.apply(load_series)
        circular = self.kappa_scorer.pi_circular_coherence(load_series)

        return {
            "mean_kappa": float(kappa.mean()),
            "min_kappa": float(kappa.min()),
            "max_kappa": float(kappa.max()),
            "current_kappa": float(kappa.iloc[-1]),
            "state": self.kappa_scorer.classify_state(float(kappa.iloc[-1])),
            "circular_coherence": circular,
            "veyn_smoothing_ratio": float(smoothed.std() / (load_series.std() + 1e-10)),
            "signal_to_noise": float(
                smoothed.std() / ((load_series - smoothed).std() + 1e-10)
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# ENTSO-E INTEGRATION (European Market Reference)
# ─────────────────────────────────────────────────────────────────────────────

class ENTSOECoherenceAnalyzer:
    """
    Fetches ENTSO-E day-ahead prices and computes κ-coherence scores.
    This provides the European market reference for calibrating the
    Veyn operator against real wholesale electricity price dynamics.

    Requires: pip install entsoe-py
    API key: Register at https://transparency.entsoe.eu/ and email
             transparency@entsoe.eu with subject "Restful API access"
    """

    def __init__(self, api_key: str, config: Optional[VeynOperatorConfig] = None):
        self.api_key = api_key
        self.config = config or VeynOperatorConfig()
        self.engine = ForecastingEngine(config)
        self._client = None

    def _get_client(self):
        """Lazy-load entsoe-py client."""
        if self._client is None:
            try:
                from entsoe import EntsoePandasClient
                self._client = EntsoePandasClient(api_key=self.api_key)
            except ImportError:
                raise ImportError(
                    "entsoe-py is required: pip install entsoe-py\n"
                    "Register for API key at https://transparency.entsoe.eu/"
                )
        return self._client

    def fetch_day_ahead_prices(
        self,
        country_code: str = "DE_LU",
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.Series:
        """
        Fetch day-ahead electricity prices from ENTSO-E.

        Parameters:
            country_code: ENTSO-E area code (default: Germany-Luxembourg)
            start: Start timestamp (default: 7 days ago)
            end: End timestamp (default: now)

        Returns:
            pd.Series of day-ahead prices (EUR/MWh)
        """
        client = self._get_client()

        if start is None:
            start = pd.Timestamp.now(tz="Europe/Berlin") - pd.Timedelta(days=7)
        if end is None:
            end = pd.Timestamp.now(tz="Europe/Berlin")

        prices = client.query_day_ahead_prices(country_code, start=start, end=end)
        return prices

    def compute_market_kappa(
        self,
        country_code: str = "DE_LU",
        days: int = 7,
    ) -> Dict:
        """
        Fetch market data and compute κ-coherence for calibration.
        """
        end = pd.Timestamp.now(tz="Europe/Berlin")
        start = end - pd.Timedelta(days=days)

        prices = self.fetch_day_ahead_prices(country_code, start, end)
        report = self.engine.compute_grid_coherence_report(prices)
        report["country_code"] = country_code
        report["period_days"] = days
        report["price_mean_eur_mwh"] = float(prices.mean())
        report["price_std_eur_mwh"] = float(prices.std())

        return report


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / TESTING
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from municipal_energy_model import MunicipalEnergyModel, generate_hourly_load_profile

    print("=" * 70)
    print("  Forecasting Engine — St. Thomas Municipal Energy")
    print("=" * 70)

    # Generate synthetic load for the largest consumer
    profile = MunicipalEnergyProfile()
    load = generate_hourly_load_profile(
        annual_kwh=profile.electricity_by_category["Community Support Services"],
        category="Community Support Services",
        year=2024,
    )

    # Run forecasting engine
    engine = ForecastingEngine()
    forecast_result = engine.forecast_load(load, horizon_hours=48)

    print(f"\n  Historical load points: {len(load)}")
    print(f"  Current κ-score:       {forecast_result['current_kappa']:.4f}")
    print(f"  Grid state:            {forecast_result['state']}")
    print(f"  48h forecast range:    {forecast_result['lower_bound'].min():.1f} - "
          f"{forecast_result['upper_bound'].max():.1f} kWh")

    # Coherence report
    report = engine.compute_grid_coherence_report(load)
    print(f"\n  Coherence Report:")
    for k, v in report.items():
        if isinstance(v, float):
            print(f"    {k}: {v:.4f}")
        else:
            print(f"    {k}: {v}")
