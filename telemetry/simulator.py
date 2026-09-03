import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import TELEMETRY_PARAMS, ANOMALY_SCENARIOS

@dataclass
class TelemetrySnapshot:
    timestamp: str
    values: Dict[str, float]
    units: Dict[str, str]
    subsystems: Dict[str, str]
    orbital_phase: float  # 0-1, fraction of orbit completed
    in_eclipse: bool
    tick: int

    def param_dict(self):
        return {
            p: {"value": self.values[p], "unit": self.units[p], "subsystem": self.subsystems[p]}
            for p in self.values
        }

    def violations(self) -> List[Dict]:
        """Return list of threshold violations."""
        viols = []
        for param, meta in TELEMETRY_PARAMS.items():
            v = self.values[param]
            if meta["warn_low"] is not None and v < meta["warn_low"]:
                viols.append({"param": param, "value": v, "threshold": meta["warn_low"], "direction": "LOW", "subsystem": meta["subsystem"]})
            if meta["warn_high"] is not None and v > meta["warn_high"]:
                viols.append({"param": param, "value": v, "threshold": meta["warn_high"], "direction": "HIGH", "subsystem": meta["subsystem"]})
        return viols


class SatelliteSimulator:
    """
    Generates realistic LEO satellite telemetry.
    Orbital period: 5400 s (90 min). Eclipse fraction: 35 %.
    All timestamps come from datetime.now(timezone.utc) - never hardcoded.
    """

    ORBITAL_PERIOD_S = 5400
    ECLIPSE_FRACTION = 0.35

    # Ground-track geometry for the 3D orbit map. A 5400 s period corresponds to
    # a ~513 km circular orbit by Kepler's third law; inclination matches the
    # ISS-like reference orbit the rest of the mission profile assumes.
    INCLINATION_DEG = 51.6
    ALTITUDE_KM = 513.7
    EARTH_ROTATION_DEG_PER_S = 360.0 / 86164.0  # sidereal day

    def __init__(self):
        self._orbit_time: float = 0.0
        self._tick: int = 0
        self._history: Dict[str, List] = {p: [] for p in TELEMETRY_PARAMS}
        self._active_anomaly: Optional[Dict] = None

    def inject_anomaly(self, scenario_key: str):
        if scenario_key not in ANOMALY_SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario_key}'")
        scenario = ANOMALY_SCENARIOS[scenario_key]
        self._active_anomaly = {
            **scenario,
            "key": scenario_key,
            "remaining_ticks": scenario["duration_ticks"],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._active_anomaly

    def clear_anomaly(self):
        self._active_anomaly = None

    def tick(self, interval_s: float = 2.0) -> TelemetrySnapshot:
        self._tick += 1
        self._orbit_time = (self._orbit_time + interval_s) % self.ORBITAL_PERIOD_S
        values = self._generate_values()

        # Store history (50-sample rolling window)
        for p, v in values.items():
            self._history[p].append({"value": v, "ts": datetime.now(timezone.utc).isoformat()})
            if len(self._history[p]) > 50:
                self._history[p].pop(0)

        return TelemetrySnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            values=values,
            units={p: m["unit"] for p, m in TELEMETRY_PARAMS.items()},
            subsystems={p: m["subsystem"] for p, m in TELEMETRY_PARAMS.items()},
            orbital_phase=self._orbit_time / self.ORBITAL_PERIOD_S,
            in_eclipse=self._in_eclipse(),
            tick=self._tick,
        )

    def get_history(self, param: str, n: int = 20) -> List[Dict]:
        return self._history.get(param, [])[-n:]

    def get_active_anomaly(self) -> Optional[Dict]:
        return self._active_anomaly

    def ground_track(self) -> Dict[str, float]:
        """Sub-satellite point for the current orbit time.

        Standard spherical ground-track: the argument of latitude drives
        latitude through the inclination, and longitude is the ascending-node
        offset minus the Earth's rotation beneath the orbit.
        """
        inc = math.radians(self.INCLINATION_DEG)
        u = 2 * math.pi * (self._orbit_time / self.ORBITAL_PERIOD_S)  # argument of latitude

        lat = math.asin(math.sin(inc) * math.sin(u))
        # Longitude relative to the ascending node.
        dlon = math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
        lon = math.degrees(dlon) - self.EARTH_ROTATION_DEG_PER_S * self._orbit_time
        lon = ((lon + 180) % 360) - 180  # wrap to [-180, 180]

        return {
            "latitude": round(math.degrees(lat), 4),
            "longitude": round(lon, 4),
            "altitude_km": self.ALTITUDE_KM,
            "inclination_deg": self.INCLINATION_DEG,
        }

    def orbital_context(self) -> Dict:
        return {
            "orbit_time_s": round(self._orbit_time, 1),
            "orbital_phase_pct": round(100 * self._orbit_time / self.ORBITAL_PERIOD_S, 1),
            "in_eclipse": self._in_eclipse(),
            "tick": self._tick,
            **self.ground_track(),
        }

    def _in_eclipse(self) -> bool:
        eclipse_start = self.ORBITAL_PERIOD_S * (1.0 - self.ECLIPSE_FRACTION)
        return self._orbit_time >= eclipse_start

    def _generate_values(self) -> Dict[str, float]:
        eclipse = self._in_eclipse()
        phase = 2 * math.pi * self._orbit_time / self.ORBITAL_PERIOD_S
        temp_var = 10 * math.sin(phase)

        base: Dict[str, float] = {
            "battery_voltage_v":  28.0 + (-3.0 if eclipse else 0.8) + random.gauss(0, 0.15),
            "battery_soc_pct":    85.0 + (-15.0 if eclipse else 0.0) + random.gauss(0, 0.4),
            "solar_current_a":    0.05 if eclipse else 6.2 + random.gauss(0, 0.12),
            "bus_power_w":        45.0 + random.gauss(0, 0.8),
            "temp_obc_c":         25.0 + temp_var * 0.4 + random.gauss(0, 0.25),
            "temp_battery_c":     20.0 + temp_var * 0.35 + random.gauss(0, 0.15),
            "temp_payload_c":     22.0 + temp_var * 0.7 + random.gauss(0, 0.3),
            "attitude_error_deg": abs(random.gauss(0.1, 0.04)),
            "reaction_wheel_rpm": 2000.0 + random.gauss(0, 18),
            "downlink_snr_db":    28.0 + random.gauss(0, 0.4),
            "memory_usage_pct":   45.0 + self._tick * 0.008 + random.gauss(0, 0.2),
            "cpu_usage_pct":      30.0 + random.gauss(0, 1.5),
        }

        if self._active_anomaly:
            for param, delta in self._active_anomaly["deltas"].items():
                base[param] = base[param] + delta
            self._active_anomaly["remaining_ticks"] -= 1
            if self._active_anomaly["remaining_ticks"] <= 0:
                self._active_anomaly = None

        for p, meta in TELEMETRY_PARAMS.items():
            base[p] = max(meta["min"], min(meta["max"], base[p]))

        return {p: round(base[p], 3) for p in base}