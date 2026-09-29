"""Command by Kyber Technologies — unified outage response dashboard.

Run with:  streamlit run command_app.py
"""

from datetime import datetime, timezone

import plotly.graph_objects as go
import streamlit as st

import crew_database
import grid_topology
import incident_manager
import predictive_risk
from dispatch_optimizer import FAULT_TYPES

st.set_page_config(page_title="Command — Kyber Technologies", page_icon="⚡", layout="wide")

NAVY, PANEL, BORDER = "#0A0E1A", "#111A2E", "#1E2A44"
BLUE, GREEN, YELLOW, AMBER, RED, MUTED = "#00D4FF", "#22C55E", "#FACC15", "#F59E0B", "#EF4444", "#7B8BA8"

SEVERITY_COLORS = {"CRITICAL": RED, "HIGH": AMBER, "MEDIUM": YELLOW, "INFO": GREEN}
RISK_COLORS = {"URGENT": RED, "HIGH": AMBER, "MEDIUM": YELLOW, "MONITOR": GREEN}
CREW_COLORS = {"AVAILABLE": GREEN, "DISPATCHED": AMBER, "ON-SITE": BLUE, "OFF-DUTY": MUTED}
TREND_ARROWS = {"RISING": ("↑", RED), "DECLINING": ("↓", AMBER), "STABLE": ("→", GREEN)}
FAULT_LABELS = {
    "LINE-FAULT": "Line Fault",
    "TRANSFORMER-FAILURE": "Transformer Failure",
    "VOLTAGE-SAG": "Voltage Sag",
    "CASCADING-FAULT": "Cascading Fault",
    "CABLE-FAULT": "Cable Fault",
}
DEGRADATION_PU_PER_DAY = -0.012
TOTAL_CUSTOMERS = sum(grid_topology.BUS_CUSTOMER_COUNTS.values())


# ---------------------------------------------------------------- session state

def init_session_state():
    first_run = "registry" not in st.session_state
    defaults = {
        "registry": incident_manager.IncidentRegistry,
        "crews": crew_database.create_crew_database,
        "alert_feed": list,
        "maintenance_queue": list,
        "focus_incident_id": lambda: None,
        "degraded_bus": lambda: None,
    }
    for key, factory in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = factory()
    if first_run:
        # The grid model lives in the Python process, so a fresh browser
        # session must not inherit faults left over from an earlier one.
        grid_topology.reset_grid_state()


init_session_state()
registry = st.session_state.registry
crews = st.session_state.crews


# ---------------------------------------------------------------- styling

st.markdown(f"""<style>
.stApp {{ background: {NAVY}; color: #FFFFFF; }}
.block-container {{ padding-top: 1.2rem; padding-bottom: 1rem; max-width: 100%; }}
header[data-testid="stHeader"] {{ background: transparent; }}
section[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
div[data-testid="stMetric"] {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 12px; }}
div[data-testid="stMetricValue"] {{ font-family: ui-monospace, Menlo, monospace; color: #FFFFFF; font-size: 1.5rem; }}
div[data-testid="stMetricLabel"] p {{ color: {MUTED}; font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; }}
.stTabs [data-baseweb="tab"] {{ font-family: ui-monospace, Menlo, monospace; letter-spacing: 0.06em; }}
.stTabs [aria-selected="true"] {{ color: {BLUE} !important; }}
.k-mono {{ font-family: ui-monospace, Menlo, monospace; }}
.k-head {{ font-family: ui-monospace, Menlo, monospace; color: {BLUE}; font-size: 0.85rem; letter-spacing: 0.12em; font-weight: 700; border-bottom: 1px solid {BORDER}; padding-bottom: 6px; margin-bottom: 10px; }}
.k-card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 9px 12px; margin-bottom: 8px; }}
.k-muted {{ color: {MUTED}; font-size: 0.78rem; }}
.k-badge {{ font-family: ui-monospace, Menlo, monospace; font-size: 0.68rem; font-weight: 700; padding: 1px 7px; border-radius: 4px; border: 1px solid; letter-spacing: 0.05em; white-space: nowrap; }}
.k-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; animation: kpulse 1.6s infinite; }}
@keyframes kpulse {{ 0% {{ box-shadow: 0 0 0 0 currentColor; }} 70% {{ box-shadow: 0 0 0 8px transparent; }} 100% {{ box-shadow: 0 0 0 0 transparent; }} }}
.k-stage {{ flex: 1; text-align: center; font-family: ui-monospace, Menlo, monospace; font-size: 0.66rem; }}
.k-node {{ width: 18px; height: 18px; border-radius: 50%; margin: 0 auto 4px auto; border: 2px solid; }}
</style>""", unsafe_allow_html=True)


