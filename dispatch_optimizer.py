"""Crew dispatch optimization engine for Command (Module A).

Ranks every AVAILABLE crew for a fault using a weighted, explainable score:

    composite = 0.40 * distance_score
              + 0.30 * specialization_score
              + 0.30 * equipment_score

Run this file directly to see a test dispatch:  python dispatch_optimizer.py
"""

import math

import crew_database

FAULT_TYPES = (
    "LINE-FAULT",
    "TRANSFORMER-FAILURE",
    "VOLTAGE-SAG",
    "CASCADING-FAULT",
    "CABLE-FAULT",
)

# Parts (and quantity) a crew should carry to fully handle each fault type.
FAULT_EQUIPMENT_REQUIREMENTS = {
    "LINE-FAULT": {"conductors": 6, "insulators": 8, "fuses": 12},
    "TRANSFORMER-FAILURE": {"transformer-bushings": 4, "voltage-regulators": 1},
    "VOLTAGE-SAG": {"voltage-regulators": 3, "fuses": 8},
    "CASCADING-FAULT": {"conductors": 6, "insulators": 8, "fuses": 12, "recloser-units": 2},
    "CABLE-FAULT": {"conductors": 8, "fuses": 6},
}

# Crew specializations (from crew_database.SPECIALIZATIONS) each fault type needs.
FAULT_REQUIRED_SPECIALIZATIONS = {
    "LINE-FAULT": ["line-fault"],
    "TRANSFORMER-FAILURE": ["transformer-repair"],
    "VOLTAGE-SAG": ["voltage-restoration"],
    "CASCADING-FAULT": ["line-fault", "substation-work"],
    "CABLE-FAULT": ["cable-splice"],
}

WEIGHT_DISTANCE = 0.40
WEIGHT_SPECIALIZATION = 0.30
WEIGHT_EQUIPMENT = 0.30

MAX_RADIUS_KM = 80.0
AVERAGE_SPEED_KM_PER_MIN = 0.8  # 48 km/h, straight-line

EARTH_RADIUS_KM = 6371.0


def _check_fault_type(fault_type):
    fault_type = fault_type.strip().upper()
    if fault_type not in FAULT_EQUIPMENT_REQUIREMENTS:
        raise ValueError(
            f"Unknown fault type {fault_type!r}. Use one of: {', '.join(FAULT_TYPES)}"
        )
    return fault_type


