"""IEEE 14-bus grid model for Command (Module B).

Builds the IEEE 14-bus network in PyPSA from MATPOWER case14 data
(135 kV, 100 MVA base; PyPSA has no built-in case14), runs an AC power flow
for realistic line loadings, and exposes it as a NetworkX graph.

It also places each bus at a substation in the Seattle-Tacoma territory so
faults can be passed to the dispatch optimizer, and tracks live line states.

Run this file directly to print the grid:  python grid_topology.py
"""

import copy
import logging
import math

import networkx as nx
import numpy as np
import pandas as pd
import pypsa

pypsa.options.api.legacy_string_dtype = True
logging.getLogger("pypsa").setLevel(logging.WARNING)

V_NOM_KV = 135.0
S_BASE_MVA = 100.0
Z_BASE_OHM = V_NOM_KV ** 2 / S_BASE_MVA

# MATPOWER case14 bus data: bus -> (Pd MW, Qd MVAr)
BUS_LOADS = {
    1: (0.0, 0.0), 2: (21.7, 12.7), 3: (94.2, 19.0), 4: (47.8, -3.9),
    5: (7.6, 1.6), 6: (11.2, 7.5), 7: (0.0, 0.0), 8: (0.0, 0.0),
    9: (29.5, 16.6), 10: (9.0, 5.8), 11: (3.5, 1.8), 12: (6.1, 1.6),
    13: (13.5, 5.8), 14: (14.9, 5.0),
}
BUS_SHUNT_MVAR = {9: 19.0}

# MATPOWER case14 generators: bus -> (Pg MW, voltage setpoint pu). Bus 1 is the slack.
GENERATORS = {1: (232.4, 1.060), 2: (40.0, 1.045), 3: (0.0, 1.010), 6: (0.0, 1.070), 8: (0.0, 1.090)}
GENERATION_BUSES = (1, 2)  # the only buses producing real power; 3, 6, 8 are synchronous condensers

# MATPOWER case14 branches: (from, to, r pu, x pu, b pu, tap ratio). Tap ratio > 0 = transformer.
BRANCHES = [
    (1, 2, 0.01938, 0.05917, 0.0528, 0), (1, 5, 0.05403, 0.22304, 0.0492, 0),
    (2, 3, 0.04699, 0.19797, 0.0438, 0), (2, 4, 0.05811, 0.17632, 0.0340, 0),
    (2, 5, 0.05695, 0.17388, 0.0346, 0), (3, 4, 0.06701, 0.17103, 0.0128, 0),
    (4, 5, 0.01335, 0.04211, 0.0, 0), (4, 7, 0.0, 0.20912, 0.0, 0.978),
    (4, 9, 0.0, 0.55618, 0.0, 0.969), (5, 6, 0.0, 0.25202, 0.0, 0.932),
    (6, 11, 0.09498, 0.19890, 0.0, 0), (6, 12, 0.12291, 0.25581, 0.0, 0),
    (6, 13, 0.06615, 0.13027, 0.0, 0), (7, 8, 0.0, 0.17615, 0.0, 0),
    (7, 9, 0.0, 0.11001, 0.0, 0), (9, 10, 0.03181, 0.08450, 0.0, 0),
    (9, 14, 0.12711, 0.27038, 0.0, 0), (10, 11, 0.08205, 0.19207, 0.0, 0),
    (12, 13, 0.22092, 0.19988, 0.0, 0), (13, 14, 0.17093, 0.34802, 0.0, 0),
]

# Standard ratings; each branch gets the smallest one that keeps base-case loading at or below 75%.
STANDARD_RATINGS_MVA = (50, 75, 100, 150, 200, 250)
MAX_BASE_LOADING = 0.75

# Customers follow case14 load so they match the bus sizes on the Atlas map.
# Buses 1, 7 and 8 carry no load in case14, so they serve no customers.
BUS_CUSTOMER_COUNTS = {
    1: 0, 2: 2350, 3: 8500, 4: 4560, 5: 1150, 6: 1450, 7: 0,
    8: 0, 9: 3010, 10: 1270, 11: 800, 12: 1220, 13: 1650, 14: 1770,
}

BUS_LOCATIONS = {
    1: {"name": "Tacoma Generating Station", "lat": 47.2400, "lon": -122.4200},
    2: {"name": "Auburn Generating Station", "lat": 47.3200, "lon": -122.2100},
    3: {"name": "Kent Valley", "lat": 47.3900, "lon": -122.2600},
    4: {"name": "Renton", "lat": 47.4700, "lon": -122.2000},
    5: {"name": "Tukwila", "lat": 47.4750, "lon": -122.2600},
    6: {"name": "SeaTac", "lat": 47.4400, "lon": -122.3000},
    7: {"name": "Newcastle", "lat": 47.5300, "lon": -122.1600},
    8: {"name": "Issaquah Condenser", "lat": 47.5300, "lon": -122.0400},
    9: {"name": "Bellevue", "lat": 47.6000, "lon": -122.1900},
    10: {"name": "Mercer Island", "lat": 47.5700, "lon": -122.2200},
    11: {"name": "Rainier Valley", "lat": 47.5400, "lon": -122.2800},
    12: {"name": "Burien", "lat": 47.4700, "lon": -122.3500},
    13: {"name": "White Center", "lat": 47.5200, "lon": -122.3500},
    14: {"name": "Seattle Downtown", "lat": 47.6050, "lon": -122.3350},
}