def badge(text, color):
    return f'<span class="k-badge" style="color:{color};border-color:{color}">{text}</span>'


def html(markup):
    st.markdown(markup, unsafe_allow_html=True)


# ---------------------------------------------------------------- helpers

def active_incidents():
    return registry.get_active_incidents()


def focus_incident():
    active = active_incidents()
    if not active:
        return None
    chosen = registry.get_incident_by_id(st.session_state.focus_incident_id)
    return chosen if chosen in active else active[-1]


def customers_without_power():
    """Customers still off supply after switching, including buses left dead by combined faults."""
    active = active_incidents()
    out = sum(i.total_customers_affected - i.customers_restored for i in active)
    incident_buses = {i.affected_bus for i in active}
    out += sum(grid_topology.BUS_CUSTOMER_COUNTS[b] for b in grid_topology.get_deenergized_buses()
               if b not in incident_buses)
    return min(out, TOTAL_CUSTOMERS)


def overloaded_lines():
    return [s["line_id"] for s in grid_topology.LINE_STATES.values()
            if s["status"] == "IN-SERVICE" and s["current_loading"] > 1.0]


def grid_health_score():
    served = 100.0 * (TOTAL_CUSTOMERS - customers_without_power()) / TOTAL_CUSTOMERS
    return max(0.0, round(served - 5.0 * len(overloaded_lines()), 1))


def relative_time(moment):
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s ago" if minutes else f"{seconds}s ago"


def push_alert(severity, title, source, incident_id=None):
    st.session_state.alert_feed.insert(0, {
        "id": f"ALERT-{len(st.session_state.alert_feed) + 1:04d}",
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "severity": severity,
        "title": title,
        "source": source,
        "incident_id": incident_id,
        "acknowledged": False,
    })


def rebuild_grid_faults():
    """Re-apply the faults of every still-active incident after one is closed."""
    grid_topology.reset_grid_state()
    for incident in active_incidents():
        grid_topology.trigger_fault_on_bus(incident.affected_bus)
        grid_topology.isolate_bus(incident.affected_bus)


# ---------------------------------------------------------------- actions (button callbacks)

def trigger_fault(fault_type):
    choice = st.session_state.get("fault_location", "default")
    bus = None if choice == "default" else int(choice)
    target = incident_manager.fault_parameters(fault_type, bus)["affected_bus"]
    if any(i.affected_bus == target for i in active_incidents()):
        st.toast(f"Bus {target} already has an active incident. Complete its repair or pick another bus.")
        return

    incident = incident_manager.respond_to_fault(fault_type, bus, registry=registry, crews=crews)
    st.session_state.focus_incident_id = incident.incident_id
    name = grid_topology.BUS_LOCATIONS[incident.affected_bus]["name"]
    push_alert(incident.severity,
               f"BUS-{incident.affected_bus} // {FAULT_LABELS[fault_type].upper()} DETECTED",
               f"DETECTED: Atlas anomaly engine · {name} bus voltage collapsed to 0.75 pu",
               incident.incident_id)
    if incident.dispatch_info:
        crew = incident.dispatch_info
        push_alert("HIGH", f"AUTO-DISPATCH // {crew['crew_id']} TO BUS-{incident.affected_bus}",
                   f"Score {crew['composite_score']:.3f} · {crew['distance_km']} km · "
                   f"ETA {crew['estimated_arrival_minutes']} min", incident.incident_id)
    else:
        push_alert("CRITICAL", f"NO CREW AVAILABLE // BUS-{incident.affected_bus}",
                   "All crews are committed or off duty; incident queued", incident.incident_id)
    for line_id in overloaded_lines():
        load = grid_topology.get_line_status(line_id)["loading_percent"]
        push_alert("HIGH", f"{line_id.upper()} // OVERLOAD {load:.0f}%",
                   "Post-fault power flow shows this branch above its rating", incident.incident_id)