def calculate_distance_km(point_a, point_b):
    """Straight-line (Haversine) distance in km between two {'lat', 'lon'} dicts."""
    lat1, lon1 = math.radians(point_a["lat"]), math.radians(point_a["lon"])
    lat2, lon2 = math.radians(point_b["lat"]), math.radians(point_b["lon"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def calculate_distance_score(distance_km):
    """1.0 at the fault location, falling to 0.0 at MAX_RADIUS_KM or beyond."""
    return max(0.0, 1.0 - distance_km / MAX_RADIUS_KM)


def calculate_equipment_score(crew, fault_type):
    """0.0-1.0: how fully the crew's stock covers the parts this fault needs.

    Each required part counts equally; carrying less than the required quantity
    earns partial credit for that part.
    """
    required = FAULT_EQUIPMENT_REQUIREMENTS[_check_fault_type(fault_type)]
    inventory = crew["parts_inventory"]
    coverage = [min(inventory.get(part, 0) / qty, 1.0) for part, qty in required.items()]
    return sum(coverage) / len(coverage)


def calculate_specialization_score(crew, fault_type):
    """0.0-1.0: share of the fault's required specializations the crew has."""
    required = FAULT_REQUIRED_SPECIALIZATIONS[_check_fault_type(fault_type)]
    matched = sum(1 for skill in required if skill in crew["specializations"])
    return matched / len(required)


def optimize_dispatch(fault_location, fault_type, top_n=3, database=None):
    """Return the top_n AVAILABLE crews for a fault, best first.

    Crews that are DISPATCHED, ON-SITE or OFF-DUTY are never recommended.
    Returns an empty list when no crew is available.
    """
    fault_type = _check_fault_type(fault_type)
    database = crew_database.CREW_DATABASE if database is None else database

    results = []
    for crew in crew_database.get_available_crews(database):
        distance_km = calculate_distance_km(crew["base_location"], fault_location)
        distance_score = calculate_distance_score(distance_km)
        specialization_score = calculate_specialization_score(crew, fault_type)
        equipment_score = calculate_equipment_score(crew, fault_type)
        composite = (
            WEIGHT_DISTANCE * distance_score
            + WEIGHT_SPECIALIZATION * specialization_score
            + WEIGHT_EQUIPMENT * equipment_score
        )
        results.append({
            "crew_id": crew["crew_id"],
            "crew_name": crew["name"],
            "base_city": crew["base_city"],
            "fault_type": fault_type,
            "distance_km": round(distance_km, 1),
            "composite_score": round(composite, 3),
            "distance_score": round(distance_score, 3),
            "specialization_score": round(specialization_score, 3),
            "equipment_score": round(equipment_score, 3),
            "estimated_arrival_minutes": round(distance_km / AVERAGE_SPEED_KM_PER_MIN),
            "recommended": False,
        })

    # Crew ID breaks ties so the ranking is always the same for the same input.
    results.sort(key=lambda r: (-r["composite_score"], r["crew_id"]))
    results = results[:top_n]
    if results:
        results[0]["recommended"] = True
    return results


def format_dispatch_recommendation(ranked_crews):
    """One-paragraph summary of the top recommendation from optimize_dispatch."""
    if not ranked_crews:
        return "NO CREW AVAILABLE: every crew is dispatched, on-site or off-duty."

    top = ranked_crews[0]
    summary = (
        f"DISPATCH {top['crew_id']} ({top['crew_name']}, {top['base_city']}) "
        f"to {top['fault_type']}: {top['distance_km']} km away, "
        f"ETA ~{top['estimated_arrival_minutes']} min. "
        f"Score {top['composite_score']:.3f} "
        f"(distance {top['distance_score']:.2f}, "
        f"skills {top['specialization_score']:.2f}, "
        f"equipment {top['equipment_score']:.2f})."
    )
    if len(ranked_crews) > 1:
        backup = ranked_crews[1]
        summary += (
            f" Backup: {backup['crew_id']} "
            f"(score {backup['composite_score']:.3f}, ETA ~{backup['estimated_arrival_minutes']} min)."
        )
    return summary


def _print_ranking(ranked_crews):
    print(f"{'Rank':<5}{'Crew':<9}{'City':<10}{'km':>6}{'ETA':>6}"
          f"{'Dist':>7}{'Skill':>7}{'Equip':>7}{'Score':>8}")
    for rank, r in enumerate(ranked_crews, start=1):
        flag = "  <- RECOMMENDED" if r["recommended"] else ""
        print(f"{rank:<5}{r['crew_id']:<9}{r['base_city']:<10}{r['distance_km']:>6}"
              f"{r['estimated_arrival_minutes']:>5}m"
              f"{r['distance_score']:>7.2f}{r['specialization_score']:>7.2f}"
              f"{r['equipment_score']:>7.2f}{r['composite_score']:>8.3f}{flag}")


if __name__ == "__main__":
    phoenix_substation = {"lat": 47.5, "lon": -122.3}

    print("TEST: LINE-FAULT at Phoenix substation (47.5, -122.3)")
    print("=" * 72)
    ranking = optimize_dispatch(phoenix_substation, "LINE-FAULT", top_n=8)
    _print_ranking(ranking)
    print()
    print(format_dispatch_recommendation(ranking))

    print("\nTOP 3 FOR EACH FAULT TYPE AT THE SAME LOCATION")
    print("=" * 72)
    top_crews = set()
    for fault in FAULT_TYPES:
        ranking = optimize_dispatch(phoenix_substation, fault)
        top_crews.add(ranking[0]["crew_id"])
        print(f"{fault:<20} " + "  ".join(
            f"{r['crew_id']} ({r['composite_score']:.3f})" for r in ranking))

    assert len(top_crews) > 1, "Every fault type picked the same crew"
    assert all(r["crew_id"] not in ("ALPHA-7", "ALPHA-8")
               for f in FAULT_TYPES
               for r in optimize_dispatch(phoenix_substation, f, top_n=8)), \
        "An off-duty crew was ranked"
    print(f"\nDifferent fault types recommend different crews: {', '.join(sorted(top_crews))}")
    print("Dispatch optimizer ready.")