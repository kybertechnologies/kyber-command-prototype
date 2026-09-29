"""Incident response manager for Command (Module D).

Tracks every fault through DETECTED -> ISOLATING -> REROUTING -> DISPATCHED ->
REPAIRING -> RESTORED, and ties the other modules together: respond_to_fault()
runs the switching engine and the dispatch optimizer and records each step.

Each incident keeps two clocks: real timestamps, and an operational timeline in
minutes built from the switching step times, crew ETA and repair estimate.
A simulated fault would otherwise "restore" in a few seconds of wall-clock time.

Run this file directly for a full lifecycle test:  python incident_manager.py
"""

import json
import uuid
from datetime import datetime, timezone

import crew_database
from dispatch_optimizer import FAULT_TYPES, optimize_dispatch
from grid_topology import (
    BUS_LOCATIONS,
    GRID_GRAPH,
    get_bus_location,
    isolate_bus,
    reset_grid_state,
    trigger_fault_on_bus,
)
from switching_calculator import calculate_switching_sequence, get_affected_customers

INCIDENT_STATES = ["DETECTED", "ISOLATING", "REROUTING", "DISPATCHED", "REPAIRING", "RESTORED"]

DEFAULT_STATE_NOTES = {
    "DETECTED": "Fault signature detected by Atlas anomaly detection engine",
    "ISOLATING": "Switching engine opening breakers to isolate the faulted section",
    "REROUTING": "Closing tie switches to restore customers through alternate paths",
    "DISPATCHED": "Field crew dispatched to the fault location",
    "REPAIRING": "Crew on site; physical repair under way",
    "RESTORED": "Repair complete; all customers restored and incident closed",
}

# Default bus, severity and repair estimate for each fault simulator button.
_FAULT_DEFAULTS = {
    "LINE-FAULT": {"bus": 9, "severity": "CRITICAL", "repair": 58,
                   "text": "Overhead conductor fault on a line out of {name} substation (Bus {bus})"},
    "TRANSFORMER-FAILURE": {"bus": 4, "severity": "CRITICAL", "repair": 94,
                            "text": "Thermal failure of the substation transformer at {name} (Bus {bus})"},
    "VOLTAGE-SAG": {"bus": 6, "severity": "HIGH", "repair": 34,
                    "text": "Voltage depression at {name} (Bus {bus}) spreading to neighbouring buses"},
    "CASCADING-FAULT": {"bus": 3, "severity": "CRITICAL", "repair": 142,
                        "text": "Cascading relay trips isolating {name} (Bus {bus}) and adjacent branches"},
    "CABLE-FAULT": {"bus": 14, "severity": "HIGH", "repair": 75,
                    "text": "Underground cable failure on a {name} (Bus {bus}) feeder"},
}
_SPREADING_FAULTS = ("VOLTAGE-SAG", "CASCADING-FAULT")


def fault_parameters(fault_type, bus=None):
    """Parameters for a fault type, optionally moved to a different bus."""
    fault_type = fault_type.strip().upper()
    if fault_type not in _FAULT_DEFAULTS:
        raise ValueError(f"Unknown fault type {fault_type!r}. Use one of: {', '.join(FAULT_TYPES)}")
    defaults = _FAULT_DEFAULTS[fault_type]
    bus = int(bus) if bus is not None else defaults["bus"]
    if bus not in GRID_GRAPH:
        raise ValueError(f"Bus {bus} is not in the IEEE 14-bus network")
    return {
        "affected_bus": bus,
        "secondary_buses": sorted(GRID_GRAPH.neighbors(bus)) if fault_type in _SPREADING_FAULTS else [],
        "severity": defaults["severity"],
        "estimated_repair_minutes": defaults["repair"],
        "customers_at_risk": get_affected_customers(bus),
        "fault_description": defaults["text"].format(name=BUS_LOCATIONS[bus]["name"], bus=bus),
    }


FAULT_PARAMETERS = {fault: fault_parameters(fault) for fault in _FAULT_DEFAULTS}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