def complete_repair(incident_id):
    incident = registry.get_incident_by_id(incident_id)
    if incident is None or not incident.is_active:
        return
    report = incident_manager.complete_repair(incident, crews=crews)
    rebuild_grid_faults()
    push_alert("INFO", f"BUS-{incident.affected_bus} // RESTORED",
               f"{report['total_customers_affected']:,} customers back on supply · "
               f"T+{report['operational_duration_minutes']} min", incident_id)


def reset_grid():
    incident_manager.reset_all(registry=registry, crews=crews)
    st.session_state.focus_incident_id = None
    push_alert("INFO", "GRID RESTORED // ALL SYSTEMS NOMINAL", "Simulator reset by operator")


def acknowledge(alert_id):
    for alert in st.session_state.alert_feed:
        if alert["id"] == alert_id:
            alert["acknowledged"] = True


def queue_inspection(bus):
    if bus not in st.session_state.maintenance_queue:
        st.session_state.maintenance_queue.append(bus)


def unqueue_inspection(bus):
    if bus in st.session_state.maintenance_queue:
        st.session_state.maintenance_queue.remove(bus)


def set_degradation(bus):
    st.session_state.degraded_bus = bus


# ---------------------------------------------------------------- data

@st.cache_data(ttl=300, show_spinner="Loading Atlas telemetry…")
def load_risk_data(degraded_bus):
    telemetry = predictive_risk.load_telemetry_data()
    if degraded_bus is not None:
        telemetry = predictive_risk.simulate_degradation(telemetry, degraded_bus, DEGRADATION_PU_PER_DAY)
    register = predictive_risk.generate_full_risk_register(telemetry)
    forecasts = {p["bus_number"]: predictive_risk.get_7day_forecast(telemetry, p["bus_number"])
                 for p in register}
    return register, forecasts


def grid_map(highlight_bus=None, height=330):
    """The 14 substations at their Seattle-Tacoma locations, with live line and crew status."""
    fig = go.Figure()
    graph = grid_topology.GRID_GRAPH
    dead = set(grid_topology.get_deenergized_buses())

    groups = {}
    for a, b, data in graph.edges(data=True):
        state = grid_topology.LINE_STATES[data["line_id"]]
        if state["status"] != "IN-SERVICE":
            key = ("#5B6478", "dot")
        elif state["current_loading"] > 1.0:
            key = (RED, "solid")
        elif state["current_loading"] > 0.8:
            key = (YELLOW, "solid")
        else:
            key = ("#2B6CB0", "solid")
        xs, ys = groups.setdefault(key, ([], []))
        xs += [graph.nodes[a]["lon"], graph.nodes[b]["lon"], None]
        ys += [graph.nodes[a]["lat"], graph.nodes[b]["lat"], None]
    for (color, dash), (xs, ys) in groups.items():
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip",
                                 line=dict(color=color, width=2.5, dash=dash)))

    nodes = list(graph.nodes(data=True))
    fig.add_trace(go.Scatter(
        x=[d["lon"] for _, d in nodes], y=[d["lat"] for _, d in nodes],
        mode="markers+text", text=[str(b) for b, _ in nodes], textposition="middle center",
        textfont=dict(color=NAVY, size=10, family="monospace"),
        marker=dict(size=[16 + d["customers"] / 400 for _, d in nodes],
                    color=[RED if b in dead else GREEN for b, _ in nodes],
                    line=dict(color=[BLUE if b == highlight_bus else NAVY for b, _ in nodes], width=3)),
        hovertext=[f"Bus {b} · {d['name']}<br>{d['customers']:,} customers<br>"
                   f"{'DE-ENERGISED' if b in dead else 'Energised'}" for b, d in nodes],
        hoverinfo="text"))

    crew_list = list(crews.values())
    fig.add_trace(go.Scatter(
        x=[c["base_location"]["lon"] for c in crew_list], y=[c["base_location"]["lat"] for c in crew_list],
        mode="markers", marker=dict(symbol="diamond", size=10, color=[CREW_COLORS[c["status"]] for c in crew_list],
                                    line=dict(color=NAVY, width=1)),
        hovertext=[f"{c['crew_id']} · {c['base_city']}<br>{c['status']}" for c in crew_list],
        hoverinfo="text"))

    fig.update_layout(height=height, margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
                      paper_bgcolor=PANEL, plot_bgcolor=PANEL, hoverlabel=dict(font_family="monospace"))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False, scaleanchor="x", scaleratio=1.48)
    return fig


