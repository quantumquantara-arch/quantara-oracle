# Quantara Oracle — St. Thomas Municipal Energy Optimization

κ-coherence energy optimization system for the City of St. Thomas, Ontario, powered by Veyn temporal coherence operators.

## Architecture

```
st_thomas_energy_optimization.py   → Main orchestration engine
├── config.py                       → All verified data & configuration
├── municipal_energy_model.py       → Energy infrastructure model
├── forecasting_engine.py           → Veyn operators & κ-scoring
├── optimization_policy.py          → Dispatch decision engine
├── battery_dispatch.py             → Battery charge/discharge LP
└── app.py                          → Streamlit dashboard
```

## Data Sources (Verified)

| Data | Source | URL |
|------|--------|-----|
| Municipal electricity consumption (2024) | City of St. Thomas CDM Plan 2025-2029 | [PDF](https://cdnsm5-hosted.civiclive.com/UserFiles/Servers/Server_12189721/Image/2025-2029%20Energy%20Conservation%20and%20Demand%20Management%20Plan.pdf) |
| Natural gas consumption (2024) | City of St. Thomas CDM Plan 2025-2029 | Same as above |
| 51 municipal properties listing | CDM Plan Appendix A | Same as above |
| 219 fleet vehicles breakdown | CDM Plan Section 4.2 | Same as above |
| Ontario TOU electricity rates | Ontario Energy Board (Nov 1, 2024) | [oeb.ca](https://www.oeb.ca/consumer-information-and-protection/electricity-rates) |
| St. Thomas rate distributor | Entegrus | [entegrus.com](https://www.entegrus.com/rates) |
| Ontario grid carbon intensity (2022) | CER Provincial Energy Profile | [cer-rec.gc.ca](https://www.cer-rec.gc.ca/en/data-analysis/energy-markets/provincial-territorial-energy-profiles/provincial-territorial-energy-profiles-ontario.html) |
| Ontario grid carbon intensity (2024) | GTHA Carbon Emissions Inventory | [carbon.taf.ca](https://carbon.taf.ca/2024/electricity-grid) |
| European market prices | ENTSO-E Transparency Platform | [transparency.entsoe.eu](https://transparency.entsoe.eu/) |
| Population | Statistics Canada Census 2021 | [statcan.gc.ca](https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/page.cfm?Lang=E&SearchText=St.+Thomas&DGUIDlist=2021A00053534021) |

### Key Verified Figures

- **Total municipal electricity (2024):** 15,097,719 kWh across 51 properties
- **Total natural gas (2024):** 1,240,964 m³
- **Building area:** 810,589 sq ft
- **Fleet:** 219 vehicles (99 diesel, 105 gasoline, 4 propane, 2 hybrid, 9 electric)
- **Streetlights:** 5,248 + 43 traffic signals
- **Ontario TOU rates:** Off-peak $0.076, Mid-peak $0.122, On-peak $0.158/kWh
- **Grid carbon intensity:** 35 g CO₂e/kWh (2022 avg), 73.8 g CO₂e/kWh (2024)

## Quick Start

```bash
pip install -r requirements.txt
# Run analysis
python st_thomas_energy_optimization.py
# Run dashboard
streamlit run app.py --server.port 8501
```

## ENTSO-E Integration (Optional)

The system can connect to the ENTSO-E Transparency Platform for European day-ahead electricity price data, used to calibrate the Veyn coherence operator against real wholesale market dynamics.

```bash
# Set API key
export ENTSOE_API_KEY="your-api-key"
# Run with ENTSO-E calibration
python st_thomas_energy_optimization.py --entsoe-key $ENTSOE_API_KEY
```

To get an API key: register at transparency.entsoe.eu, then email transparency@entsoe.eu with subject "Restful API access".

## How It Works

### Veyn Operator

The Veyn temporal coherence operator smooths time-series signals using exponentially-weighted harmonic layers scaled by the golden ratio (φ):

```
V(t) = Σᵢ φ⁻ⁱ · Σⱼ e^(-(t-tⱼ)/τᵢ) · x(tⱼ)
```

where τᵢ follows the Fibonacci sequence (1, 1, 2, 3, 5, 8, 13, 21 hours).

### κ-Coherence Score

The κ-score measures how well the Veyn-smoothed signal captures the underlying load pattern:

```
κ(t) = 1 - σ_residual(t) / σ_raw(t)
```

- κ ≥ 0.7: **COHERENT** — grid is stable, predictable
- 0.3 < κ < 0.7: **TRANSITIONAL** — active optimization recommended
- κ ≤ 0.3: **STRESSED** — demand response needed

### π→φ→e Reasoning Loop

1. **π-step:** Circular coherence check — periodicity validation
2. **φ-step:** Golden-ratio harmonic smoothing — multi-scale pattern extraction
3. **e-step:** Exponential decay kernel — temporal memory weighting