class Incident:
    def __init__(self, fault_type, bus=None):
        params = fault_parameters(fault_type, bus)
        self.incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
        self.fault_type = fault_type.strip().upper()
        self.affected_bus = params["affected_bus"]
        self.secondary_buses = params["secondary_buses"]
        self.severity = params["severity"]
        self.estimated_repair_minutes = params["estimated_repair_minutes"]
        self.fault_description = params["fault_description"]
        self.total_customers_affected = params["customers_at_risk"]
        self.state = "DETECTED"
        self.created_at = _now()
        self.closed_at = None
        self.state_history = [{
            "state": "DETECTED",
            "timestamp": self.created_at,
            "elapsed_minutes": 0,
            "notes": DEFAULT_STATE_NOTES["DETECTED"],
        }]
        self.dispatch_info = None
        self.switching_sequence = None
        self.customers_restored = 0
        self.resolution_timeline = []
        self.incident_report = None

    @property
    def is_active(self):
        return self.state != "RESTORED"

    @property
    def elapsed_minutes(self):
        return self.state_history[-1]["elapsed_minutes"]

    def _log(self, event, elapsed_minutes=None):
        self.resolution_timeline.append({
            "timestamp": _now(),
            "elapsed_minutes": self.elapsed_minutes if elapsed_minutes is None else elapsed_minutes,
            "event": event,
            "customers_restored": self.customers_restored,
        })

    def advance_state(self, notes=None, elapsed_minutes=None):
        """Move to the next state in INCIDENT_STATES and record it."""
        if self.state == "RESTORED":
            raise ValueError(f"{self.incident_id} is already RESTORED")
        next_state = INCIDENT_STATES[INCIDENT_STATES.index(self.state) + 1]
        elapsed = self.elapsed_minutes if elapsed_minutes is None else max(elapsed_minutes, self.elapsed_minutes)
        self.state = next_state
        self.state_history.append({
            "state": next_state,
            "timestamp": _now(),
            "elapsed_minutes": elapsed,
            "notes": notes or DEFAULT_STATE_NOTES[next_state],
        })
        return next_state

    def attach_dispatch(self, crew_recommendation):
        self.dispatch_info = crew_recommendation
        self._log(f"{crew_recommendation['crew_id']} ({crew_recommendation['crew_name']}) assigned, "
                  f"ETA {crew_recommendation['estimated_arrival_minutes']} min")

    def attach_switching_sequence(self, sequence):
        self.switching_sequence = sequence
        self._log(f"Switching plan {sequence['sequence_id']} attached "
                  f"({len(sequence['steps'])} steps, {sequence['restoration_percentage']}% restorable)")

    def update_restoration_progress(self, customers_restored, note=None, elapsed_minutes=None):
        self.customers_restored = max(0, min(int(customers_restored), self.total_customers_affected))
        self._log(note or f"{self.customers_restored:,} of {self.total_customers_affected:,} customers restored",
                  elapsed_minutes)

    def close_incident(self, notes=None, elapsed_minutes=None):
        """Finish the lifecycle, restore every customer and build the incident report.

        Any states not yet reached are passed through in order, so the history
        always shows the full sequence.
        """
        if self.state == "RESTORED":
            raise ValueError(f"{self.incident_id} is already RESTORED")
        while self.state != "REPAIRING":
            self.advance_state(elapsed_minutes=elapsed_minutes)
        self.customers_restored = self.total_customers_affected
        self.advance_state(notes, elapsed_minutes)
        self._log("Incident closed; all customers restored")
        self.closed_at = _now()

        switched = self.switching_sequence["customers_restorable_before_repair"] if self.switching_sequence else 0
        self.incident_report = {
            "incident_id": self.incident_id,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "affected_bus": self.affected_bus,
            "substation": BUS_LOCATIONS[self.affected_bus]["name"],
            "fault_description": self.fault_description,
            "total_customers_affected": self.total_customers_affected,
            "customers_restored_by_switching": switched,
            "customers_restored_by_repair": self.total_customers_affected - switched,
            "crew_dispatched": self.dispatch_info["crew_id"] if self.dispatch_info else None,
            "operational_duration_minutes": self.elapsed_minutes,
            "total_duration_minutes": round((self.closed_at - self.created_at).total_seconds() / 60, 2),
            "states": [(h["state"], h["elapsed_minutes"]) for h in self.state_history],
            "created_at": _iso(self.created_at),
            "closed_at": _iso(self.closed_at),
        }
        return self.incident_report

    def to_dict(self):
        """Everything about the incident as JSON-safe values."""
        def clean(obj):
            if isinstance(obj, dict):
                return {k: clean(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [clean(v) for v in obj]
            return _iso(obj)

        return clean({
            "incident_id": self.incident_id,
            "fault_type": self.fault_type,
            "state": self.state,
            "severity": self.severity,
            "affected_bus": self.affected_bus,
            "secondary_buses": self.secondary_buses,
            "fault_description": self.fault_description,
            "estimated_repair_minutes": self.estimated_repair_minutes,
            "total_customers_affected": self.total_customers_affected,
            "customers_restored": self.customers_restored,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "state_history": self.state_history,
            "dispatch_info": self.dispatch_info,
            "switching_sequence": self.switching_sequence,
            "resolution_timeline": self.resolution_timeline,
            "incident_report": self.incident_report,
        })


class IncidentRegistry:
    """All active and closed incidents.

    The Streamlit dashboard should keep its own IncidentRegistry() in
    st.session_state; REGISTRY below is shared by every browser session.
    """

    def __init__(self):
        self.incidents = []

    def create_incident(self, fault_type, bus=None):
        incident = Incident(fault_type, bus)
        self.incidents.append(incident)
        return incident

    def get_active_incidents(self):
        return [i for i in self.incidents if i.is_active]

    def get_incident_by_id(self, incident_id):
        return next((i for i in self.incidents if i.incident_id == incident_id), None)

    def get_incident_log(self):
        return [i.to_dict() for i in sorted(self.incidents, key=lambda i: i.created_at, reverse=True)]

    def clear_all(self):
        self.incidents.clear()


REGISTRY = IncidentRegistry()


def respond_to_fault(fault_type, bus=None, registry=None, crews=None):
    """Run the full Command pipeline for one fault and return the incident.

    Detect -> isolate and reroute (switching engine) -> dispatch the best crew
    (dispatch optimizer) -> crew on site. The incident is left in REPAIRING;
    call complete_repair() when the crew finishes.
    """
    registry = REGISTRY if registry is None else registry
    crews = crew_database.CREW_DATABASE if crews is None else crews

    incident = registry.create_incident(fault_type, bus)
    bus = incident.affected_bus
    trigger_fault_on_bus(bus)

    sequence = calculate_switching_sequence(bus, incident.fault_type)
    incident.attach_switching_sequence(sequence)

    clock = 1
    opens = [s for s in sequence["steps"] if s["action"] == "OPEN"]
    incident.advance_state(
        f"Opening {', '.join(s['switch_id'] for s in opens)}" if opens
        else "No isolation needed; correcting voltage in place", clock)
    for step in sequence["steps"]:
        if step["action"] == "OPEN":
            clock += step["time_estimate_minutes"]
    isolate_bus(bus)

    closes = [s for s in sequence["steps"] if s["action"] == "CLOSE"]
    incident.advance_state(
        f"Closing {', '.join(s['switch_id'] for s in closes)}" if closes
        else "No alternate path available; customers wait for repair", clock)
    for step in sequence["steps"]:
        if step["action"] == "OPEN":
            continue
        clock += step["time_estimate_minutes"]
        if step["action"] == "CLOSE":
            incident.update_restoration_progress(
                step["customers_restored"],
                f"{step['action']} {step['switch_id']}: {step['customers_restored']:,} customers restored",
                clock)

    ranking = optimize_dispatch(get_bus_location(bus), incident.fault_type, top_n=3, database=crews)
    if ranking:
        crew = ranking[0]
        crew_database.update_crew_status(crew["crew_id"], "DISPATCHED",
                                         active_incident=incident.incident_id, database=crews)
        incident.attach_dispatch(crew)
        incident.advance_state(
            f"{crew['crew_id']} ({crew['crew_name']}) dispatched from {crew['base_city']}, "
            f"{crew['distance_km']} km, ETA {crew['estimated_arrival_minutes']} min", clock)
        arrival = clock + crew["estimated_arrival_minutes"]
        crew_database.update_crew_status(crew["crew_id"], "ON-SITE", database=crews)
        incident.advance_state(
            f"{crew['crew_id']} on site at {BUS_LOCATIONS[bus]['name']}; "
            f"repair estimated at {incident.estimated_repair_minutes} min", arrival)
    else:
        incident.advance_state("No crew available; incident queued for the next free crew", clock)
    return incident


def complete_repair(incident, crews=None):
    """Close an incident, release its crew, and reset the grid if nothing else is active."""
    crews = crew_database.CREW_DATABASE if crews is None else crews
    finish = incident.elapsed_minutes + incident.estimated_repair_minutes
    report = incident.close_incident(
        f"Repair complete at {BUS_LOCATIONS[incident.affected_bus]['name']}; "
        f"all {incident.total_customers_affected:,} customers restored", finish)
    if incident.dispatch_info:
        crew_database.update_crew_status(incident.dispatch_info["crew_id"], "AVAILABLE", database=crews)
    return report


def reset_all(registry=None, crews=None):
    """The fault simulator's RESET button: clear incidents, crews and grid state."""
    registry = REGISTRY if registry is None else registry
    crews = crew_database.CREW_DATABASE if crews is None else crews
    registry.clear_all()
    crews.clear()
    crews.update(crew_database.create_crew_database())
    reset_grid_state()


if __name__ == "__main__":
    registry = IncidentRegistry()
    crews = crew_database.create_crew_database()

    print("FAULT PARAMETERS")
    print("=" * 72)
    for fault, p in FAULT_PARAMETERS.items():
        print(f"{fault:<20} Bus {p['affected_bus']:<3} {p['severity']:<9} "
              f"{p['customers_at_risk']:>6,} customers  repair {p['estimated_repair_minutes']} min")

    print("\nFULL LIFECYCLE: LINE-FAULT ON BUS 9")
    print("=" * 72)
    incident = respond_to_fault("LINE-FAULT", registry=registry, crews=crews)
    print(f"{incident.incident_id} is {incident.state}; "
          f"crew {incident.dispatch_info['crew_id']} is {crews[incident.dispatch_info['crew_id']]['status']}")
    report = complete_repair(incident, crews=crews)
    for h in incident.state_history:
        print(f"  T+{h['elapsed_minutes']:>3} min  {h['state']:<11} {h['notes']}")
    print("\nINCIDENT REPORT")
    print(json.dumps(report, indent=2))

    states = [h["state"] for h in incident.state_history]
    assert states == INCIDENT_STATES, states
    assert crews[report["crew_dispatched"]]["status"] == "AVAILABLE"
    try:
        incident.advance_state()
        raise AssertionError("advance_state should fail once RESTORED")
    except ValueError:
        pass
    json.dumps(incident.to_dict())

    print("\nALL FAULT TYPES (run at the same time)")
    print("=" * 72)
    reset_all(registry, crews)
    for fault in FAULT_TYPES:
        inc = respond_to_fault(fault, registry=registry, crews=crews)
        crew = inc.dispatch_info["crew_id"] if inc.dispatch_info else "none"
        print(f"{inc.incident_id}  {fault:<20} Bus {inc.affected_bus:<3} {inc.state:<10} "
              f"{inc.customers_restored:>5,}/{inc.total_customers_affected:,} restored by switching, "
              f"crew {crew}")
    print(f"Active incidents: {len(registry.get_active_incidents())}, "
          f"crews still available: {len(crew_database.get_available_crews(crews))}")

    reset_all(registry, crews)
    assert not registry.incidents and len(crew_database.get_available_crews(crews)) == 6
    print("\nReset OK. Incident manager ready.")