# ---------------------------------------------------------------- header

active = active_incidents()
focus = focus_incident()
in_fault = bool(active)
deployed = sum(1 for c in crews.values() if c["status"] in ("DISPATCHED", "ON-SITE"))
restored_now = sum(i.customers_restored for i in active)

brand, status, metrics = st.columns([1.2, 1.5, 2.3], vertical_alignment="center")
with brand:
    html(f'<div class="k-mono" style="color:{BLUE};font-size:0.8rem;letter-spacing:0.2em">KYBER TECHNOLOGIES</div>'
         f'<div style="font-size:1.9rem;font-weight:800;letter-spacing:0.12em;line-height:1.1">COMMAND</div>'
         f'<div class="k-muted">Autonomous outage response orchestration</div>')
with status:
    color, text = (RED, "RESPONSE ACTIVE — INCIDENT IN PROGRESS") if in_fault else (GREEN, "STANDBY — ALL SYSTEMS NOMINAL")
    html(f'<div class="k-card k-mono" style="color:{color};font-weight:700;font-size:0.85rem;text-align:center;margin:0">'
         f'<span class="k-dot" style="background:{color};color:{color}"></span>{text}</div>')
with metrics:
    m1, m2, m3 = st.columns(3)
    m1.metric("Active Incidents", len(active))
    m2.metric("Crews Deployed", f"{deployed} / {len(crews)}")
    m3.metric("Customers Restored", f"{restored_now:,}")

left, center, right = st.columns([1, 2, 1], gap="medium")


# ---------------------------------------------------------------- left: crew board

with left:
    html('<div class="k-head">CREW BOARD</div>')
    cards = []
    for crew in crews.values():
        color = CREW_COLORS[crew["status"]]
        parts = sum(1 for qty in crew["parts_inventory"].values() if qty > 0)
        responding = ""
        if crew["active_incident"]:
            responding = (f'<div class="k-mono" style="color:{color};font-size:0.72rem;margin-top:3px">'
                          f'RESPONDING TO: {crew["active_incident"]}</div>')
        cards.append(
            f'<div class="k-card" style="border-left:3px solid {color};padding:7px 10px;margin-bottom:6px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center">'
            f'<b style="font-size:0.88rem">{crew["name"]}</b>{badge(crew["status"], color)}</div>'
            f'<div class="k-muted">{crew["crew_id"]} · {crew["base_city"]} · {crew["vehicle_type"]}</div>'
            f'<div class="k-muted">{", ".join(crew["specializations"])} · {parts} part types</div>'
            f'{responding}</div>')
    html("".join(cards))


# ---------------------------------------------------------------- center: tabs

