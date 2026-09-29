"""Predictive risk engine for Command (Module C).

Reads the 30 days of 15-minute Atlas telemetry, fits a trend to each bus's
voltage over the last 72 hours and estimates how many days remain before the
voltage leaves its safe band. Buses closest to a breach rank highest.

Run this file directly to print the risk register:  python predictive_risk.py
"""

import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from grid_topology import BUS_LOCATIONS

PROJECT_DIR = Path(__file__).resolve().parent
TELEMETRY_PATHS = (
    PROJECT_DIR / "data" / "atlas_training_data.csv",
    PROJECT_DIR / "atlas_training_data.csv",
)

TREND_WINDOW_HOURS = 72
LOWER_THRESHOLD = 0.95
UPPER_THRESHOLD = 1.05
# Buses held near or above 1.05 pu by a generator (e.g. bus 8 at 1.09 pu in case14)
# are judged against Atlas's red band instead, or they would always look URGENT.
WIDE_LOWER_THRESHOLD = 0.90
WIDE_UPPER_THRESHOLD = 1.10
THRESHOLD_MARGIN = 0.01
ANOMALY_LOW = 0.96
ANOMALY_HIGH = 1.04

# A 72-hour trend only counts if it is statistically significant and large
# enough to matter; otherwise sensor noise would produce random breach dates.
MIN_SLOPE_PU_PER_DAY = 0.001
MAX_P_VALUE = 0.01
NO_BREACH_DAYS = 999.0

RISK_ACTIONS = {
    "URGENT": "IMMEDIATE INSPECTION REQUIRED",
    "HIGH": "SCHEDULE INSPECTION WITHIN 48H",
    "MEDIUM": "MONITOR AND SCHEDULE ROUTINE CHECK",
    "MONITOR": "NO ACTION REQUIRED — CONTINUE MONITORING",
}


def _find_column(columns, patterns):
    for pattern in patterns:
        for col in columns:
            if re.search(pattern, col.lower()):
                return col
    return None