LINE_STATUSES = ("IN-SERVICE", "FAULTED", "ISOLATED")


def branch_id(bus_a, bus_b, is_transformer):
    low, high = sorted((bus_a, bus_b))
    return f"{'Trafo' if is_transformer else 'Line'}-{low}-{high}"


def build_pypsa_network(out_of_service_buses=()):
    """Return a PyPSA network of case14 without the given buses and their branches."""
    out = set(out_of_service_buses)
    n = pypsa.Network()
    for bus, (pd_mw, qd_mvar) in BUS_LOADS.items():
        if bus in out:
            continue
        v_set = GENERATORS.get(bus, (0.0, 1.0))[1]
        n.add("Bus", str(bus), v_nom=V_NOM_KV, v_mag_pu_set=v_set)
        if pd_mw or qd_mvar:
            n.add("Load", f"Load-{bus}", bus=str(bus), p_set=pd_mw, q_set=qd_mvar)
        if bus in BUS_SHUNT_MVAR:
            n.add("ShuntImpedance", f"Shunt-{bus}", bus=str(bus),
                  b=BUS_SHUNT_MVAR[bus] / S_BASE_MVA / Z_BASE_OHM)
    for bus, (pg_mw, _) in GENERATORS.items():
        if bus in out:
            continue
        n.add("Generator", f"Gen-{bus}", bus=str(bus), p_set=pg_mw,
              control="Slack" if bus == 1 else "PV")
    for f, t, r, x, b, tap in BRANCHES:
        if f in out or t in out:
            continue
        name = branch_id(f, t, tap > 0)
        if tap > 0:
            n.add("Transformer", name, bus0=str(f), bus1=str(t), s_nom=S_BASE_MVA,
                  r=r, x=x, tap_ratio=tap, model="pi")
        else:
            n.add("Line", name, bus0=str(f), bus1=str(t), s_nom=S_BASE_MVA,
                  r=r * Z_BASE_OHM, x=x * Z_BASE_OHM, b=b / Z_BASE_OHM)
    return n


def solve_branch_flows_mva(out_of_service_buses=()):
    """Run an AC power flow and return {branch_id: apparent power flow in MVA}."""
    n = build_pypsa_network(out_of_service_buses)
    n.pf()
    flows = {}
    for component, table in (("lines", n.lines_t), ("transformers", n.transformers_t)):
        for name in getattr(n, component).index:
            s0 = np.hypot(table.p0[name].iloc[0], table.q0[name].iloc[0])
            s1 = np.hypot(table.p1[name].iloc[0], table.q1[name].iloc[0])
            flows[name] = float(max(s0, s1))
    return flows


def build_ieee14_graph():
    """NetworkX graph of the 14 buses with one edge per line or transformer."""
    base_flows = solve_branch_flows_mva()
    graph = nx.Graph()
    for bus, info in BUS_LOCATIONS.items():
        graph.add_node(bus, name=info["name"], lat=info["lat"], lon=info["lon"],
                       customers=BUS_CUSTOMER_COUNTS[bus],
                       is_generation=bus in GENERATION_BUSES)
    for f, t, _, _, _, tap in BRANCHES:
        is_transformer = tap > 0
        line_id = branch_id(f, t, is_transformer)
        flow = base_flows[line_id]
        capacity = next((r for r in STANDARD_RATINGS_MVA if flow / r <= MAX_BASE_LOADING),
                        STANDARD_RATINGS_MVA[-1])
        graph.add_edge(f, t, line_id=line_id, capacity_mva=float(capacity),
                       is_transformer=is_transformer, flow_mva=round(flow, 1),
                       initial_loading=round(flow / capacity, 3))
    return graph


GRID_GRAPH = build_ieee14_graph()

_INITIAL_LINE_STATES = {
    data["line_id"]: {
        "line_id": data["line_id"],
        "buses": (min(a, b), max(a, b)),
        "status": "IN-SERVICE",
        "current_loading": data["initial_loading"],
    }
    for a, b, data in GRID_GRAPH.edges(data=True)
}
LINE_STATES = copy.deepcopy(_INITIAL_LINE_STATES)
FAULTED_BUSES = set()


def get_bus_location(bus):
    """{'lat', 'lon'} of a bus's substation, ready for dispatch_optimizer."""
    info = BUS_LOCATIONS[int(bus)]
    return {"lat": info["lat"], "lon": info["lon"]}