def render_active_response():
    if focus is None:
        html(f'<div class="k-card" style="text-align:center;padding:26px 12px">'
             f'<div style="width:46px;height:46px;border-radius:50%;background:{GREEN};margin:0 auto 12px auto;'
             f'box-shadow:0 0 24px {GREEN}88"></div>'
             f'<div class="k-mono" style="color:{MUTED};font-size:1.05rem;letter-spacing:0.08em">'
             f'MONITORING — NO ACTIVE INCIDENTS</div>'
             f'<div class="k-muted" style="margin-top:4px">All 14 grid nodes operating within normal parameters</div>'
             f'<div class="k-mono" style="margin-top:10px;color:{GREEN}">GRID HEALTH: NOMINAL · {grid_health_score():.0f}%</div>'
             f'</div>')
        st.plotly_chart(grid_map(), config={"displayModeBar": False}, key="map_idle")
        html('<div class="k-muted">Use the Fault Simulator in the sidebar to trigger an incident.</div>')
        return

    if len(active) > 1:
        ids = [i.incident_id for i in active]
        chosen = st.selectbox(
            "Focus incident", ids, index=ids.index(focus.incident_id),
            format_func=lambda i: (lambda inc: f"{inc.incident_id} · {FAULT_LABELS[inc.fault_type]} · "
                                               f"Bus {inc.affected_bus}")(registry.get_incident_by_id(i)))
        if chosen != focus.incident_id:
            st.session_state.focus_incident_id = chosen
            st.rerun()

    inc = focus
    sev = SEVERITY_COLORS[inc.severity]
    name = grid_topology.BUS_LOCATIONS[inc.affected_bus]["name"]
    html(f'<div class="k-card" style="border-left:3px solid {sev}">'
         f'<div style="display:flex;justify-content:space-between;align-items:center">'
         f'<span class="k-mono" style="color:{BLUE};font-weight:700;font-size:1.05rem">{inc.incident_id}</span>'
         f'{badge(inc.severity, sev)}</div>'
         f'<div style="font-weight:700;margin-top:4px">{FAULT_LABELS[inc.fault_type].upper()} · BUS {inc.affected_bus} ({name})</div>'
         f'<div class="k-muted">{inc.fault_description}</div>'
         f'<div class="k-mono" style="font-size:0.8rem;margin-top:6px">'
         f'Detected {relative_time(inc.created_at)} · {inc.total_customers_affected:,} customers affected'
         f'{" · secondary buses " + ", ".join(map(str, inc.secondary_buses)) if inc.secondary_buses else ""}</div>'
         f'</div>')

    reached = {h["state"]: h["elapsed_minutes"] for h in inc.state_history}
    nodes = []
    for state in incident_manager.INCIDENT_STATES:
        if state == inc.state:
            color, fill = BLUE, BLUE
        elif state in reached:
            color, fill = GREEN, GREEN
        else:
            color, fill = MUTED, "transparent"
        when = f"T+{reached[state]}m" if state in reached else "—"
        nodes.append(f'<div class="k-stage" style="color:{color}"><div class="k-node" '
                     f'style="border-color:{color};background:{fill}"></div>{state}<br>'
                     f'<span style="color:{MUTED}">{when}</span></div>')
    html(f'<div class="k-card"><div class="k-muted" style="margin-bottom:6px">RESPONSE TIMELINE (operational minutes)</div>'
         f'<div style="display:flex;position:relative">'
         f'<div style="position:absolute;top:9px;left:8%;right:8%;height:2px;background:{BORDER}"></div>'
         f'{"".join(nodes)}</div></div>')

    seq = inc.switching_sequence
    crew = inc.dispatch_info
    c1, c2, c3, c4 = st.columns(4)
    switching_done = inc.state_history[3]["elapsed_minutes"] if len(inc.state_history) > 3 else None
    c1.metric("Switching Complete", f"T+{switching_done} min" if switching_done is not None else "—",
              help="Operational minutes from detection until all switching steps finish")
    c2.metric("Customers Restored", f"{inc.customers_restored:,}",
              help=f"Restored by switching out of {inc.total_customers_affected:,} affected")
    c3.metric("Crew ETA", f"{crew['estimated_arrival_minutes']} min" if crew else "NO CREW")
    c4.metric("Restoration", f"{seq['restoration_percentage']:.1f}%" if seq else "—")

    map_col, steps_col = st.columns([1, 1.15])
    with map_col:
        st.plotly_chart(grid_map(inc.affected_bus, height=380), config={"displayModeBar": False},
                        key="map_incident")
        html(f'<div class="k-muted">Circles: substations (red = de-energised). Diamonds: crews. '
             f'Dotted: isolated branches. Red lines: overloaded.</div>')
    with steps_col:
        html('<div class="k-head">SWITCHING SEQUENCE — AUTOMATED RESPONSE</div>')
        rows = []
        for step in seq["steps"]:
            remote = step["is_remote_operable"]
            tag = badge("REMOTE — EXECUTABLE", BLUE) if remote else badge("MANUAL — CREW REQUIRED", AMBER)
            rows.append(
                f'<div class="k-card" style="padding:7px 10px;margin-bottom:6px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<span class="k-mono"><b>{step["step_number"]:02d} {step["action"]} {step["switch_id"]}</b></span>{tag}</div>'
                f'<div style="font-size:0.8rem">{step["location_description"]}</div>'
                f'<div class="k-muted" style="font-style:italic">{step["rationale"]}</div>'
                f'<div class="k-mono" style="font-size:0.75rem;color:{GREEN if step["customers_restored"] else MUTED}">'
                f'{step["customers_restored"]:,} customers restored · {step["time_estimate_minutes"]} min</div></div>')
        with st.container(height=420, border=False):
            html("".join(rows))

    st.button(f"✓ Complete repair and close {inc.incident_id}", type="primary", width="stretch",
              on_click=complete_repair, args=(inc.incident_id,), key=f"close_{inc.incident_id}")


