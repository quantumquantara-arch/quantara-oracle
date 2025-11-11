import streamlit as st
import requests
import pandas as pd
import numpy as np
import os

st.set_page_config(page_title="Quantara Oracle", layout="centered")
st.title("Quantara Oracle")
st.markdown("*Live coherence audit of the U.S. energy grid*")

# === GET EIA DATA ===
API_KEY = os.environ.get("EIA_KEY", "demo")
url = f"https://api.eia.gov/v2/electricity/rto/region-data/data/?api_key={API_KEY}&frequency=hourly&data[0]=value&facets[region-id][]=US48&start=2025-11-10T00&sort[0][column]=period"

try:
    data = requests.get(url).json()['response']['data']
    df = pd.DataFrame(data)
    df['period'] = pd.to_datetime(df['period'])
    latest = df.iloc[0]
    demand = float(latest['value'])
    st.success(f"Live U.S. Demand: **{demand:,.0f} MWh**")
except:
    demand = 400000
    st.warning("Using demo data")

# === VEYN COHERENCE ===
def veyn_coherence(demand, steps=24):
    tau = 0.8
    memory = [tau]
    for _ in range(steps):
        drift = -0.002 * tau + np.random.normal(0, 0.005)
        tau = np.clip(tau + drift + (1 - demand/500000)*0.1, 0.0, 1.0)
        memory.append(tau)
    return memory[-1], memory

final_tau, path = veyn_coherence(demand)
kappa = round(final_tau, 3)

# === EBI AUDIT ===
status = "PASS" if kappa >= 0.7 else "FAIL"
color = "green" if kappa >= 0.7 else "red"

st.metric("κ Coherence Score", kappa, delta=f"{kappa-0.8:+.3f}")
st.write(f"**EBI Audit: <span style='color:{color}'>{status}</span>**", unsafe_allow_html=True)

# === PLOT ===
st.line_chart(path, use_container_width=True)
st.caption("24-hour coherence forecast (conceptual)")

# === FOOTER ===
st.markdown("---")
st.markdown("**Built by Nadine Squires** — [quantara.earth](https://quantara.earth)")
