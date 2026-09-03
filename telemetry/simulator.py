import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import TELEMETRY_PARAMS, ANOMALY_SCENARIOS, RATE_LIMITS
from telemetry.conditioning import ParamLimits, TelemetryConditioner

# Ground segment used for the "same ground-station visibility" clustering test.
# A satellite sees a station when the station lies inside its horizon circle.
GROUND_STATIONS = [
    {"id": "SGP", "name": "Singapore",   "lat": 1.35,    "lon": 103.82},
    {"id": "SVB", "name": "Svalbard",    "lat": 78.23,   "lon": 15.41},
    {"id": "AWA", "name": "Awarua NZ",   "lat": -46.53,  "lon": 168.38},
    {"id": "GLD", "name": "Goldstone",   "lat": 35.43,   "lon": -116.89},
    {"id": "KRU", "name": "Kourou",      "lat": 5.25,    "lon": -52.80},
    {"id": "HYD", "name": "Hyderabad",   "lat": 17.42,   "lon": 78.45},
]

EARTH_R_KM = 6371.0


def _build_limits() -> Dict[str, ParamLimits]:
    """Detection configuration, derived from the mission limit set."""
    return {
        name: ParamLimits(
            warn_low=meta["warn_low"],
            warn_high=meta["warn_high"],
            rate_limit_per_s=RATE_LIMITS.get(name),
            # 3 breaches inside 8 s at a 2 s cadence: a lone bad frame cannot
            # reach it, a real step does within about 6 s.
            confirm_count=3,
            confirm_window_s=8.0,
            clear_count=5,
            clear_window_s=20.0,
            stale_after_s=30.0,
        )
        for name, meta in TELEMETRY_PARAMS.items()
    }