def render_predictive_risk():
    try:
        register, forecasts = load_risk_data(st.session_state.degraded_bus)
    except FileNotFoundError:
        st.error("Atlas training data not found — complete the Atlas build before running Command. "
                 "Copy generate_telemetry.py from Atlas into this folder and run `python generate_telemetry.py`.")
        return
    except ValueError as exc:
        st.error(f"Atlas training data could not be read: {exc}")
        return

    if st.session_state.degraded_bus is not None:
        st.warning(f"SIMULATED: a failing voltage regulator is being injected on Bus "
                   f"{st.session_state.degraded_bus} ({DEGRADATION_PU_PER_DAY} pu/day over the last 72 h).")

    counts = {level: sum(p["risk_level"] == level for p in register) for level in RISK_COLORS}
    boxes = "".join(
        f'<div class="k-card" style="flex:1;text-align:center;border-top:3px solid {color};margin:0">'
        f'<div class="k-mono" style="font-size:1.6rem;color:{color}">{counts[level]}</div>'
        f'<div class="k-muted">{level}</div></div>' for level, color in RISK_COLORS.items())
    html(f'<div style="display:flex;gap:8px;margin-bottom:10px">{boxes}</div>')

    with st.expander("Demo: simulate equipment degradation"):
        buses = list(range(1, 15))
        pick = st.selectbox("Bus", buses, index=11,
                            format_func=lambda b: f"Bus {b} · {grid_topology.BUS_LOCATIONS[b]['name']}")
        d1, d2 = st.columns(2)
        d1.button("Inject degradation", on_click=set_degradation, args=(pick,), width="stretch")
        d2.button("Clear simulation", on_click=set_degradation, args=(None,), width="stretch",
                  disabled=st.session_state.degraded_bus is None)

    queue = st.session_state.maintenance_queue
    columns = st.columns(2)
    for index, p in enumerate(register):
        color = RISK_COLORS[p["risk_level"]]
        arrow, arrow_color = TREND_ARROWS[p["trend_direction"]]
        days = "STABLE" if p["days_to_fault"] >= predictive_risk.NO_BREACH_DAYS else f"{p['days_to_fault']:.1f} DAYS"
        deviation = p["current_voltage_mean"] - p["baseline_voltage_mean"]
        spark = "".join(
            f'<span title="Day {d["day"]}: {d["projected_voltage_mean"]:.3f} pu" style="display:inline-block;'
            f'width:12%;height:6px;margin-right:1%;border-radius:2px;background:{RISK_COLORS[d["projected_risk_level"]]}"></span>'
            for d in forecasts[p["bus_number"]])
        with columns[index % 2]:
            html(f'<div class="k-card" style="border-left:3px solid {color}">'
                 f'<div style="display:flex;justify-content:space-between;align-items:center">'
                 f'<b>{p["bus_name"]} · {p["substation"]}</b>{badge(p["risk_level"], color)}</div>'
                 f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:4px">'
                 f'<span class="k-mono" style="font-size:1.3rem">{days}</span>'
                 f'<span class="k-mono" style="color:{arrow_color}">{arrow} {p["trend_direction"]}</span></div>'
                 f'<div class="k-mono k-muted">V-MAG {p["current_voltage_mean"]:.4f} pu · Δ {deviation:+.4f} · '
                 f'score {p["risk_score"]}</div>'
                 f'<div style="margin-top:6px" title="7-day forecast">{spark}</div></div>')
            queued = p["bus_number"] in queue
            st.button("✓ QUEUED" if queued else "SCHEDULE INSPECTION", key=f"inspect_{p['bus_number']}",
                      on_click=queue_inspection, args=(p["bus_number"],), disabled=queued, width="stretch")

    html('<div class="k-head" style="margin-top:12px">MAINTENANCE QUEUE</div>')
    if not queue:
        html('<div class="k-muted">No inspections scheduled.</div>')
    for position, bus in enumerate(queue, start=1):
        profile = next((p for p in register if p["bus_number"] == bus), None)
        q1, q2 = st.columns([4, 1], vertical_alignment="center")
        q1.markdown(f"**{position}. BUS-{bus}** · {grid_topology.BUS_LOCATIONS[bus]['name']}"
                    + (f" · {profile['recommended_action']}" if profile else ""))
        q2.button("Remove", key=f"unqueue_{bus}", on_click=unqueue_inspection, args=(bus,), width="stretch")


