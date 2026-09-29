"""STEP 8 - Session State Integrity Check, automated.

Drives command_app.py through Streamlit's AppTest exactly like an operator
clicking the sidebar, and checks the seven Step 8 items after every click.

Run:  python test_session_state.py
"""

import copy

from streamlit.testing.v1 import AppTest

import crew_database
import grid_topology

FAULTS = ["LINE-FAULT", "TRANSFORMER-FAILURE", "VOLTAGE-SAG", "CASCADING-FAULT", "CABLE-FAULT"]
PRESSES_PER_BUTTON = 10
failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"  FAIL  {message}")


def metric(at, label):
    return next(m.value for m in at.metric if m.label == label)


def health(at):
    return float(metric(at, "Command Health Score"))


def state(at, key):
    return at.session_state[key]


def active_incidents(at):
    return [i for i in state(at, "registry").incidents if i.is_active]


def incident_log_ids(at):
    """Incident IDs in the order the INCIDENT LOG tab renders them."""
    ids, in_log = [], False
    known = {i.incident_id for i in state(at, "registry").incidents}
    for block in at.markdown:
        if "INCIDENT LOG — SESSION HISTORY" in block.value:
            in_log = True
            continue
        if in_log:
            found = [i for i in known if f">{i}<" in block.value]
            if not found:
                break
            ids.append(found[0])
    return ids


def check_invariants(at, where, prev_alerts, prev_health, expected_fault=None):
    """The Step 8 checklist, evaluated after one click."""
    check(not at.exception, f"{where}: app raised {[e.value for e in at.exception]}")
    alerts = state(at, "alert_feed")
    active = active_incidents(at)
    crews = state(at, "crews")

    # 1. Alert feed accumulates and never resets to empty.
    check(len(alerts) >= prev_alerts, f"{where}: alert feed shrank {prev_alerts} -> {len(alerts)}")

    # 2. Crew statuses match the incidents they are working.
    active_ids = {i.incident_id for i in active}
    for crew in crews.values():
        if crew["status"] in ("DISPATCHED", "ON-SITE"):
            check(crew["active_incident"] in active_ids,
                  f"{where}: {crew['crew_id']} is {crew['status']} for a non-active incident")
    for inc in active:
        if inc.dispatch_info:
            crew = crews[inc.dispatch_info["crew_id"]]
            check(crew["status"] != "AVAILABLE" and crew["active_incident"] == inc.incident_id,
                  f"{where}: {crew['crew_id']} reverted to {crew['status']} while assigned to {inc.incident_id}")

    # 3. Customers Restored equals the sum over active incidents.
    expected_restored = f"{sum(i.customers_restored for i in active):,}"
    check(metric(at, "Customers Restored") == expected_restored,
          f"{where}: Customers Restored shows {metric(at, 'Customers Restored')}, expected {expected_restored}")

    # 4. Switching sequence panel shows the focused incident's own steps.
    focus = state(at, "registry").get_incident_by_id(state(at, "focus_incident_id"))
    if expected_fault is not None:
        check(focus is not None and focus.fault_type == expected_fault,
              f"{where}: focused incident is {focus and focus.fault_type}, expected {expected_fault}")
    if focus is not None and focus.is_active:
        panel = next((m.value for m in at.markdown if "ACTIVE FAULT // RECOMMENDED RESPONSE" in m.value), "")
        for step in focus.switching_sequence["steps"]:
            check(step["switch_id"] in panel, f"{where}: switching panel missing {step['switch_id']}")

    # 5. Health score only moves down while faults are being added.
    if prev_health is not None:
        check(health(at) <= prev_health, f"{where}: health score rose {prev_health} -> {health(at)}")

    # 6. Incident log lists every incident, newest first.
    log_ids = incident_log_ids(at)
    expected_order = [i.incident_id for i in sorted(state(at, "registry").incidents,
                                                    key=lambda i: i.created_at, reverse=True)]
    check(log_ids == expected_order, f"{where}: incident log order {log_ids} != {expected_order}")

    # The shared grid model matches this session's incidents.
    check(set(grid_topology.FAULTED_BUSES) == {i.affected_bus for i in active},
          f"{where}: grid faults {sorted(grid_topology.FAULTED_BUSES)} != incident buses")
    return len(alerts), health(at)