# Missions the fleet can fly. The orbit map maps its constellation groups onto
# these when a satellite is adopted.
MISSIONS = {
    "imaging":    "Earth Imaging",
    "comms":      "Communications",
    "weather":    "Weather",
    "navigation": "Navigation",
    "station":    "Crewed Station",
}

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
    Generates realistic LEO satellite telemetry for one spacecraft.

    Defaults describe LYRA-1, the mission satellite. Passing orbit parameters
    lets the fleet model any object the operator adopts from the orbit map:
    period and altitude come from that object's real TLE, so its eclipse
    cadence and ground track match the orbit it is actually flying.

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

    #: "Past 1 min data logging" — the window every health and detection
    #: decision is averaged over, so one noisy sample cannot raise an anomaly.
    WINDOW_S = 60.0

    def __init__(self, sat_id: str = "LYRA-1", name: str = "LYRA-1",
                 norad_id: Optional[str] = None, altitude_km: Optional[float] = None,
                 inclination_deg: Optional[float] = None,
                 period_s: Optional[float] = None,
                 mission: str = "imaging", raan_deg: Optional[float] = None):
        self.sat_id = sat_id
        self.name = name
        self.norad_id = norad_id
        self.mission = mission if mission in MISSIONS else "imaging"

        # Instance values shadow the class defaults, so every existing
        # `self.ORBITAL_PERIOD_S` reference keeps working unchanged.
        if period_s:
            self.ORBITAL_PERIOD_S = max(600.0, float(period_s))
        if altitude_km:
            self.ALTITUDE_KM = float(altitude_km)
        if inclination_deg is not None:
            self.INCLINATION_DEG = float(inclination_deg)

        # A per-spacecraft RNG, seeded off the id. Without it every satellite
        # in the fleet draws the same noise from the module-level `random` and
        # their telemetry moves in lockstep, which reads as obviously fake.
        self._rng = random.Random(sat_id)

        # Stagger the starting phase too, so adopted satellites are not all
        # entering eclipse on the same tick.
        self._orbit_time: float = self._rng.random() * self.ORBITAL_PERIOD_S
        # Total time since epoch. Distinct from _orbit_time, which wraps.
        self._elapsed_s: float = self._rng.random() * 86400.0
        self._tick: int = 0
        self._history: Dict[str, List] = {p: [] for p in TELEMETRY_PARAMS}
        self._active_anomaly: Optional[Dict] = None

        # Right ascension of the ascending node. Together with inclination this
        # is what actually defines an orbital plane, so it is what clustering
        # groups on — two satellites can share an altitude and an inclination
        # and still fly planes 180 degrees apart.
        self.RAAN_DEG = float(raan_deg) if raan_deg is not None else (self._rng.random() * 360.0)

        # Rolling one-minute window of raw frames: [(sim_time_s, {param: value})].
        # Aged on the simulator's own accumulated clock rather than wall time, so
        # a stalled event loop cannot quietly shorten "the past minute", and the
        # behaviour is reproducible in tests.
        self._window: List = []
        self._sim_time: float = 0.0

        # Detection lives here. The window below is kept for characterising
        # a fault; it is never what decides one.
        self._conditioner = TelemetryConditioner(_build_limits(), window_s=self.WINDOW_S)
        self._frame = None

        # Solid-state recorder fill level. Modelled with state because the
        # original `45 + tick * 0.008` grows without bound: it crosses the 85 %
        # limit at tick 5000 and never comes back, so any satellite left
        # running for ~2.8 hours reports a permanent memory fault.
        self._memory_pct: float = 45.0

    def identity(self) -> Dict:
        return {
            "sat_id": self.sat_id,
            "name": self.name,
            "norad_id": self.norad_id,
            "mission": self.mission,
            "mission_label": MISSIONS[self.mission],
            "altitude_km": round(self.ALTITUDE_KM, 1),
            "inclination_deg": round(self.INCLINATION_DEG, 2),
            "raan_deg": round(self.RAAN_DEG, 2),
            "period_min": round(self.ORBITAL_PERIOD_S / 60.0, 1),
        }

    # ------------------------------------------------------------------ window
    def window_average(self) -> Dict[str, float]:
        """Mean of every parameter over the past minute.

        Detection runs on this rather than the instantaneous frame: a single
        noisy sample is diluted by the other ~29 in the window, so it cannot
        raise an anomaly on its own, while a genuine excursion pulls the mean
        across the threshold within a few seconds.
        """
        if not self._window:
            return {}
        totals: Dict[str, float] = {p: 0.0 for p in TELEMETRY_PARAMS}
        for _, frame in self._window:
            for p, v in frame.items():
                totals[p] += v
        n = len(self._window)
        return {p: round(t / n, 3) for p, t in totals.items()}

    def window_span_s(self) -> float:
        """Seconds of telemetry actually held in the window."""
        if len(self._window) < 2:
            return 0.0
        return round(self._window[-1][0] - self._window[0][0], 1)

    def window_samples(self) -> int:
        return len(self._window)

    def conditioned(self):
        """The latest conditioned frame, or None before the first tick."""
        return self._frame

    def confirmed_violations(self) -> List[Dict]:
        """Faults that have passed persistence. The only escalation trigger."""
        if self._frame is None:
            return []
        return [
            {
                "param": r.name,
                "value": r.value,
                "subsystem": TELEMETRY_PARAMS[r.name]["subsystem"],
                "direction": ("LOW" if TELEMETRY_PARAMS[r.name]["warn_low"] is not None
                              and r.value < TELEMETRY_PARAMS[r.name]["warn_low"] else "HIGH"),
                "threshold": (TELEMETRY_PARAMS[r.name]["warn_low"]
                              if TELEMETRY_PARAMS[r.name]["warn_low"] is not None
                              and r.value < TELEMETRY_PARAMS[r.name]["warn_low"]
                              else TELEMETRY_PARAMS[r.name]["warn_high"]),
                "state": r.state.value,
                "z": round(r.z, 2) if r.z is not None else None,
                "confirmed_via": r.confirmed_via.value if r.confirmed_via else None,
                "trend": r.stats.trend if r.stats else "unknown",
            }
            for r in self._frame.confirmed
        ]

    def evidence(self) -> List[str]:
        """Computed justification for the confirmed faults."""
        return self._frame.evidence() if self._frame else []

    def transient_count(self) -> int:
        """Parameters currently out of limit but not yet persistent."""
        return len(self._frame.suspect) if self._frame else 0

    # ------------------------------------------------------------------ health
    def health_score(self) -> int:
        """0-100, scored on faults that have actually been confirmed.

        Scored against confirmed state rather than the window mean. The mean
        lags a step change by roughly half the window, which had a spacecraft
        reporting 100/NORMAL while it was visibly carrying a fault. Depth is
        measured from the breached limit to the hard limit, not across the
        parameter's full range: a battery 0.9 V under a 24.5 V floor is 35 %
        of the way to the 22.0 V cutout but only 8 % of the 22-33.6 V span,
        and the latter scores a real fault as healthy.
        """
        if self._frame is None:
            return 100
        score = 100.0
        for r in self._frame.confirmed:
            meta = TELEMETRY_PARAMS[r.name]
            lo, hi = meta["warn_low"], meta["warn_high"]
            over = 0.0
            if lo is not None and r.value < lo:
                over = (lo - r.value) / max(lo - meta["min"], 1e-6)
            elif hi is not None and r.value > hi:
                over = (r.value - hi) / max(meta["max"] - hi, 1e-6)
            score -= min(over, 1.0) * 55.0
        return int(max(0.0, min(100.0, score)))

    def health_state(self) -> str:
        score = self.health_score()
        return "NORMAL" if score > 80 else "DEGRADED" if score > 45 else "CRITICAL"

    # -------------------------------------------------------------- visibility
    def visible_stations(self) -> List[str]:
        """Ground stations inside the satellite's horizon circle right now."""
        track = self.ground_track()
        lat1 = math.radians(track["latitude"])
        lon1 = math.radians(track["longitude"])
        # Half-angle from the sub-satellite point to the horizon.
        horizon = math.acos(EARTH_R_KM / (EARTH_R_KM + max(self.ALTITUDE_KM, 1.0)))

        seen = []
        for gs in GROUND_STATIONS:
            lat2 = math.radians(gs["lat"])
            lon2 = math.radians(gs["lon"])
            central = math.acos(max(-1.0, min(1.0,
                math.sin(lat1) * math.sin(lat2) +
                math.cos(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))))
            if central <= horizon:
                seen.append(gs["id"])
        return seen

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
        self._elapsed_s += interval_s
        values = self._generate_values()

        # Store history (50-sample rolling window)
        for p, v in values.items():
            self._history[p].append({"value": v, "ts": datetime.now(timezone.utc).isoformat()})
            if len(self._history[p]) > 50:
                self._history[p].pop(0)

        # Rolling one-minute window, trimmed by age. The count cap is a memory
        # guard for an unexpectedly fast cadence, not the primary rule.
        self._sim_time += interval_s
        self._window.append((self._sim_time, dict(values)))
        cutoff = self._sim_time - self.WINDOW_S
        while self._window and self._window[0][0] < cutoff:
            self._window.pop(0)
        if len(self._window) > 600:
            del self._window[:-600]

        # Detection runs on the raw frame at the spacecraft's own clock.
        self._frame = self._conditioner.ingest(values, self._sim_time)

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

        The rotation term uses total elapsed time, not orbit-relative time.
        `_orbit_time` wraps at every period, so driving Earth rotation from it
        made the track retrace the same path forever instead of precessing
        west each orbit - and a track that never moves can miss every ground
        station for the entire mission, which is exactly what it did.
        """
        inc = math.radians(self.INCLINATION_DEG)
        u = 2 * math.pi * (self._orbit_time / self.ORBITAL_PERIOD_S)  # argument of latitude

        lat = math.asin(math.sin(inc) * math.sin(u))
        # Longitude relative to the ascending node.
        dlon = math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
        lon = math.degrees(dlon) - self.EARTH_ROTATION_DEG_PER_S * self._elapsed_s
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

    def _recorder_fill(self) -> float:
        """Recorder fills while out of contact and drains over a ground pass.

        A real recorder saw-tooths between passes rather than climbing forever,
        and the cap sits below the 85 % warning limit so ordinary operations
        never look like a fault.
        """
        if self.visible_stations():
            self._memory_pct = max(20.0, self._memory_pct - 2.0)   # downlinking
        else:
            self._memory_pct = min(78.0, self._memory_pct + 0.05)  # recording
        return self._memory_pct

    def _generate_values(self) -> Dict[str, float]:
        eclipse = self._in_eclipse()
        phase = 2 * math.pi * self._orbit_time / self.ORBITAL_PERIOD_S
        temp_var = 10 * math.sin(phase)

        base: Dict[str, float] = {
            "battery_voltage_v":  28.0 + (-3.0 if eclipse else 0.8) + self._rng.gauss(0, 0.15),
            "battery_soc_pct":    85.0 + (-15.0 if eclipse else 0.0) + self._rng.gauss(0, 0.4),
            "solar_current_a":    0.05 if eclipse else 6.2 + self._rng.gauss(0, 0.12),
            "bus_power_w":        45.0 + self._rng.gauss(0, 0.8),
            "temp_obc_c":         25.0 + temp_var * 0.4 + self._rng.gauss(0, 0.25),
            "temp_battery_c":     20.0 + temp_var * 0.35 + self._rng.gauss(0, 0.15),
            "temp_payload_c":     22.0 + temp_var * 0.7 + self._rng.gauss(0, 0.3),
            "attitude_error_deg": abs(self._rng.gauss(0.1, 0.04)),
            "reaction_wheel_rpm": 2000.0 + self._rng.gauss(0, 18),
            "downlink_snr_db":    28.0 + self._rng.gauss(0, 0.4),
            "memory_usage_pct":   self._recorder_fill() + self._rng.gauss(0, 0.2),
            "cpu_usage_pct":      30.0 + self._rng.gauss(0, 1.5),
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