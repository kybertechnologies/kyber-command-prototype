"""Switching sequence engine for Command (Module B).

For a fault on an IEEE 14-bus substation, works out which switches to open to
isolate the fault and which ties to close to restore customers from
neighbouring substations before the crew finishes the repair.

The steps are derived from the grid topology and base-case line loadings in
grid_topology.py, so different buses and fault types give different sequences.

Run this file directly to see a bus 9 example:  python switching_calculator.py
"""

import itertools
from collections import defaultdict

import networkx as nx

from dispatch_optimizer import FAULT_TYPES
from grid_topology import (
    BUS_CUSTOMER_COUNTS,
    BUS_LOCATIONS,
    GENERATION_BUSES,
    GRID_GRAPH,
    LINE_STATES,
    get_bus_location,
    get_deenergized_buses,
    get_line_status,
    isolate_bus,
    reset_grid_state,
    trigger_fault_on_bus,
)

# A distribution tie between two substations can carry roughly one feeder's
# worth of customers, scaled by how much spare capacity the neighbour has.
TIE_TRANSFER_LIMIT_CUSTOMERS = 1200
# Share of customers a switched capacitor bank brings back to normal voltage.
CAPACITOR_VOLTAGE_RECOVERY = 0.70

MAX_TIES = {"TRANSFORMER-FAILURE": 2, "CASCADING-FAULT": 3, "VOLTAGE-SAG": 1, "CABLE-FAULT": 1}

STEP_MINUTES = {"breaker": 1, "capacitor": 2, "verify": 3, "disconnect": 4, "tie": 5}

_sequence_counters = defaultdict(itertools.count)


def _name(bus):
    return BUS_LOCATIONS[bus]["name"]


def get_islanded_buses(faulted_bus, graph=GRID_GRAPH):
    """Buses left with no path to a generation bus once faulted_bus is removed."""
    remaining = graph.copy()
    remaining.remove_node(faulted_bus)
    sources = [b for b in GENERATION_BUSES if b in remaining]
    energized = set()
    for source in sources:
        energized |= nx.node_connected_component(remaining, source)
    return sorted(set(remaining.nodes) - energized)


def get_affected_customers(faulted_bus):
    """Customers at the faulted bus plus any buses cut off from generation by it."""
    faulted_bus = int(faulted_bus)
    buses = [faulted_bus] + get_islanded_buses(faulted_bus)
    return sum(BUS_CUSTOMER_COUNTS[b] for b in buses)

def _live_neighbours(bus, exclude=()):
    """Neighbouring substations that still have a path to generation."""
    dead = set(get_deenergized_buses())
    return [nb for nb in GRID_GRAPH.neighbors(bus) if nb not in dead and nb not in exclude]

def _neighbour_headroom(neighbour, faulted_bus):
    """Spare capacity (0-1) at a neighbour: 1 minus its most loaded remaining branch."""
    loadings = [LINE_STATES[GRID_GRAPH.edges[neighbour, nb]["line_id"]]["current_loading"]
                for nb in GRID_GRAPH.neighbors(neighbour) if nb != faulted_bus]
    return max(0.0, 1.0 - max(loadings, default=1.0))


def _tie_candidates(faulted_bus):
    """Neighbouring substations that serve customers and still have supply, best first."""
    dead = set(get_deenergized_buses())
    candidates = []
    for nb in GRID_GRAPH.neighbors(faulted_bus):
        if BUS_CUSTOMER_COUNTS[nb] == 0 or nb in dead:
            continue
        headroom = _neighbour_headroom(nb, faulted_bus)
        capacity = int(round(TIE_TRANSFER_LIMIT_CUSTOMERS * headroom))
        if capacity > 0:
            candidates.append((nb, capacity, headroom))
    candidates.sort(key=lambda c: (-c[1], c[0]))
    return candidates


def _step(action, switch_id, location, rationale, restored, kind, remote):
    return {"action": action, "switch_id": switch_id, "location_description": location,
            "rationale": rationale, "customers_restored": restored,
            "time_estimate_minutes": STEP_MINUTES[kind], "is_remote_operable": remote}


def _tie_steps(faulted_bus, customers_to_restore, restored, max_ties):
    steps = []
    for nb, capacity, headroom in _tie_candidates(faulted_bus)[:max_ties]:
        remaining = customers_to_restore - restored
        if remaining <= 0:
            break
        transferred = min(capacity, remaining)
        restored += transferred
        steps.append(_step(
            "CLOSE", f"TIE-{faulted_bus}-{nb}",
            f"Normally-open feeder tie between {_name(faulted_bus)} and {_name(nb)} (Bus {nb})",
            f"Transfers {transferred:,} customers onto {_name(nb)}, which has "
            f"{headroom:.0%} spare capacity (tie limit {capacity:,} customers).",
            restored, "tie", remote=False))
    return steps, restored


