"""umbraNocti — Cockpit Streamlit (v2).

Consume el API local. Asume que la API ya está arrancada con
`python scripts/run_api.py`.

Cambios vs v1:
- Equity curve REAL (mark-to-market) en vez de cost-basis.
- KPIs nuevos: drawdown, realized PnL, gross exposure, peak equity, cash.
- Tabla de EXITS (qué cerró el bot y con cuánto realized PnL — esto es lo
  más importante para entender si el bot está vendiendo correctamente).
- Gráfico CANDLESTICK por mercado con EMAs, canal de Donchian, soportes y
  resistencias. Permite seleccionar mercado e intervalo.
"""

from __future__ import annotations

import os

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

API_URL = os.environ.get("UMBRA_API_URL", "http://127.0.0.1:8000")


def _get(path: str, params: dict | None = None):
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{API_URL}{path}", params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        st.error(f"Error al consultar {path}: {exc}")
        return None


def _post(path: str):
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f"{API_URL}{path}")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        st.error(f"Error en {path}: {exc}")
        return None


st.set_page_config(
    page_title="umbraNocti — Cockpit",
    page_icon=":crescent_moon:",
    layout="wide",
)

st.title("umbraNocti — Cockpit v2")
st.caption(f"API: `{API_URL}`")

# ---------------------------------------------------------------------------
# Controles
# ---------------------------------------------------------------------------

c_refresh, c_halt, c_resume, c_flatten = st.columns([1, 1, 1, 1])
if c_refresh.button("Refresh", use_container_width=True):
    st.rerun()
if c_halt.button("Halt + Flatten", use_container_width=True, type="primary"):
    res = _post("/admin/halt")
    if res:
        st.warning(f"Halted. Flattened: {res.get('flattened')}")
if c_resume.button("Resume", use_container_width=True):
    res = _post("/admin/resume")
    if res:
        st.success("Resumed.")
if c_flatten.button("Flatten only", use_container_width=True):
    res = _post("/admin/flatten")
    if res:
        st.info(f"Flattened: {res.get('n')}")

# ---------------------------------------------------------------------------
# Fetch data
# ---------------------------------------------------------------------------

stats = _get("/stats") or {}
portfolio = _get("/portfolio") or {}
health = _get("/portfolio/health") or {}
universe = _get("/universe") or []
signals = _get("/signals", {"limit": 50}) or []
fills = _get("/fills", {"limit": 50}) or []
exits = _get("/exits", {"limit": 50}) or []

# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Estado del portfolio")
k1, k2, k3, k4 = st.columns(4)
k1.metric("Equity (USD)", f"${portfolio.get('equity_usd', 0):.2f}")
k2.metric("Drawdown", f"{portfolio.get('drawdown_pct', 0) * 100:.2f}%")
k3.metric(
    "Realized PnL",
    f"${portfolio.get('realized_pnl_usd_total', 0):.2f}",
)
k4.metric(
    "Unrealized PnL",
    f"${portfolio.get('unrealized_pnl_usd', 0):.2f}",
)

k5, k6, k7, k8 = st.columns(4)
k5.metric("Cash", f"${portfolio.get('cash_usd', 0):.2f}")
k6.metric("Gross exposure", f"${portfolio.get('gross_exposure_usd', 0):.2f}")
k7.metric("Peak equity", f"${portfolio.get('peak_equity_usd', 0):.2f}")
k8.metric("Open positions", portfolio.get("n_open_positions", 0))

cb = health.get("circuit_breakers", {})
if health.get("halted_by_redis_failure"):
    st.error(
        "🔴 Redis no disponible; el HALT puede ser un falso positivo. Verifica Redis y vuelve a cargar."
    )
elif health.get("halted") or cb.get("dd_halt_active"):
    st.error("⛔ HALT activo o DD ≥ umbral de halt. El bot no toma nuevas señales y ha intentado flatten.")
elif cb.get("dd_throttle_active"):
    st.warning("⚠️ DD throttle activo — sizing reducido a la mitad.")

# ---------------------------------------------------------------------------
# Equity curve real
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Equity curve (mark-to-market real)")
ec = _get("/portfolio/equity-curve", {"hours": 24}) or []
if ec:
    df = pd.DataFrame(ec)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts")
    eq_chart = pd.DataFrame(
        {
            "Equity": df["equity_usd"],
            "Peak": df["peak_equity_usd"],
        }
    )
    st.line_chart(eq_chart)
    st.caption("Drawdown (%):")
    st.area_chart(df["drawdown_pct"] * 100)
    pnl_chart = pd.DataFrame(
        {
            "Realized acumulado": df["realized_pnl_usd_total"],
            "Unrealized": df["unrealized_pnl_usd"],
        }
    )
    st.line_chart(pnl_chart)
else:
    st.info("Aún no hay snapshots de equity (el loop tarda ~60s en producir el primero).")

# ---------------------------------------------------------------------------
# CANDLESTICKS por mercado
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Análisis técnico por mercado")

if not universe:
    st.info("Universo vacío.")
