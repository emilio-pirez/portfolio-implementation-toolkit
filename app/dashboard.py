"""Portfolio Implementation Toolkit - Streamlit dashboard.

Run with:  streamlit run app/dashboard.py

On a fresh clone (empty database) the first launch builds an offline demo:
synthetic market data, about six weeks of `run-day` with the example flows,
and the with/without-equitization backtest. Takes about a minute, once.
"""

from __future__ import annotations

import threading
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import Engine

from pit import __version__
from pit.config import AppConfig, load_config
from pit.dashboard import queries as q
from pit.db import init_db, make_engine
from pit.demo import build_demo
from pit.orders.basket import EXPORT_COLUMNS

# Categorical slots 1-3 of the validated reference palette (fixed order).
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
# Status colors, always shown together with an icon and a label.
HARD_RED, SOFT_AMBER = "#e34948", "#eda100"
SCENARIO_COLORS = {"With equitization": BLUE, "Without equitization": ORANGE}
SCENARIO_NAMES = {
    "with_equitization": "With equitization",
    "without_equitization": "Without equitization",
}
PAGES = [
    "Overview",
    "Positions vs. Model",
    "Order Blotter",
    "Compliance",
    "Cash & Equitization",
    "Audit Log",
]

st.set_page_config(page_title="Portfolio Implementation Toolkit", layout="wide")


# --------------------------------------------------------------------------- setup


@st.cache_resource
def get_engine(url: str) -> Engine:
    engine = make_engine(url)
    init_db(engine)
    return engine


@st.cache_resource
def demo_lock() -> threading.Lock:
    """One lock per server process, shared by all browser sessions."""
    return threading.Lock()


def bootstrap(engine: Engine, cfg: AppConfig) -> None:
    """Build the offline demo the first time the dashboard opens an empty database.

    The lock makes a second browser tab wait instead of building the demo twice.
    """
    if q.data_sources(engine) and q.has_positions(engine):
        return
    with st.status("First launch: building an offline demo (about 1 minute)...", expanded=True):
        bar = st.progress(0.0, text="Waiting for the demo build...")
        with demo_lock():
            if not (q.data_sources(engine) and q.has_positions(engine)):
                build_demo(engine, cfg, progress=lambda msg, frac: bar.progress(frac, text=msg))
    st.rerun()


def pct(x: float) -> str:
    return f"{x:.2%}"


def usd(x: float) -> str:
    return f"USD {x:,.0f}"


def usd_m(x: float) -> str:
    return f"${x / 1e6:,.1f}m"


def add_limit(fig: go.Figure, y: float, label: str, below: bool = False) -> go.Figure:
    """Dashed reference line for a compliance limit, labelled in muted text.

    Minimum limits put their label under the line so it never sits on the data.
    """
    fig.add_hline(
        y=y,
        line_dash="dash",
        line_width=1,
        line_color="#8a8984",
        annotation_text=label,
        annotation_position="bottom left" if below else "top left",
        annotation_font_color="#8a8984",
    )
    return fig


def line_chart(
    df: pd.DataFrame, y: list[str], title: str, colors: list[str], fmt: str
) -> go.Figure:
    fig = px.line(df, y=y, title=title, color_discrete_sequence=colors)
    fig.update_traces(line={"width": 2})
    fig.update_layout(
        hovermode="x unified",
        legend={"orientation": "h", "y": -0.2, "title": None},
        yaxis={"tickformat": fmt, "title": None},
        xaxis={"title": None},
        margin={"t": 50, "b": 10},
    )
    return fig


PCT = st.column_config.NumberColumn(format="percent")
MONEY = st.column_config.NumberColumn(format="dollar")


# --------------------------------------------------------------------------- pages


