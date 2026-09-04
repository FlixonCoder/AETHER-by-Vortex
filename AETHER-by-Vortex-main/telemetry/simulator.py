import copy
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import TELEMETRY_PARAMS, ANOMALY_SCENARIOS
from telemetry.spacecraft_state import (
    SpacecraftState,
    OrbitalModel,
    FaultEngine,
    RecoveryEngine,
    SpacecraftPhysics,
    ORBITAL_PERIOD_S,
    ECLIPSE_FRACTION,
)


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
        """Return list of threshold violations against TELEMETRY_PARAMS."""
        viols = []
        for param, meta in TELEMETRY_PARAMS.items():
            if param not in self.values:
                continue
            v = self.values[param]
            if meta["warn_low"] is not None and v < meta["warn_low"]:
                viols.append({
                    "param": param,
                    "value": v,
                    "threshold": meta["warn_low"],
                    "direction": "LOW",
                    "subsystem": meta["subsystem"]
                })
            if meta["warn_high"] is not None and v > meta["warn_high"]:
                viols.append({
                    "param": param,
                    "value": v,
                    "threshold": meta["warn_high"],
                    "direction": "HIGH",
                    "subsystem": meta["subsystem"]
                })

        # Orbital context-aware check: solar generation in sunlit phase
        if not self.in_eclipse:
            if self.values.get("solar_current_a", 6.2) < 3.0:
                viols.append({
                    "param": "solar_current_a",
                    "value": self.values["solar_current_a"],
                    "threshold": 3.0,
                    "direction": "LOW",
                    "subsystem": "EPS"
                })
            if self.values.get("solar_power_w", 173.0) < 80.0:
                viols.append({
                    "param": "solar_power_w",
                    "value": self.values["solar_power_w"],
                    "threshold": 80.0,
                    "direction": "LOW",
                    "subsystem": "EPS"
                })
        return viols