def render_incident_log():
    incidents = sorted(registry.incidents, key=lambda i: i.created_at, reverse=True)
    html(f'<div class="k-head">INCIDENT LOG — SESSION HISTORY · {len(incidents)} INCIDENT'
         f'{"" if len(incidents) == 1 else "S"}</div>')
    if not incidents:
        html(f'<div class="k-card k-mono" style="text-align:center;color:{MUTED};padding:24px">'
             f'NO INCIDENTS THIS SESSION — GRID NOMINAL</div>')
        return

    for inc in incidents:
        seq = inc.switching_sequence or {}
        first_restore = next((e["elapsed_minutes"] for e in inc.resolution_timeline if e["customers_restored"] > 0), None)
        status_badge = badge("RESOLVED", GREEN) if not inc.is_active else badge(inc.state, RED)
        crew = inc.dispatch_info["crew_id"] if inc.dispatch_info else "none"
        duration = f"T+{inc.elapsed_minutes} min" if not inc.is_active else "in progress"
        html(f'<div class="k-card" style="border-left:3px solid {SEVERITY_COLORS[inc.severity]}">'
             f'<div style="display:flex;justify-content:space-between;align-items:center">'
             f'<span class="k-mono" style="color:{BLUE};font-weight:700">{inc.incident_id}</span>'
             f'<span>{badge(inc.severity, SEVERITY_COLORS[inc.severity])} {status_badge}</span></div>'
             f'<div style="font-weight:600">{FAULT_LABELS[inc.fault_type]} · Bus {inc.affected_bus} '
             f'({grid_topology.BUS_LOCATIONS[inc.affected_bus]["name"]})</div>'
             f'<div class="k-mono k-muted">Detected {inc.created_at.astimezone():%H:%M:%S} · '
             f'first restoration {"T+" + str(first_restore) + " min" if first_restore is not None else "—"} · '
             f'{seq.get("restoration_percentage", 0):.1f}% by switching · crew {crew} · {duration}</div></div>')

    closed = [i for i in incidents if not i.is_active]
    dispatched = [i for i in incidents if i.dispatch_info]
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Incidents Handled", len(incidents))
    s2.metric("Avg Crew ETA", f"{sum(i.dispatch_info['estimated_arrival_minutes'] for i in dispatched) / len(dispatched):.0f} min"
              if dispatched else "—")
    s3.metric("Avg Switching Restore",
              f"{sum(i.switching_sequence['restoration_percentage'] for i in incidents) / len(incidents):.1f}%")
    s4.metric("Customers Affected", f"{sum(i.total_customers_affected for i in incidents):,}")
    if closed:
        avg = sum(i.elapsed_minutes for i in closed) / len(closed)
        html(f'<div class="k-muted">Detection is immediate (Atlas anomaly engine, T+0). '
             f'Average time to full restoration for resolved incidents: {avg:.0f} operational minutes.</div>')


with center:
    tab_response, tab_risk, tab_log = st.tabs(["ACTIVE RESPONSE", "PREDICTIVE RISK", "INCIDENT LOG"])
    with tab_response:
        render_active_response()
    with tab_risk:
        render_predictive_risk()
    with tab_log:
        render_incident_log()


# ---------------------------------------------------------------- right: intelligence feed