def page_overview(engine: Engine, cfg: AppConfig, as_of: date) -> None:
    hist = q.portfolio_history(engine, cfg)
    day = hist.loc[pd.Timestamp(as_of)]
    prev = (
        hist.loc[: pd.Timestamp(as_of)].iloc[-2]
        if len(hist.loc[: pd.Timestamp(as_of)]) > 1
        else day
    )
    fut = q.futures_positions(engine, cfg, as_of)
    alerts = q.active_alerts(engine)

    es = int(fut.loc[fut["contract"].str.startswith("ES"), "contracts"].sum())
    mes = int(fut.loc[fut["contract"].str.startswith("MES"), "contracts"].sum())
    c = st.columns(3) + st.columns(3)
    nav_delta = f"{day['nav'] / prev['nav'] - 1:+.2%} vs prior day"
    c[0].metric("NAV (USD)", usd_m(day["nav"]), nav_delta)
    c[1].metric("Total exposure", pct(day["exposure"]), help="(stocks + futures notional) / NAV")
    c[2].metric("Active alerts", len(alerts))
    c[3].metric("Cash", pct(day["cash_pct"]), help="Physical cash / NAV")
    c[4].metric(
        "Net cash",
        pct(day["net_cash_pct"]),
        help="Cash - futures notional: the cash that is truly out of the market",
    )
    c[5].metric(
        "Futures notional (USD)",
        usd_m(day["futures_notional"]),
        f"{es} ES · {mes} MES",
        delta_color="off",
        delta_arrow="off",
    )

    left, right = st.columns(2)
    left.plotly_chart(
        line_chart(hist, ["nav"], "NAV", [BLUE], ",.0f").update_layout(showlegend=False),
        width="stretch",
    )
    lim = cfg.compliance.total_exposure_range
    exp_fig = line_chart(hist, ["exposure"], "Total exposure (% of NAV)", [BLUE], ".1%")
    exp_fig.update_layout(showlegend=False, yaxis={"range": [lim.min - 0.01, lim.max + 0.01]})
    add_limit(exp_fig, lim.min, f"min {lim.min:.0%}", below=True)
    add_limit(exp_fig, lim.max, f"max {lim.max:.0%}")
    right.plotly_chart(exp_fig, width="stretch")
    cash = (hist[["cash", "futures_notional"]].div(hist["nav"], axis=0)).rename(
        columns={"cash": "Physical cash", "futures_notional": "Futures notional"}
    )
    cash["Net cash"] = hist["net_cash_pct"]
    st.plotly_chart(
        line_chart(
            cash, list(cash.columns), "Cash and futures (% of NAV)", [BLUE, ORANGE, AQUA], ".1%"
        ),
        width="stretch",
    )

    st.subheader("Active alerts")
    if alerts.empty:
        st.success("No active alerts: all post-trade checks passed and no basket is waiting.")
    else:
        st.dataframe(alerts, hide_index=True, width="stretch")
    if not fut.empty:
        st.subheader("Futures positions")
        st.dataframe(fut, hide_index=True, column_config={"notional": MONEY})


def page_positions(engine: Engine, cfg: AppConfig, as_of: date) -> None:
    h = q.holdings(engine, cfg, as_of)
    sectors = sorted(h["sector"].unique())
    chosen = st.multiselect("Sectors", sectors, default=sectors)
    view = h[h["sector"].isin(chosen)]

    sec = q.sector_weights(engine, cfg, as_of)
    long = sec.melt(
        id_vars="sector",
        value_vars=["portfolio", "target", "benchmark"],
        var_name="series",
        value_name="weight",
    )
    fig = px.bar(
        long,
        x="sector",
        y="weight",
        color="series",
        barmode="group",
        title="Sector weights: portfolio vs. target vs. benchmark",
        color_discrete_map={"portfolio": BLUE, "target": ORANGE, "benchmark": AQUA},
    )
    fig.update_layout(
        yaxis={"tickformat": ".0%", "title": None},
        xaxis={"title": None},
        legend={"orientation": "h", "y": -0.35, "title": None},
        bargap=0.25,
    )
    st.plotly_chart(fig, width="stretch")

    top = view.reindex(view["vs_target"].abs().sort_values(ascending=False).index).head(15)
    top = top.assign(
        direction=top["vs_target"].map(lambda v: "overweight" if v >= 0 else "underweight")
    )
    fig2 = px.bar(
        top,
        x="ticker",
        y="vs_target",
        color="direction",
        title="Largest deviations from target (% of NAV)",
        color_discrete_map={"overweight": BLUE, "underweight": ORANGE},
    )
    fig2.update_layout(
        yaxis={"tickformat": ".2%", "title": None},
        xaxis={"title": None},
        legend={"orientation": "h", "y": -0.25, "title": None},
    )
    st.plotly_chart(fig2, width="stretch")

    st.dataframe(
        view,
        hide_index=True,
        width="stretch",
        column_config={
            "weight": PCT,
            "target": PCT,
            "benchmark": PCT,
            "vs_target": PCT,
            "vs_benchmark": PCT,
            "market_value": MONEY,
            "price": MONEY,
        },
    )