class SatelliteSimulator:
    """
    Causal, physics-informed LEO satellite telemetry engine.
    Uses SpacecraftState as the single source of truth:
      SpacecraftState -> FaultState -> Subsystem Physics -> Telemetry
    Maintains full backward compatibility with the existing AETHER interface.
    """

    ORBITAL_PERIOD_S = ORBITAL_PERIOD_S
    ECLIPSE_FRACTION = ECLIPSE_FRACTION

    INCLINATION_DEG = 51.6
    ALTITUDE_KM = 513.7
    EARTH_ROTATION_DEG_PER_S = 360.0 / 86164.0  # sidereal day

    def __init__(self):
        self._state = SpacecraftState()
        self._orbital = OrbitalModel()
        self._fault_engine = FaultEngine()
        self._recovery_engine = RecoveryEngine()
        self._physics = SpacecraftPhysics()

        self._tick: int = 0
        self._history: Dict[str, List] = {p: [] for p in TELEMETRY_PARAMS}
        self._active_anomaly: Optional[Dict] = None

    @property
    def _orbit_time(self) -> float:
        return self._state.orbit_time_s

    @_orbit_time.setter
    def _orbit_time(self, val: float):
        self._state.orbit_time_s = float(val)

    def inject_anomaly(self, scenario_key: str, duration_ticks: Optional[int] = None) -> Dict:
        """Inject an anomaly into the physical spacecraft state."""
        if scenario_key not in ANOMALY_SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario_key}'")

        scenario = ANOMALY_SCENARIOS[scenario_key]
        duration = duration_ticks if duration_ticks is not None else scenario.get("duration_ticks", 30)

        self._fault_engine.inject(self._state, scenario_key, duration)
        self._active_anomaly = {
            **scenario,
            "key": scenario_key,
            "remaining_ticks": duration,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._active_anomaly

    def clear_anomaly(self) -> None:
        """Clear the active fault; allows natural continuous recovery."""
        self._state.active_fault = None
        self._active_anomaly = None

    def apply_recovery_command(self, cmd_name: str, params: Optional[dict] = None) -> dict:
        """Apply a recovery command to the underlying SpacecraftState."""
        return self._recovery_engine.apply(self._state, cmd_name, params or {})

    def clone_state(self) -> SpacecraftState:
        """Clone internal state for digital-twin forward simulation."""
        return copy.deepcopy(self._state)

    def tick(self, interval_s: float = 2.0) -> TelemetrySnapshot:
        """Advance simulation by interval_s seconds and generate correlated telemetry."""
        self._tick += 1

        # 1. Update orbit and environment
        self._orbital.update(self._state, interval_s)

        # 2. Progress active fault
        self._fault_engine.step(self._state, interval_s)

        # 3. Step internal continuous state (thermal, battery SOC, natural damping)
        self._physics.step_state(self._state, interval_s)

        # 4. Derive telemetry via physics equations
        raw_values = self._physics.compute_telemetry(self._state)

        # 5. Bound and clamp to TELEMETRY_PARAMS limits
        values: Dict[str, float] = {}
        for p, meta in TELEMETRY_PARAMS.items():
            val = raw_values.get(p, meta["nominal"])
            values[p] = round(max(meta["min"], min(meta["max"], val)), 3)

        # 6. Record history (50-sample window)
        now_ts = datetime.now(timezone.utc).isoformat()
        for p, v in values.items():
            if p not in self._history:
                self._history[p] = []
            self._history[p].append({"value": v, "ts": now_ts})
            if len(self._history[p]) > 50:
                self._history[p].pop(0)

        # 7. Maintain active anomaly countdown
        if self._active_anomaly:
            rem = max(0, self._state.fault_duration_ticks - self._state.fault_tick)
            self._active_anomaly["remaining_ticks"] = rem
            if self._state.active_fault is None or rem <= 0:
                self._active_anomaly = None

        return TelemetrySnapshot(
            timestamp=now_ts,
            values=values,
            units={p: m["unit"] for p, m in TELEMETRY_PARAMS.items()},
            subsystems={p: m["subsystem"] for p, m in TELEMETRY_PARAMS.items()},
            orbital_phase=self._state.orbit_time_s / self.ORBITAL_PERIOD_S,
            in_eclipse=self._state.eclipse,
            tick=self._tick,
        )

    def compute_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Derive an instantaneous snapshot without stepping time."""
        raw_values = self._physics.compute_telemetry(self._state)
        values: Dict[str, float] = {}
        for p, meta in TELEMETRY_PARAMS.items():
            val = raw_values.get(p, meta["nominal"])
            values[p] = round(max(meta["min"], min(meta["max"], val)), 3)

        now_ts = datetime.now(timezone.utc).isoformat()
        return TelemetrySnapshot(
            timestamp=now_ts,
            values=values,
            units={p: m["unit"] for p, m in TELEMETRY_PARAMS.items()},
            subsystems={p: m["subsystem"] for p, m in TELEMETRY_PARAMS.items()},
            orbital_phase=self._state.orbit_time_s / self.ORBITAL_PERIOD_S,
            in_eclipse=self._state.eclipse,
            tick=self._tick,
        )

    def get_history(self, param: str, n: int = 20) -> List[Dict]:
        return self._history.get(param, [])[-n:]

    def get_active_anomaly(self) -> Optional[Dict]:
        return self._active_anomaly

    def ground_track(self) -> Dict[str, float]:
        """Sub-satellite point ground track geometry."""
        return self._orbital.ground_track(self._state.orbit_time_s)

    def orbital_context(self) -> Dict:
        """Orbital context dictionary for agents and UI."""
        return {
            "orbit_time_s": round(self._state.orbit_time_s, 1),
            "orbital_phase_pct": round(100.0 * self._state.orbit_time_s / self.ORBITAL_PERIOD_S, 1),
            "in_eclipse": self._in_eclipse(),
            "tick": self._tick,
            **self.ground_track(),
        }

    def _in_eclipse(self) -> bool:
        return self._state.eclipse