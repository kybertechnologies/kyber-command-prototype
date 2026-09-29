"""Field crew database for Command's crew dispatch engine (Module A).

Eight crews based across the Seattle-Tacoma service territory in Washington
State. Run this file directly to print every crew:  python crew_database.py
"""

import copy

CREW_STATUSES = ("AVAILABLE", "DISPATCHED", "ON-SITE", "OFF-DUTY")

# Each specialization matches one fault type the fault simulator can trigger.
SPECIALIZATIONS = (
    "line-fault",
    "transformer-repair",
    "voltage-restoration",
    "cable-splice",
    "substation-work",
)

PART_TYPES = (
    "fuses",
    "insulators",
    "conductors",
    "transformer-bushings",
    "voltage-regulators",
    "recloser-units",
)

VEHICLE_TYPES = ("Bucket Truck", "Line Truck", "Service Van")

_INITIAL_CREWS = {
    "ALPHA-1": {
        "crew_id": "ALPHA-1",
        "name": "Alpha Team 1",
        "base_city": "Seattle",
        "base_location": {"lat": 47.6062, "lon": -122.3321},
        "status": "AVAILABLE",
        "specializations": ["line-fault", "cable-splice"],
        "parts_inventory": {
            "fuses": 24,
            "insulators": 12,
            "conductors": 8,
            "transformer-bushings": 2,
            "voltage-regulators": 0,
            "recloser-units": 1,
        },
        "vehicle_type": "Bucket Truck",
        "active_incident": None,
    },
    "ALPHA-2": {
        "crew_id": "ALPHA-2",
        "name": "Alpha Team 2",
        "base_city": "Tacoma",
        "base_location": {"lat": 47.2529, "lon": -122.4443},
        "status": "AVAILABLE",
        "specializations": ["transformer-repair", "substation-work"],
        "parts_inventory": {
            "fuses": 16,
            "insulators": 6,
            "conductors": 4,
            "transformer-bushings": 6,
            "voltage-regulators": 2,
            "recloser-units": 2,
        },
        "vehicle_type": "Line Truck",
        "active_incident": None,
    },
    "ALPHA-3": {
        "crew_id": "ALPHA-3",
        "name": "Alpha Team 3",
        "base_city": "Bellevue",
        "base_location": {"lat": 47.6101, "lon": -122.2015},
        "status": "AVAILABLE",
        "specializations": ["voltage-restoration", "line-fault"],
        "parts_inventory": {
            "fuses": 20,
            "insulators": 10,
            "conductors": 6,
            "transformer-bushings": 0,
            "voltage-regulators": 3,
            "recloser-units": 1,
        },
        "vehicle_type": "Bucket Truck",
        "active_incident": None,
    },
    "ALPHA-4": {
        "crew_id": "ALPHA-4",
        "name": "Alpha Team 4",
        "base_city": "Renton",
        "base_location": {"lat": 47.4829, "lon": -122.2171},
        "status": "AVAILABLE",
        "specializations": ["cable-splice", "transformer-repair", "line-fault"],
        "parts_inventory": {
            "fuses": 18,
            "insulators": 8,
            "conductors": 10,
            "transformer-bushings": 4,
            "voltage-regulators": 0,
            "recloser-units": 1,
        },
        "vehicle_type": "Line Truck",
        "active_incident": None,
    },
    "ALPHA-5": {
        "crew_id": "ALPHA-5",
        "name": "Alpha Team 5",
        "base_city": "Kent",
        "base_location": {"lat": 47.3809, "lon": -122.2348},
        "status": "AVAILABLE",
        "specializations": ["substation-work", "voltage-restoration"],
        "parts_inventory": {
            "fuses": 10,
            "insulators": 4,
            "conductors": 2,
            "transformer-bushings": 2,
            "voltage-regulators": 4,
            "recloser-units": 3,
        },
        "vehicle_type": "Service Van",
        "active_incident": None,
    },
    "ALPHA-6": {
        "crew_id": "ALPHA-6",
        "name": "Alpha Team 6",
        "base_city": "Redmond",
        "base_location": {"lat": 47.6740, "lon": -122.1215},
        "status": "AVAILABLE",
        "specializations": ["transformer-repair", "voltage-restoration", "cable-splice"],
        "parts_inventory": {
            "fuses": 22,
            "insulators": 8,
            "conductors": 6,
            "transformer-bushings": 5,
            "voltage-regulators": 2,
            "recloser-units": 0,
        },
        "vehicle_type": "Bucket Truck",
        "active_incident": None,
    },
    "ALPHA-7": {
        "crew_id": "ALPHA-7",
        "name": "Alpha Team 7",
        "base_city": "Kirkland",
        "base_location": {"lat": 47.6815, "lon": -122.2087},
        "status": "OFF-DUTY",
        "specializations": ["line-fault", "substation-work"],
        "parts_inventory": {
            "fuses": 20,
            "insulators": 14,
            "conductors": 12,
            "transformer-bushings": 1,
            "voltage-regulators": 1,
            "recloser-units": 2,
        },
        "vehicle_type": "Line Truck",
        "active_incident": None,
    },
    "ALPHA-8": {
        "crew_id": "ALPHA-8",
        "name": "Alpha Team 8",
        "base_city": "Auburn",
        "base_location": {"lat": 47.3073, "lon": -122.2285},
        "status": "OFF-DUTY",
        "specializations": ["cable-splice", "voltage-restoration"],
        "parts_inventory": {
            "fuses": 12,
            "insulators": 4,
            "conductors": 8,
            "transformer-bushings": 0,
            "voltage-regulators": 2,
            "recloser-units": 0,
        },
        "vehicle_type": "Service Van",
        "active_incident": None,
    },
}