def get_line_status(line_id):
    """Current status and loading of a line or transformer."""
    state = LINE_STATES.get(line_id)
    if state is None:
        raise ValueError(f"Unknown line ID: {line_id!r}")
    return {"line_id": line_id, "status": state["status"],
            "current_loading": state["current_loading"],
            "loading_percent": round(state["current_loading"] * 100, 1)}


def trigger_fault_on_bus(bus):
    """Mark every branch at a bus FAULTED and re-solve the power flow for the rest.

    Returns the list of line IDs that were faulted.
    """
    bus = int(bus)
    if bus not in GRID_GRAPH:
        raise ValueError(f"Bus {bus} is not in the IEEE 14-bus network")

    FAULTED_BUSES.add(bus)
    faulted = []
    for neighbour in GRID_GRAPH.neighbors(bus):
        line_id = GRID_GRAPH.edges[bus, neighbour]["line_id"]
        LINE_STATES[line_id]["status"] = "FAULTED"
        LINE_STATES[line_id]["current_loading"] = 0.0
        faulted.append(line_id)

    # Buses cut off from every generator are dead; leaving them in would give
    # the power flow an island with no supply, which it cannot solve.
    out_buses = set(get_deenergized_buses())
    flows = solve_branch_flows_mva(out_buses) if len(out_buses) < len(GRID_GRAPH) else {}
    for line_id, state in LINE_STATES.items():
        if state["status"] == "IN-SERVICE":
            a, b = state["buses"]
            capacity = GRID_GRAPH.edges[a, b]["capacity_mva"]
            state["current_loading"] = round(flows.get(line_id, 0.0) / capacity, 3)
    return faulted


def isolate_bus(bus):
    """Change the FAULTED branches at a bus to ISOLATED once switching has opened them."""
    for neighbour in GRID_GRAPH.neighbors(int(bus)):
        state = LINE_STATES[GRID_GRAPH.edges[int(bus), neighbour]["line_id"]]
        if state["status"] == "FAULTED":
            state["status"] = "ISOLATED"


def reset_grid_state():
    """Put every line back IN-SERVICE at its base-case loading."""
    LINE_STATES.clear()
    LINE_STATES.update(copy.deepcopy(_INITIAL_LINE_STATES))
    FAULTED_BUSES.clear()


def get_deenergized_buses():
    """Buses with no in-service path to a generation bus (including faulted buses)."""
    live = nx.Graph()
    live.add_nodes_from(b for b in GRID_GRAPH.nodes if b not in FAULTED_BUSES)
    live.add_edges_from((a, b) for a, b, data in GRID_GRAPH.edges(data=True)
                        if LINE_STATES[data["line_id"]]["status"] == "IN-SERVICE")
    energized = set()
    for source in GENERATION_BUSES:
        if source in live:
            energized |= nx.node_connected_component(live, source)
    return sorted(set(GRID_GRAPH.nodes) - energized)


if __name__ == "__main__":
    print(f"IEEE 14-BUS GRID - {GRID_GRAPH.number_of_nodes()} buses, "
          f"{GRID_GRAPH.number_of_edges()} branches")
    print("=" * 72)
    print(f"{'Bus':<5}{'Substation':<28}{'Customers':>10}{'Lat':>10}{'Lon':>11}")
    for bus, data in GRID_GRAPH.nodes(data=True):
        print(f"{bus:<5}{data['name']:<28}{data['customers']:>10,}"
              f"{data['lat']:>10.4f}{data['lon']:>11.4f}")
    print(f"Total customers: {sum(BUS_CUSTOMER_COUNTS.values()):,}")

    print(f"\n{'Branch':<12}{'Flow MVA':>10}{'Rating':>8}{'Loading':>9}")
    for _, _, data in sorted(GRID_GRAPH.edges(data=True), key=lambda e: sorted(e[:2])):
        print(f"{data['line_id']:<12}{data['flow_mva']:>10.1f}{data['capacity_mva']:>8.0f}"
              f"{data['initial_loading'] * 100:>8.1f}%")

    print("\nState test: fault on bus 9")
    before = get_line_status("Line-9-14")["loading_percent"]
    faulted = trigger_fault_on_bus(9)
    print(f"  Faulted: {', '.join(faulted)}")
    print(f"  Line-9-14 status: {get_line_status('Line-9-14')['status']}")
    print(f"  Line-13-14 loading: {_INITIAL_LINE_STATES['Line-13-14']['current_loading'] * 100:.1f}%"
          f" -> {get_line_status('Line-13-14')['loading_percent']}% (picks up bus 14's load)")
    reset_grid_state()
    assert get_line_status("Line-9-14")["loading_percent"] == before

    for bus in (9, 4, 6):
        trigger_fault_on_bus(bus)
    print(f"  Faults on buses 9, 4 and 6 together: de-energised buses {get_deenergized_buses()}")
    reset_grid_state()
    assert get_deenergized_buses() == []
    print("  Reset OK. Grid topology ready.")