def page_blotter(engine: Engine) -> None:
    b = q.baskets(engine)
    if b.empty:
        st.info("No baskets yet. Run `pit run-day`.")
        return
    labels = {r.batch_id: f"{r.date} · {r.status} · {r.batch_id[:8]}" for r in b.itertuples()}
    batch = st.selectbox("Basket", list(labels), format_func=labels.get)
    row = b[b["batch_id"] == batch].iloc[0]
    orders = q.basket_orders(engine, batch)

    c = st.columns(5)
    c[0].metric("Status", row["status"])
    c[1].metric("Orders", int(row["orders"]))
    c[2].metric("Buys (USD)", usd_m(row["buy_notional"]))
    c[3].metric("Sells (USD)", usd_m(row["sell_notional"]))
    c[4].metric("Est. cost (USD)", f"${row['est_cost']:,.0f}")
    st.dataframe(
        orders.drop(columns=["id", "batch_id"]),
        hide_index=True,
        width="stretch",
        column_config={"est_price": MONEY, "est_notional": MONEY, "est_cost": MONEY},
    )
    desk = orders[EXPORT_COLUMNS].assign(quantity=orders["quantity"].astype(int))
    st.download_button(
        "Export desk CSV",
        desk.to_csv(index=False),
        file_name=f"basket_{batch[:8]}.csv",
        mime="text/csv",
    )
    st.subheader("All baskets")
    st.dataframe(
        b,
        hide_index=True,
        width="stretch",
        column_config={"buy_notional": MONEY, "sell_notional": MONEY, "est_cost": MONEY},
    )


def _status_icon(passed: bool, severity: str) -> str:
    if passed:
        return "✅ pass"
    return "⛔ HARD breach" if severity == "HARD" else "⚠️ SOFT alert"


