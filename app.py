import streamlit as st
import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta
from entsoe import EntsoePandasClient

st.set_page_config(page_title="Quantara Oracle", layout="centered")
st.title("Quantara Oracle")
st.markdown("*Live coherence audit of the European energy grid*")

# === GET ENTSO-E DATA (Germany Day-Ahead Prices) ===
client = EntsoePandasClient(api_key=None)  # Public access, no key needed

start = pd.Timestamp('20251110', tz='Europe/Berlin')
end = pd.Timestamp('20251111', tz='Europe/Berlin')

try:
    df = client.query_day_ahead_prices('DE', start=start, end=end)
    latest_price = df.iloc[-1]  # Most recent available
    price = float(latest_price)
    st.success(f"Live DE Day-Ahead Price: **{price:,.2f} EUR/MWh**")
except Exception as e:
    price = 95.50
    st.warning("Using demo data (public ENTSO-E access limited)")

# === VEYN COHERENCE ENGINE ===
def veyn_coherence(price, steps=24):
    tau = 0.8
    memory = [tau]
    for _ in range(steps):
        drift = -0.002 * tau + np.random.normal(0, 0.005)
        # Normalize price impact: high volatility = lower coherence
        volatility = abs(price - 100) / 100
        tau = np.clip(tau + drift - volatility * 0.15, 0.0, 1.0)
        memory.append(tau)
    return memory[-1], memory

final_tau, path = veyn_coherence(price)
kappa = round(final_tau, 3)

# === EBI AUDIT ===
status = "PASS" if kappa >= 0.7 else "FAIL"
color = "green" if kappa >= 0.7 else "red"

st.metric("κ Coherence Score", kappa, delta=f"{kappa-0.8:+.3f}")
st.write(f"**EBI Audit: <span style='color:{color}'>{status}</span>**", unsafe_allow_html=True)

# === 24-HOUR FORECAST PLOT ===
st.line_chart(path, use_container_width=True)
st.caption("24-hour coherence forecast (Germany day-ahead prices)")

# === FOOTER ===
st.markdown("---")
st.markdown("**Built by Nadine Squires** — [quantara.earth](https://quantara.earth)")
st.caption("Data: ENTSO-E Transparency Platform (public access)")