def load_telemetry_data(path=None):
    """Load Atlas telemetry as a DataFrame with timestamp, bus and voltage_pu columns.

    Looks in data/atlas_training_data.csv, then atlas_training_data.csv, unless a
    path is given. Other Atlas columns (power, frequency) are kept as they are.
    """
    candidates = [Path(path)] if path else list(TELEMETRY_PATHS)
    csv_path = next((p for p in candidates if p.exists()), None)
    if csv_path is None:
        raise FileNotFoundError(
            "Atlas telemetry not found. Looked for: "
            + ", ".join(str(p) for p in candidates)
            + ". Complete the Atlas build first: copy generate_telemetry.py into this "
              "repo and run `python generate_telemetry.py`."
        )

    df = pd.read_csv(csv_path)
    time_col = _find_column(df.columns, [r"timestamp", r"time", r"date"])
    bus_col = _find_column(df.columns, [r"^bus$", r"bus_?(id|num|number)", r"bus"])
    volt_col = _find_column(df.columns, [r"volt", r"v_?pu", r"^vm", r"v_mag"])
    missing = [name for name, col in
               (("timestamp", time_col), ("bus", bus_col), ("voltage", volt_col)) if col is None]
    if missing:
        raise ValueError(
            f"{csv_path.name} is missing a {', '.join(missing)} column. "
            f"Columns found: {', '.join(df.columns)}"
        )

    df = df.rename(columns={time_col: "timestamp", bus_col: "bus", volt_col: "voltage_pu"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["bus"] = df["bus"].astype(str).str.extract(r"(\d+)", expand=False).astype(int)
    df["voltage_pu"] = pd.to_numeric(df["voltage_pu"], errors="coerce")
    return df.dropna(subset=["voltage_pu"]).sort_values(["bus", "timestamp"]).reset_index(drop=True)


def simulate_degradation(telemetry, bus_number, pu_per_day, hours=TREND_WINDOW_HOURS):
    """Return a copy of the telemetry with a steady voltage drift added to one bus.

    For demos and testing only: e.g. pu_per_day=-0.012 mimics a failing
    regulator pulling the bus down about 0.036 pu over three days.
    """
    df = telemetry.copy()
    end = df["timestamp"].max()
    start = end - pd.Timedelta(hours=hours)
    mask = (df["bus"] == bus_number) & (df["timestamp"] > start)
    elapsed_days = (df.loc[mask, "timestamp"] - start).dt.total_seconds() / 86400
    df.loc[mask, "voltage_pu"] += pu_per_day * elapsed_days
    return df


def _risk_level(days_to_fault):
    if days_to_fault < 2:
        return "URGENT"
    if days_to_fault < 7:
        return "HIGH"
    if days_to_fault < 14:
        return "MEDIUM"
    return "MONITOR"


def _days_to_breach(start_voltage, slope_per_day, threshold):
    if threshold > start_voltage and slope_per_day > 0:
        return (threshold - start_voltage) / slope_per_day
    if threshold < start_voltage and slope_per_day < 0:
        return (threshold - start_voltage) / slope_per_day
    return NO_BREACH_DAYS


def _operating_band(baseline_mean):
    lower = WIDE_LOWER_THRESHOLD if baseline_mean <= LOWER_THRESHOLD + THRESHOLD_MARGIN else LOWER_THRESHOLD
    upper = WIDE_UPPER_THRESHOLD if baseline_mean >= UPPER_THRESHOLD - THRESHOLD_MARGIN else UPPER_THRESHOLD
    return lower, upper


def _remove_daily_cycle(bus_data, recent, baseline_mean):
    """Voltages in the recent window with the bus's normal time-of-day swing removed.

    Load peaks pull voltage down every afternoon; a straight line fitted through
    those daily swings tilts even when nothing is degrading.
    """
    time_of_day = bus_data["timestamp"].dt.strftime("%H:%M")
    daily_profile = bus_data["voltage_pu"].groupby(time_of_day).mean() - baseline_mean
    offsets = recent["timestamp"].dt.strftime("%H:%M").map(daily_profile).fillna(0.0)
    return recent["voltage_pu"] - offsets


def calculate_bus_risk_profile(telemetry, bus_number):
    """Risk profile for one bus based on its last 72 hours versus its 30-day baseline."""
    bus_number = int(bus_number)
    bus_data = telemetry[telemetry["bus"] == bus_number]
    if bus_data.empty:
        raise ValueError(f"No telemetry for bus {bus_number}")

    window_start = bus_data["timestamp"].max() - pd.Timedelta(hours=TREND_WINDOW_HOURS)
    recent = bus_data[bus_data["timestamp"] > window_start]

    baseline_mean = float(bus_data["voltage_pu"].mean())
    current_mean = float(recent["voltage_pu"].mean())
    current_std = float(recent["voltage_pu"].std())
    lower, upper = _operating_band(baseline_mean)

    elapsed_days = (recent["timestamp"] - recent["timestamp"].min()).dt.total_seconds() / 86400
    fit = stats.linregress(elapsed_days, _remove_daily_cycle(bus_data, recent, baseline_mean))
    slope = float(fit.slope)
    significant = fit.pvalue < MAX_P_VALUE and abs(slope) >= MIN_SLOPE_PU_PER_DAY

    # Project forward from where the trend line ends, not from the 72h mean,
    # which sits 36 hours in the past.
    trend_end_voltage = float(fit.intercept + slope * elapsed_days.max())
    if current_mean > upper or current_mean < lower:
        days_upper = 0.0 if current_mean > upper else NO_BREACH_DAYS
        days_lower = 0.0 if current_mean < lower else NO_BREACH_DAYS
    elif significant:
        days_upper = _days_to_breach(trend_end_voltage, slope, upper)
        days_lower = _days_to_breach(trend_end_voltage, slope, lower)
    else:
        days_upper = days_lower = NO_BREACH_DAYS
    days_to_fault = min(days_upper, days_lower)
    risk_level = _risk_level(days_to_fault)

    if significant and slope > 0:
        trend_direction = "RISING"
    elif significant and slope < 0:
        trend_direction = "DECLINING"
    else:
        trend_direction = "STABLE"

    anomaly_low = ANOMALY_LOW if lower == LOWER_THRESHOLD else lower + THRESHOLD_MARGIN
    anomaly_high = ANOMALY_HIGH if upper == UPPER_THRESHOLD else upper - THRESHOLD_MARGIN
    anomalies = recent[(recent["voltage_pu"] < anomaly_low) | (recent["voltage_pu"] > anomaly_high)]
    last_anomaly = anomalies["timestamp"].max() if not anomalies.empty else None

    return {
        "bus_number": bus_number,
        "bus_name": f"BUS-{bus_number}",
        "substation": BUS_LOCATIONS.get(bus_number, {}).get("name", f"Bus {bus_number}"),
        "current_voltage_mean": round(current_mean, 4),
        "current_voltage_std": round(current_std, 4),
        "trend_end_voltage": round(trend_end_voltage, 4),
        "baseline_voltage_mean": round(baseline_mean, 4),
        "voltage_trend_slope": round(slope, 5),
        "trend_p_value": round(float(fit.pvalue), 4),
        "voltage_deviation_from_baseline": round(abs(current_mean - baseline_mean), 4),
        "upper_threshold": upper,
        "lower_threshold": lower,
        "days_to_upper_breach": round(days_upper, 1),
        "days_to_lower_breach": round(days_lower, 1),
        "days_to_fault": round(days_to_fault, 1),
        "risk_level": risk_level,
        "risk_score": int(np.clip(round(100 - days_to_fault * 5), 0, 100)),
        "trend_direction": trend_direction,
        "last_anomaly_detected": last_anomaly,
        "recommended_action": RISK_ACTIONS[risk_level],
    }


def generate_full_risk_register(telemetry):
    """Risk profiles for every bus in the telemetry, highest risk first."""
    profiles = [calculate_bus_risk_profile(telemetry, bus) for bus in sorted(telemetry["bus"].unique())]
    return sorted(profiles, key=lambda p: (-p["risk_score"], p["days_to_fault"], p["bus_number"]))


def get_7day_forecast(telemetry, bus_number):
    """Projected daily mean voltage and risk level for the next 7 days."""
    profile = calculate_bus_risk_profile(telemetry, bus_number)
    if profile["trend_direction"] == "STABLE":
        start, slope = profile["current_voltage_mean"], 0.0
    else:
        start, slope = profile["trend_end_voltage"], profile["voltage_trend_slope"]
    forecast = []
    for day in range(1, 8):
        remaining = max(0.0, profile["days_to_fault"] - day)
        forecast.append({
            "day": day,
            "projected_voltage_mean": round(start + slope * day, 4),
            "projected_risk_level": _risk_level(remaining),
        })
    return forecast


def get_risk_summary(telemetry):
    register = generate_full_risk_register(telemetry)
    counts = {level: sum(p["risk_level"] == level for p in register) for level in RISK_ACTIONS}
    return {
        "total_buses_monitored": len(register),
        "buses_urgent": counts["URGENT"],
        "buses_high": counts["HIGH"],
        "buses_medium": counts["MEDIUM"],
        "buses_monitor": counts["MONITOR"],
        "highest_risk_bus": register[0]["bus_number"],
        "last_updated": datetime.now().isoformat(timespec="seconds"),
    }


def _print_register(register, limit=None):
    print(f"{'Bus':<5}{'Substation':<27}{'V now':>7}{'Base':>7}{'Slope/day':>11}"
          f"{'Trend':>11}{'Days':>7}{'Score':>6}  Risk")
    for p in register[:limit]:
        days = "  --" if p["days_to_fault"] >= NO_BREACH_DAYS else f"{p['days_to_fault']:>6.1f}"
        print(f"{p['bus_number']:<5}{p['substation']:<27}{p['current_voltage_mean']:>7.3f}"
              f"{p['baseline_voltage_mean']:>7.3f}{p['voltage_trend_slope']:>+11.4f}"
              f"{p['trend_direction']:>11}{days:>7}{p['risk_score']:>6}  {p['risk_level']}")


if __name__ == "__main__":
    telemetry = load_telemetry_data()
    print(f"Loaded {len(telemetry):,} readings for {telemetry['bus'].nunique()} buses, "
          f"{telemetry['timestamp'].min():%Y-%m-%d} to {telemetry['timestamp'].max():%Y-%m-%d}")

    print("\nRISK REGISTER (Atlas telemetry as recorded)")
    print("=" * 84)
    register = generate_full_risk_register(telemetry)
    _print_register(register)
    print(f"\nSummary: {get_risk_summary(telemetry)}")

    print("\nTOP 5 RISK PROFILES")
    print("=" * 84)
    for p in register[:5]:
        print(f"{p['bus_name']} ({p['substation']}): {p['risk_level']}, score {p['risk_score']}, "
              f"{p['trend_direction']}, last anomaly {p['last_anomaly_detected']}")
        print(f"   -> {p['recommended_action']}")

    print("\nDETECTION TEST: simulated failing regulator on bus 12 (-0.012 pu/day)")
    print("=" * 84)
    degraded = simulate_degradation(telemetry, 12, -0.012)
    degraded_register = generate_full_risk_register(degraded)
    _print_register(degraded_register, limit=3)
    bus12 = next(p for p in degraded_register if p["bus_number"] == 12)
    print("\n7-day forecast for bus 12:")
    for day in get_7day_forecast(degraded, 12):
        print(f"   Day {day['day']}: {day['projected_voltage_mean']:.3f} pu  {day['projected_risk_level']}")

    assert bus12["trend_direction"] == "DECLINING" and bus12["risk_level"] != "MONITOR", \
        "Trend detection missed the simulated degradation"
    assert degraded_register[0]["bus_number"] == 12
    print("\nPredictive risk engine ready.")