def _line_fault_steps(bus, customers):
    branches = sorted(GRID_GRAPH.edges(bus, data=True),
                      key=lambda e: (-e[2]["initial_loading"], e[1]))
    _, far, data = branches[0]
    line_id = data["line_id"]
    steps = [
        _step("OPEN", f"CB-{bus}-{far}",
              f"{_name(bus)} substation (Bus {bus}), breaker on {line_id}",
              f"{line_id} is the most heavily loaded branch at Bus {bus} "
              f"({data['initial_loading']:.0%}) and the faulted section. Opening the local end "
              f"stops fault current from {_name(bus)}.", 0, "breaker", True),
        _step("OPEN", f"CB-{far}-{bus}",
              f"{_name(far)} substation (Bus {far}), breaker on {line_id}",
              f"Opens the far end so {line_id} is de-energised from both sides and safe "
              f"for the crew.", 0, "breaker", True),
    ]
    supplies = _live_neighbours(bus, exclude={far})
    if supplies:
        steps.append(_step(
            "CLOSE", f"CB-{bus}F", f"{_name(bus)} substation (Bus {bus}), main feeder breaker",
            f"Bus {bus} is still supplied by {len(supplies)} other energised branch(es), so the "
            f"feeders that tripped on undervoltage can be re-energised.",
            customers, "breaker", True))
        return steps, customers
    tie_steps, restored = _tie_steps(bus, customers, 0, 1)
    return steps + tie_steps, restored


def _transformer_steps(bus, customers):
    steps = [
        _step("OPEN", f"CB-{bus}-XH",
              f"{_name(bus)} substation (Bus {bus}), transformer high-side breaker",
              "Disconnects the failed substation transformer from the transmission bus.",
              0, "breaker", True),
        _step("OPEN", f"CB-{bus}-XL",
              f"{_name(bus)} substation (Bus {bus}), transformer low-side breaker",
              "Disconnects the transformer from the distribution feeders so tie transfers "
              "cannot back-feed it while the crew works on it.", 0, "breaker", True),
    ]
    tie_steps, restored = _tie_steps(bus, customers, 0, MAX_TIES["TRANSFORMER-FAILURE"])
    return steps + tie_steps, restored


def _cascading_steps(bus, customers):
    breakers = ", ".join(f"CB-{bus}-{nb}" for nb in sorted(GRID_GRAPH.neighbors(bus)))
    steps = [
        _step("OPEN", f"CB-{bus}-ALL",
              f"{_name(bus)} substation (Bus {bus}), all line breakers",
              f"Protection has tripped multiple branches. Confirm and lock out every breaker "
              f"at Bus {bus} ({breakers}) so the fault cannot spread further.",
              0, "breaker", True),
        _step("OPEN", f"DS-{bus}-BUS",
              f"{_name(bus)} substation (Bus {bus}), main bus disconnect",
              "Gives the crew a visible open point on the damaged bus before any "
              "restoration switching begins.", 0, "disconnect", False),
    ]
    tie_steps, restored = _tie_steps(bus, customers, 0, MAX_TIES["CASCADING-FAULT"])
    return steps + tie_steps, restored


def _voltage_sag_steps(bus, customers):
    supplied = bool(_live_neighbours(bus))
    restored = int(round(customers * CAPACITOR_VOLTAGE_RECOVERY)) if supplied else 0
    steps = [_step(
        "CLOSE", f"CAP-{bus}A", f"{_name(bus)} substation (Bus {bus}), switched capacitor bank",
        "Injects reactive power to lift the sagging bus voltage back toward 1.0 pu."
        if supplied else "Pre-armed only: every neighbouring substation is already de-energised, "
        "so there is no supply for the capacitor to support.",
        restored, "capacitor", True)]
    tie_steps, restored = _tie_steps(bus, customers, restored, MAX_TIES["VOLTAGE-SAG"])
    return steps + tie_steps, restored


def _cable_fault_steps(bus, customers):
    supplied = bool(_live_neighbours(bus))
    restored = int(round(customers * 0.5)) if supplied else 0
    steps = [
        _step("OPEN", f"SW-{bus}-C1",
              f"{_name(bus)} (Bus {bus}), underground cable sectionalizer upstream of fault",
              "Isolates the faulted cable section from the substation side.",
              0, "disconnect", False),
        _step("CLOSE", f"CB-{bus}F",
              f"{_name(bus)} substation (Bus {bus}), main feeder breaker",
              "Re-energises the healthy cable sections between the substation and the fault."
              if supplied else "Held open: the substation has no energised supply to feed "
              "the healthy cable sections from.",
              restored, "breaker", True),
    ]
    tie_steps, restored = _tie_steps(bus, customers, restored, MAX_TIES["CABLE-FAULT"])
    return steps + tie_steps, restored


_SEQUENCE_BUILDERS = {
    "LINE-FAULT": _line_fault_steps,
    "TRANSFORMER-FAILURE": _transformer_steps,
    "CASCADING-FAULT": _cascading_steps,
    "VOLTAGE-SAG": _voltage_sag_steps,
    "CABLE-FAULT": _cable_fault_steps,
}