def _checks_table(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No checks stored for this selection.")
        return
    view = df.assign(
        result=[_status_icon(bool(p), s) for p, s in zip(df["passed"], df["severity"], strict=True)]
    )
    st.dataframe(
        view[["result", "check_name", "value", "limit_value", "message"]],
        hide_index=True,
        width="stretch",
        column_config={
            "message": st.column_config.TextColumn(width="large"),
            "value": st.column_config.NumberColumn(format="%.4f"),
        },
    )


def page_compliance(engine: Engine, as_of: date) -> None:
    b = q.baskets(engine)
    left, right = st.columns(2)
    with left:
        st.subheader("Pre-trade checks")
        if b.empty:
            st.info("No baskets yet.")
        else:
            labels = {
                r.batch_id: f"{r.date} · {r.status} · {r.batch_id[:8]}" for r in b.itertuples()
            }
            batch = st.selectbox("Basket", list(labels), format_func=labels.get, key="cmp_batch")
            _checks_table(q.basket_checks(engine, batch))
    with right:
        st.subheader(f"Post-trade checks · {as_of}")
        _checks_table(q.basket_checks(engine, f"POST-{as_of.isoformat()}"))

    st.subheader("Breach history")
    counts = q.breach_counts(engine)
    if counts.empty:
        st.success("No breaches recorded.")
        return
    counts["label"] = counts["kind"] + " · " + counts["severity"]
    fig = px.bar(
        counts,
        x="date",
        y="breaches",
        color="severity",
        color_discrete_map={"HARD": HARD_RED, "SOFT": SOFT_AMBER},
        hover_data=["kind"],
        title="Failed checks per day",
    )
    fig.update_layout(
        legend={"orientation": "h", "y": -0.25, "title": None},
        xaxis={"title": None},
        yaxis={"title": None, "dtick": 1},
    )
    st.plotly_chart(fig, width="stretch")


def page_cash(engine: Engine, cfg: AppConfig) -> None:
    runs = q.backtest_runs(engine)
    if runs.empty:
        st.info("No backtest yet. Run `pit backtest`.")
        return
    labels = {
        r.run_id: f"{r.start} → {r.end} · {r.flows} flows · {r.run_id[:8]}"
        for r in runs.itertuples()
    }
    run = st.selectbox("Backtest run", list(labels), format_func=labels.get)
    summary = runs.loc[runs["run_id"] == run, "summary"].iloc[0]
    daily = q.backtest_daily(engine, run)
    st.caption(
        "Same market data, flows and basket logic in both scenarios; the only difference is "
        "whether idle cash is equitized with index futures."
    )

    daily = daily.assign(scenario=daily["scenario"].map(SCENARIO_NAMES))

    def scenario_chart(value: str, title: str, fmt: str) -> go.Figure:
        wide = daily.pivot(index="date", columns="scenario", values=value)
        return line_chart(
            wide, list(wide.columns), title, [SCENARIO_COLORS[c] for c in wide.columns], fmt
        )

    idle = scenario_chart("unequitized_pct", "Idle cash, net of futures (% of NAV)", ".1%")
    add_limit(
        idle, cfg.compliance.cash_range.max, f"cash limit {cfg.compliance.cash_range.max:.0%}"
    )
    st.plotly_chart(idle, width="stretch")
    left, right = st.columns(2)
    left.plotly_chart(
        scenario_chart("cum_cash_drag_bp", "Cumulative cash drag (bp)", ",.1f"), width="stretch"
    )
    exp = scenario_chart("exposure", "Total exposure (% of NAV)", ".1%")
    add_limit(
        exp,
        cfg.compliance.total_exposure_range.min,
        f"min {cfg.compliance.total_exposure_range.min:.0%}",
        below=True,
    )
    right.plotly_chart(exp, width="stretch")
    st.subheader("Summary")
    st.dataframe(summary.style.format("{:,.4f}"), width="stretch")


def page_audit(engine: Engine) -> None:
    types = q.event_types(engine)
    c = st.columns([3, 2, 2])
    events = c[0].multiselect("Event types", types)
    rng = c[1].date_input("Logged between", value=())
    text = c[2].text_input("Search details", placeholder="e.g. a batch id or ticker")
    start, end = (rng[0], rng[1]) if isinstance(rng, tuple) and len(rng) == 2 else (None, None)
    log = q.audit_events(engine, events or None, start, end, text or None)
    st.caption(f"{len(log)} events (newest first)")
    st.dataframe(log, hide_index=True, width="stretch")
    if not log.empty:
        pick = st.selectbox(
            "Inspect event",
            log["id"],
            format_func=lambda i: f"#{i} {log.set_index('id').loc[i, 'event']}",
        )
        st.json(log.set_index("id").loc[pick, "details"])


# --------------------------------------------------------------------------- main


def main() -> None:
    cfg = load_config()
    engine = get_engine(cfg.database_url)
    bootstrap(engine, cfg)

    st.sidebar.title("Portfolio Implementation Toolkit")
    st.sidebar.caption(f"v{__version__} · {cfg.fund.name}")
    page = st.sidebar.radio("Page", PAGES, key="page")
    dates = q.position_dates(engine)
    as_of = st.sidebar.selectbox("As of", dates[::-1], key="as_of")
    sources = ", ".join(sorted(q.data_sources(engine))) or "none"
    st.sidebar.caption(f"Data source: {sources}")

    st.title(page)
    if q.is_synthetic(engine):
        st.warning(
            "**SYNTHETIC DATA** - offline mode. Prices are simulated (correlated GBM), "
            "not real market data. Run `pit load-data` with API keys for live data.",
            icon="⚠️",
        )

    if page == "Overview":
        page_overview(engine, cfg, as_of)
    elif page == "Positions vs. Model":
        page_positions(engine, cfg, as_of)
    elif page == "Order Blotter":
        page_blotter(engine)
    elif page == "Compliance":
        page_compliance(engine, as_of)
    elif page == "Cash & Equitization":
        page_cash(engine, cfg)
    else:
        page_audit(engine)


main()