with right:
    html('<div class="k-head">INTELLIGENCE FEED</div>')
    feed_filter = st.segmented_control("Filter", ["ALL", "CRITICAL", "HIGH"], default="ALL", required=True,
                                       key="feed_filter", label_visibility="collapsed")
    alerts = [a for a in st.session_state.alert_feed if feed_filter == "ALL" or a["severity"] == feed_filter]
    with st.container(height=360, border=False):
        if not alerts:
            html(f'<div class="k-card" style="border-left:3px solid {GREEN}">'
                 f'<div class="k-mono" style="color:{GREEN};font-weight:700">MONITORING — NO ACTIVE ALERTS</div>'
                 f'<div class="k-muted">Atlas anomaly detection is watching all 14 buses.</div></div>')
        for alert in alerts:
            color = SEVERITY_COLORS[alert["severity"]]
            faded = "opacity:0.55;" if alert["acknowledged"] else ""
            html(f'<div class="k-card" style="border-left:3px solid {color};{faded}margin-bottom:4px">'
                 f'<div style="display:flex;justify-content:space-between">'
                 f'<span class="k-mono k-muted">{alert["timestamp"]}</span>{badge(alert["severity"], color)}</div>'
                 f'<div style="font-weight:700;font-size:0.85rem;margin-top:3px">{alert["title"]}</div>'
                 f'<div class="k-muted">{alert["source"]}</div></div>')
            if not alert["acknowledged"]:
                st.button("ACKNOWLEDGE", key=f"ack_{alert['id']}", on_click=acknowledge,
                          args=(alert["id"],), type="tertiary")

    if focus is not None:
        seq, crew = focus.switching_sequence, focus.dispatch_info
        steps = "".join(
            f'<div class="k-mono" style="font-size:0.78rem">{s["step_number"]:02d} {s["action"]} {s["switch_id"]}</div>'
            for s in seq["steps"])
        if crew:
            crew_status = crews[crew["crew_id"]]["status"]
            crew_card = (f'<div class="k-card" style="margin:8px 0 0 0;border-left:3px solid {CREW_COLORS[crew_status]}">'
                         f'<div class="k-muted">CREW DISPATCH</div><b>{crew["crew_name"]}</b> '
                         f'{badge(crew_status, CREW_COLORS[crew_status])}'
                         f'<div class="k-mono" style="font-size:0.78rem">{crew["distance_km"]} km · '
                         f'ETA {crew["estimated_arrival_minutes"]} min · score {crew["composite_score"]:.3f}</div></div>')
        else:
            crew_card = f'<div class="k-mono" style="color:{RED};margin-top:8px">NO CREW AVAILABLE</div>'
        html(f'<div class="k-card" style="border:1px solid {RED};margin-top:8px">'
             f'<div class="k-mono" style="color:{RED};font-weight:700;font-size:0.8rem;margin-bottom:6px">'
             f'ACTIVE FAULT // RECOMMENDED RESPONSE</div>'
             f'<div style="font-size:0.85rem;margin-bottom:6px"><b>{FAULT_LABELS[focus.fault_type]}</b> · '
             f'Bus {focus.affected_bus}</div>{steps}{crew_card}</div>')


# ---------------------------------------------------------------- sidebar: fault simulator

with st.sidebar:
    html(f'<div class="k-mono" style="color:{BLUE};font-weight:700;letter-spacing:0.12em">FAULT SIMULATOR</div>'
         f'<div class="k-mono" style="color:{RED};font-size:0.7rem;letter-spacing:0.1em">RESTRICTED — OPERATIONAL USE ONLY</div>')
    st.divider()
    st.selectbox("Fault location", ["default"] + [str(b) for b in range(1, 15)], key="fault_location",
                 format_func=lambda v: "Default bus for each fault type" if v == "default"
                 else f"Bus {v} · {grid_topology.BUS_LOCATIONS[int(v)]['name']}")
    for fault in FAULT_TYPES:
        st.button(f"Trigger {FAULT_LABELS[fault]}", key=f"trigger_{fault}", on_click=trigger_fault,
                  args=(fault,), width="stretch")
    st.divider()
    if focus is not None:
        st.button(f"Complete repair · {focus.incident_id}", on_click=complete_repair,
                  args=(focus.incident_id,), width="stretch", key="sidebar_close")
    st.button("Reset Grid", type="primary", on_click=reset_grid, width="stretch")

    with st.expander("ACTIVE RESPONSE METRICS", expanded=True):
        st.metric("Command Health Score", f"{grid_health_score():.1f}")
        st.metric("Active Incidents", len(active))
        off = customers_without_power()
        st.metric("Customers Without Power", f"{off:,}")
        if in_fault:
            affected = sum(i.total_customers_affected for i in active)
            pct = 100 * sum(i.customers_restored for i in active) / affected if affected else 100
            st.metric("Restoration (switching)", f"{pct:.1f}%")
        overloads = overloaded_lines()
        if overloads:
            st.caption("Overloaded: " + ", ".join(overloads))