def calculate_switching_sequence(faulted_bus, fault_type):
    """Build the full isolation-and-restoration switching plan for a fault.

    This only plans the steps; call trigger_fault_on_bus to change LINE_STATES.
    """
    faulted_bus = int(faulted_bus)
    fault_type = fault_type.strip().upper()
    if faulted_bus not in GRID_GRAPH:
        raise ValueError(f"Bus {faulted_bus} is not in the IEEE 14-bus network")
    if fault_type not in _SEQUENCE_BUILDERS:
        raise ValueError(f"Unknown fault type {fault_type!r}. Use one of: {', '.join(FAULT_TYPES)}")

    total_affected = get_affected_customers(faulted_bus)
    bus_customers = BUS_CUSTOMER_COUNTS[faulted_bus]
    steps, restored = _SEQUENCE_BUILDERS[fault_type](faulted_bus, bus_customers)

    steps.append(_step(
        "VERIFY", f"SCADA-{faulted_bus}", f"Control room SCADA, {_name(faulted_bus)} area",
        f"Confirm the fault is isolated, voltages at restored buses are within 0.95-1.05 pu "
        f"and no neighbouring branch is overloaded. {restored:,} customers back on supply.",
        restored, "verify", True))
    for number, step in enumerate(steps, start=1):
        step["step_number"] = number

    sequence_number = next(_sequence_counters[faulted_bus]) + 1
    return {
        "sequence_id": f"SEQ-BUS{faulted_bus}-{sequence_number:03d}",
        "faulted_bus": faulted_bus,
        "bus_name": _name(faulted_bus),
        "fault_location": get_bus_location(faulted_bus),
        "fault_type": fault_type,
        "islanded_buses": get_islanded_buses(faulted_bus),
        "total_customers_affected": total_affected,
        "steps": steps,
        "estimated_total_time_minutes": sum(s["time_estimate_minutes"] for s in steps),
        "customers_restorable_before_repair": restored,
        "restoration_percentage": round(restored / total_affected * 100, 1) if total_affected else 100.0,
    }


def format_switching_sequence(sequence):
    lines = [
        f"{sequence['sequence_id']}  {sequence['fault_type']} at Bus {sequence['faulted_bus']} "
        f"({sequence['bus_name']})",
        f"Customers affected: {sequence['total_customers_affected']:,}",
    ]
    for s in sequence["steps"]:
        mode = "REMOTE" if s["is_remote_operable"] else "FIELD"
        lines.append(f"  {s['step_number']}. {s['action']:<7}{s['switch_id']:<12}"
                     f"[{mode}, {s['time_estimate_minutes']} min]  "
                     f"restored {s['customers_restored']:,}")
        lines.append(f"       {s['location_description']}")
        lines.append(f"       Why: {s['rationale']}")
    lines.append(
        f"Total {sequence['estimated_total_time_minutes']} min. "
        f"Restorable before repair: {sequence['customers_restorable_before_repair']:,} "
        f"of {sequence['total_customers_affected']:,} ({sequence['restoration_percentage']}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    print("FULL SWITCHING SEQUENCE: TRANSFORMER-FAILURE ON BUS 9")
    print("=" * 72)
    print(format_switching_sequence(calculate_switching_sequence(9, "TRANSFORMER-FAILURE")))

    print("\nALL FAULT TYPES ON BUS 9")
    print("=" * 72)
    for fault in FAULT_TYPES:
        seq = calculate_switching_sequence(9, fault)
        actions = " -> ".join(f"{s['action']} {s['switch_id']}" for s in seq["steps"])
        print(f"{fault:<20}{len(seq['steps'])} steps, {seq['restoration_percentage']:>5}% "
              f"restored in {seq['estimated_total_time_minutes']} min")
        print(f"{'':<20}{actions}")

    print("\nCASCADING-FAULT ON EVERY LOAD BUS")
    print("=" * 72)
    for bus in sorted(BUS_CUSTOMER_COUNTS):
        if BUS_CUSTOMER_COUNTS[bus] == 0:
            continue
        seq = calculate_switching_sequence(bus, "CASCADING-FAULT")
        print(f"Bus {bus:<3}{seq['bus_name']:<18}{seq['total_customers_affected']:>6,} affected, "
              f"{seq['customers_restorable_before_repair']:>6,} restorable "
              f"({seq['restoration_percentage']}%), {len(seq['steps'])} steps")

    print("\nLINE STATE TEST")
    trigger_fault_on_bus(9)
    print(f"  After fault:     Line-9-14 {get_line_status('Line-9-14')['status']}")
    isolate_bus(9)
    print(f"  After isolation: Line-9-14 {get_line_status('Line-9-14')['status']}")
    reset_grid_state()
    print(f"  After reset:     Line-9-14 {get_line_status('Line-9-14')['status']}")

    for seq in (calculate_switching_sequence(b, f) for b in range(1, 15) for f in FAULT_TYPES):
        assert seq["steps"][-1]["action"] == "VERIFY"
        assert 0 <= seq["customers_restorable_before_repair"] <= seq["total_customers_affected"]
        restored = [s["customers_restored"] for s in seq["steps"]]
        assert restored == sorted(restored), "customers_restored must never go down"
    assert get_affected_customers(7) == 0 and get_islanded_buses(7) == [8]
    print("\nAll 70 bus/fault combinations checked. Switching calculator ready.")