CREW_DATABASE = copy.deepcopy(_INITIAL_CREWS)


def create_crew_database():
    """Return a fresh, independent copy of all 8 crews in their starting state.

    The Streamlit dashboard should keep its own copy in st.session_state,
    because CREW_DATABASE is shared by every browser session.
    """
    return copy.deepcopy(_INITIAL_CREWS)


def reset_crew_database():
    """Put CREW_DATABASE back to its starting state and return it."""
    CREW_DATABASE.clear()
    CREW_DATABASE.update(copy.deepcopy(_INITIAL_CREWS))
    return CREW_DATABASE


def get_available_crews(database=None):
    """Return a list of the crews whose status is AVAILABLE."""
    database = CREW_DATABASE if database is None else database
    return [crew for crew in database.values() if crew["status"] == "AVAILABLE"]


def get_crew_by_id(crew_id, database=None):
    """Return the crew dictionary for crew_id, or None if there is no such crew."""
    database = CREW_DATABASE if database is None else database
    return database.get(crew_id.strip().upper())


def update_crew_status(crew_id, new_status, active_incident=None, database=None):
    """Change a crew's status and return the updated crew.

    Setting a crew to AVAILABLE or OFF-DUTY clears its active_incident.
    Raises ValueError for an unknown crew ID or status.
    """
    database = CREW_DATABASE if database is None else database
    crew = get_crew_by_id(crew_id, database)
    if crew is None:
        raise ValueError(f"Unknown crew ID: {crew_id!r}")

    new_status = new_status.strip().upper()
    if new_status not in CREW_STATUSES:
        raise ValueError(
            f"Invalid status {new_status!r}. Use one of: {', '.join(CREW_STATUSES)}"
        )

    crew["status"] = new_status
    if new_status in ("AVAILABLE", "OFF-DUTY"):
        crew["active_incident"] = None
    elif active_incident is not None:
        crew["active_incident"] = active_incident
    return crew


if __name__ == "__main__":
    print("COMMAND CREW DATABASE - Seattle-Tacoma Service Territory")
    print("=" * 72)
    for crew in CREW_DATABASE.values():
        loc = crew["base_location"]
        print(f"{crew['crew_id']:<8} {crew['name']:<13} {crew['status']:<10} "
              f"{crew['base_city']:<9} ({loc['lat']:.4f}, {loc['lon']:.4f})")
        print(f"         Vehicle:         {crew['vehicle_type']}")
        print(f"         Specializations: {', '.join(crew['specializations'])}")
        parts = ", ".join(f"{name} x{qty}" for name, qty in crew["parts_inventory"].items())
        print(f"         Parts:           {parts}")
        print("-" * 72)

    available = get_available_crews()
    print(f"Available crews: {len(available)} of {len(CREW_DATABASE)} "
          f"({', '.join(c['crew_id'] for c in available)})")

    print("\nSpecialization coverage among available crews:")
    for skill in SPECIALIZATIONS:
        crews = [c["crew_id"] for c in available if skill in c["specializations"]]
        print(f"  {skill:<20} {', '.join(crews)}")

    test_db = create_crew_database()
    update_crew_status("ALPHA-1", "DISPATCHED", active_incident="INC-TEST", database=test_db)
    assert test_db["ALPHA-1"]["status"] == "DISPATCHED"
    assert test_db["ALPHA-1"]["active_incident"] == "INC-TEST"
    assert len(get_available_crews(test_db)) == 5
    update_crew_status("ALPHA-1", "AVAILABLE", database=test_db)
    assert test_db["ALPHA-1"]["active_incident"] is None
    assert get_crew_by_id("NOPE-9") is None
    assert CREW_DATABASE["ALPHA-1"]["status"] == "AVAILABLE"
    print("\nFunction checks passed. Crew database ready.")