def main():
    at = AppTest.from_file("command_app.py", default_timeout=120).run()
    check(not at.exception, "initial load raised an exception")
    baseline_health = health(at)
    baseline_lines = copy.deepcopy(grid_topology.LINE_STATES)
    prev_alerts, prev_health = 0, baseline_health
    print(f"Initial load: health {baseline_health}, 0 incidents, 0 alerts")

    print(f"\n[1] Each fault button pressed {PRESSES_PER_BUTTON} times (default bus)")
    at.selectbox(key="fault_location").set_value("default").run()
    for fault in FAULTS:
        before = len(state(at, "registry").incidents)
        for press in range(1, PRESSES_PER_BUTTON + 1):
            at.button(key=f"trigger_{fault}").click().run()
            prev_alerts, prev_health = check_invariants(
                at, f"{fault} press {press}", prev_alerts, prev_health,
                expected_fault=fault if press == 1 else None)
        created = len(state(at, "registry").incidents) - before
        check(created == 1, f"{fault}: {created} incidents from {PRESSES_PER_BUTTON} presses, expected 1")
        print(f"  {fault:<20} 10 presses -> {created} incident, 9 duplicate presses blocked · "
              f"health {prev_health} · alerts {prev_alerts}")

    print("\n[2] Ten more faults on different buses via the Fault location picker")
    used = {i.affected_bus for i in active_incidents(at)}
    free_buses = [b for b in range(1, 15) if b not in used]
    for n, bus in enumerate(free_buses[:10]):
        fault = FAULTS[n % len(FAULTS)]
        at.selectbox(key="fault_location").set_value(str(bus)).run()
        at.button(key=f"trigger_{fault}").click().run()
        prev_alerts, prev_health = check_invariants(at, f"{fault} on bus {bus}", prev_alerts, prev_health,
                                                    expected_fault=fault)
        inc = active_incidents(at)[-1]
        crew = inc.dispatch_info["crew_id"] if inc.dispatch_info else "NO CREW"
        print(f"  {inc.incident_id} {fault:<20} bus {bus:<3} crew {crew:<9} "
              f"health {prev_health:5.1f} · restored {metric(at, 'Customers Restored'):>6} · alerts {prev_alerts}")
    total = len(state(at, "registry").incidents)
    deployed = sum(1 for c in state(at, "crews").values() if c["status"] != "AVAILABLE"
                   and c["active_incident"])
    print(f"  -> {total} incidents in log, {deployed} crews committed, "
          f"{metric(at, 'Customers Without Power')} customers without power")

    print("\n[3] Complete repair on the focused incident")
    focus_id = state(at, "focus_incident_id")
    focus = state(at, "registry").get_incident_by_id(focus_id)
    crew_id = focus.dispatch_info["crew_id"] if focus.dispatch_info else None
    at.button(key="sidebar_close").click().run()
    check_invariants(at, "after repair", prev_alerts, None)
    check(not focus.is_active, f"{focus_id} still active after repair")
    if crew_id:
        check(state(at, "crews")[crew_id]["status"] == "AVAILABLE", f"{crew_id} not released after repair")
    print(f"  {focus_id} RESTORED, crew {crew_id} back to AVAILABLE, "
          f"{len(active_incidents(at))} incidents still active")

    print("\n[4] Reset Grid - one press")
    alerts_before_reset = len(state(at, "alert_feed"))
    at.button[[b.label for b in at.button].index("Reset Grid")].click().run()
    fresh = crew_database.create_crew_database()
    crews = state(at, "crews")
    check(not at.exception, "reset raised an exception")
    check(len(active_incidents(at)) == 0, "active incidents remain after reset")
    check(len(state(at, "registry").incidents) == 0, "incident log not cleared by reset")
    check(metric(at, "Active Incidents") == "0", "Active Incidents metric not 0")
    check(metric(at, "Crews Deployed") == f"0 / {len(fresh)}", f"Crews Deployed shows {metric(at, 'Crews Deployed')}")
    check(metric(at, "Customers Restored") == "0", "Customers Restored not 0")
    check(metric(at, "Customers Without Power") == "0", "Customers Without Power not 0")
    check(health(at) == baseline_health, f"health {health(at)} != initial {baseline_health}")
    check(crews == fresh, "crew database differs from a fresh one (status, assignment or inventory)")
    check(grid_topology.LINE_STATES == baseline_lines, "line states/loadings differ from base case")
    check(not grid_topology.FAULTED_BUSES and not grid_topology.get_deenergized_buses(), "grid still faulted")
    check(state(at, "focus_incident_id") is None, "focused incident not cleared")
    check(state(at, "alert_feed")[0]["title"].startswith("GRID RESTORED"), "no GRID RESTORED alert on top")
    check(len(state(at, "alert_feed")) == alerts_before_reset + 1, "alert history lost on reset")
    print(f"  health {health(at)} · 0 incidents · 0/{len(fresh)} crews deployed · all "
          f"{len(baseline_lines)} lines IN-SERVICE at base loading · GRID RESTORED alert on top")

    print("\n[5] Page refresh / second browser tab")
    at.selectbox(key="fault_location").set_value("default").run()
    at.button(key="trigger_LINE-FAULT").click().run()
    other_tab = AppTest.from_file("command_app.py", default_timeout=120).run()
    check(metric(other_tab, "Active Incidents") == "0", "a fresh tab inherited another session's incidents")
    check(health(other_tab) == baseline_health, "a fresh tab shows another session's grid damage")
    at.run()
    check_invariants(at, "original tab after the other tab loaded", 0, None)
    check(set(grid_topology.FAULTED_BUSES) == {9}, "original tab's grid fault was not restored")
    print("  a refreshed page starts clean; the original tab keeps its own incident and grid state")

    print("\n" + "=" * 72)
    if failures:
        print(f"SESSION STATE INTEGRITY CHECK FAILED - {len(failures)} problem(s)")
        raise SystemExit(1)
    print("SESSION STATE INTEGRITY CHECK PASSED - all 7 Step 8 items verified")


if __name__ == "__main__":
    main()