else:
    markets_options = {
        f"#{m['rank']} — {m.get('question', m['condition_id'])[:80]}": m["condition_id"]
        for m in universe
    }
    col_m, col_i = st.columns([3, 1])
    selected_label = col_m.selectbox("Mercado", list(markets_options.keys()))
    interval = col_i.selectbox("Intervalo", ["1m", "5m", "15m", "1h"], index=1)
    cid = markets_options[selected_label]

    candles = _get(f"/markets/{cid}/candles", {"interval": interval, "n": 120})
    if candles and candles.get("bars"):
        bars = candles["bars"]
        df_b = pd.DataFrame(bars)
        df_b["ts"] = pd.to_datetime(df_b["ts"])

        fig = go.Figure()
        fig.add_trace(
            go.Candlestick(
                x=df_b["ts"],
                open=df_b["open"],
                high=df_b["high"],
                low=df_b["low"],
                close=df_b["close"],
                name="Precio (mid YES)",
                increasing_line_color="#26a69a",
                decreasing_line_color="#ef5350",
            )
        )

        trend = candles.get("trend") or {}
        # EMAs como líneas planas (último valor) — no calculamos histórico aquí
        if trend.get("ema_fast") is not None:
            fig.add_hline(
                y=trend["ema_fast"],
                line_dash="dot",
                line_color="#42a5f5",
                annotation_text=f"EMA{int(20)}",
                annotation_position="right",
            )
        if trend.get("ema_slow") is not None:
            fig.add_hline(
                y=trend["ema_slow"],
                line_dash="dot",
                line_color="#ab47bc",
                annotation_text=f"EMA{int(50)}",
                annotation_position="right",
            )
        # Canal de Donchian
        if trend.get("channel_high") is not None:
            fig.add_hline(
                y=trend["channel_high"],
                line_dash="dash",
                line_color="#ffa726",
                annotation_text="Canal alto",
                annotation_position="right",
            )
            fig.add_hline(
                y=trend["channel_low"],
                line_dash="dash",
                line_color="#ffa726",
                annotation_text="Canal bajo",
                annotation_position="right",
            )

        # Soportes y resistencias
        lv = candles.get("levels") or {}
        for s in (lv.get("supports") or [])[:3]:
            fig.add_hline(
                y=s["price"],
                line_color="#66bb6a",
                line_width=1,
                annotation_text=f"S ({s['touches']}t)",
                annotation_position="left",
            )
        for r in (lv.get("resistances") or [])[:3]:
            fig.add_hline(
                y=r["price"],
                line_color="#e53935",
                line_width=1,
                annotation_text=f"R ({r['touches']}t)",
                annotation_position="left",
            )

        fig.update_layout(
            height=500,
            margin=dict(l=10, r=10, t=30, b=10),
            xaxis_rangeslider_visible=False,
            yaxis_title="Probabilidad YES",
            xaxis_title="Tiempo (UTC)",
            template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)

        # Resumen TA
        col_ta1, col_ta2, col_ta3, col_ta4 = st.columns(4)
        col_ta1.metric("Régimen", trend.get("regime", "—"))
        col_ta2.metric(
            "Pos. en canal",
            f"{(trend.get('position_in_channel') or 0) * 100:.0f}%",
        )
        col_ta3.metric(
            "Ancho canal",
            f"{(trend.get('channel_width_pct') or 0) * 100:.1f}%",
        )
        col_ta4.metric(
            "Pendiente",
            f"{(trend.get('slope') or 0) * 1000:.2f}‰/bar",
        )
    else:
        st.info(
            "Aún no hay velas OHLC para este mercado. El ohlc_loop tarda ~1 min en "
            "agregar y necesita >= 5 minutos de snapshots para tener bars 5m completos."
        )

# ---------------------------------------------------------------------------
# Posiciones abiertas
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Posiciones abiertas")
if portfolio.get("positions"):
    df_pos = pd.DataFrame(portfolio["positions"])
    cols = [
        "market_id",
        "side",
        "shares",
        "avg_entry_price",
        "current_price",
        "unrealized_pnl_usd",
        "unrealized_pnl_pct",
        "realized_pnl_usd",
        "total_cost_usd",
        "n_fills",
        "age_hours",
    ]
    df_pos = df_pos[[c for c in cols if c in df_pos.columns]]
    st.dataframe(df_pos, use_container_width=True, hide_index=True)
else:
    st.info("Sin posiciones abiertas.")

# ---------------------------------------------------------------------------
# Exits — la tabla más importante para saber si el bot está vendiendo
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Últimos exits (cierres) — qué cerró y con qué realized PnL")
if exits:
    df_x = pd.DataFrame(exits)
    cols = [
        "ts",
        "market_id",
        "side",
        "shares",
        "fill_price",
        "mid_at_fill",
        "slippage_bps",
        "proceeds_usd",
        "realized_pnl_usd",
    ]
    df_x = df_x[[c for c in cols if c in df_x.columns]]
    st.dataframe(df_x, use_container_width=True, hide_index=True)
else:
    st.info("Aún no hay exits ejecutados.")

# ---------------------------------------------------------------------------
# Señales
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Últimas señales (50)")
if signals:
    df_sig = pd.DataFrame(signals)
    df_sig = df_sig[
        [
            "ts",
            "market_id",
            "edge",
            "side",
            "market_price",
            "fair_price",
            "edge_value",
            "strength",
            "notional_usd",
            "accepted",
            "reason",
        ]
    ]
    st.dataframe(df_sig, use_container_width=True, hide_index=True)
else:
    st.info("Aún no hay señales generadas.")

# ---------------------------------------------------------------------------
# Fills (apertura)
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Últimos fills de apertura (OPEN)")
if fills:
    df_fills = pd.DataFrame(fills)
    df_fills = df_fills[
        [
            "ts",
            "market_id",
            "side",
            "shares",
            "fill_price",
            "mid_at_fill",
            "slippage_bps",
            "notional_usd",
        ]
    ]
    st.dataframe(df_fills, use_container_width=True, hide_index=True)
else:
    st.info("Aún no hay fills.")

# ---------------------------------------------------------------------------
# Universo
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Universo activo")
if universe:
    df_u = pd.DataFrame(universe)
    st.dataframe(df_u, use_container_width=True, hide_index=True)
else:
    st.info("Universo vacío.")
