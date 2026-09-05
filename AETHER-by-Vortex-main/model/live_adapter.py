"""
Live Telemetry Adapter for the AETHER v2 ML Anomaly Classifier.

Bridges the live AETHER telemetry pipeline (snapshot.values / history dicts)
to the trained two-stage ML model (AnomalyDetector in model/inference.py).

Key design decisions
────────────────────
* Module-level singleton — models are loaded exactly once at import time.
  Any failure (missing .joblib, wrong scikit-learn version, etc.) sets
  _DETECTOR = None and every subsequent call to classify_from_live_telemetry()
  immediately returns None, letting the caller fall through to the existing
  deterministic rule engine without any risk of crashing the pipeline.

* Short history tolerance — the model was trained on 40-sample windows but
  episode_to_features() computes per-channel statistics (mean, std, slope …)
  that are window-length tolerant.  We pad missing / short series by
  back-filling from the current snapshot value so we always have ≥ 2 rows.

* No subsystem from ML — the model classifies anomaly *type*, not subsystem.
  primary_subsystem is always derived from violations[0]["subsystem"] so it
  stays grounded in actual violated telemetry rather than a guessed label.

* Full exception isolation — the entire function body is wrapped in
  try/except; any runtime error returns None, never raises into watcher.py.
"""

import sys
import os
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Resolve paths without relying on the process cwd.
# We import ML_MODEL_DIR from config so the path is always absolute and
# consistent with how every other path constant in the project is defined.
# ---------------------------------------------------------------------------
# Add the project root to sys.path so that 'config' can be imported even when
# this module is first loaded from within the model/ sub-package.
_HERE = Path(__file__).parent.resolve()          # …/model/
_ROOT = _HERE.parent                              # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Also add the model/ directory itself to sys.path so that inference.py's
# relative imports of generate_telemetry and features still resolve correctly
# when AnomalyDetector is instantiated from a different working directory.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import ML_MODEL_DIR from config (absolute path, cwd-independent).
# ---------------------------------------------------------------------------
try:
    from config import ML_MODEL_DIR
    _MODEL_DIR_STR = str(ML_MODEL_DIR)
except Exception as _cfg_err:
    logger.warning("live_adapter: could not import ML_MODEL_DIR from config (%s). "
                   "Falling back to relative model/models path.", _cfg_err)
    _MODEL_DIR_STR = str(_HERE / "models")

# ---------------------------------------------------------------------------
# Known 10 primary anomaly types — used to validate the model's output.
# ---------------------------------------------------------------------------
KNOWN_ANOMALY_TYPES = {
    "battery_undervoltage",
    "solar_array_degradation",
    "rw_saturation",
    "gyro_drift",
    "battery_overtemperature",
    "obc_memory_overflow",
    "comms_degradation",
    "gps_loss",
    "power_bus_overcurrent",
    "solar_thermal_excursion",
}

# Subsystem sanity list — keeps us honest if violations somehow carry garbage.
_KNOWN_SUBSYSTEMS = {"EPS", "ADCS", "COMMS", "THERMAL", "OBC"}

# ---------------------------------------------------------------------------
# Module-level singleton — load once, fail gracefully.
# ---------------------------------------------------------------------------
_DETECTOR = None
_MODEL_LOAD_FAILED = False

try:
    from inference import AnomalyDetector  # model/inference.py
    _DETECTOR = AnomalyDetector(model_dir=_MODEL_DIR_STR)
    logger.info("live_adapter: ML AnomalyDetector loaded from %s", _MODEL_DIR_STR)
except Exception as _load_err:
    _MODEL_LOAD_FAILED = True
    logger.warning(
        "live_adapter: ML model failed to load (%s: %s). "
        "classify_from_live_telemetry will always return None — "
        "the rule-based fallback will be used instead.",
        type(_load_err).__name__,
        _load_err,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_from_live_telemetry(
    snapshot_values: dict,
    history: Dict[str, List[dict]],
    violations: List[dict],
) -> Optional[dict]:
    """
    Classify a live telemetry window using the trained ML anomaly detector.

    Parameters
    ----------
    snapshot_values : dict
        Current telemetry values keyed by parameter name (float values).
        Shape: {"battery_voltage_v": 26.1, "battery_soc_pct": 61.0, …}

    history : dict
        Recent per-parameter sample lists, as returned by
        SatelliteSimulator.get_history().  Each list contains dicts like
        {"value": <float>, "ts": <iso-string>}.  The last ~20 samples are
        passed in by watcher.py; the model was trained on 40-sample windows
        but feature extraction is window-length tolerant — short series are
        padded by back-filling from snapshot_values so the DataFrame always
        has ≥ 2 rows.

    violations : list[dict]
        Active threshold violations from TelemetrySnapshot.violations().
        Each dict has at least: {"param": str, "subsystem": str, …}.

    Returns
    -------
    dict | None
        Classification result conforming to the _rule_fallback() contract:
            anomaly_type       : str
            primary_subsystem  : str   (one of EPS, ADCS, COMMS, THERMAL, OBC)
            severity           : str   ("MEDIUM" placeholder — caller discards)
            affected_params    : list[str]
            trend              : "worsening" | "stable" | "improving"
            confidence         : float (0.0–1.0)
            summary            : str

        Returns None if the model is unavailable or any error occurs,
        so the caller safely falls through to the rule-based engine.
    """
    if _MODEL_LOAD_FAILED or _DETECTOR is None:
        return None

    try:
        # ------------------------------------------------------------------
        # 1. Resolve the 20 model channels from generate_telemetry.CHANNELS.
        # ------------------------------------------------------------------
        from generate_telemetry import CHANNELS  # model/generate_telemetry.py

        # ------------------------------------------------------------------
        # 2. Build a DataFrame: one column per channel, one row per sample.
        #    Short / missing series are padded with the current snapshot value
        #    (back-fill) so episode_to_features() never receives a length-1
        #    series.  We guarantee at least 2 rows.
        # ------------------------------------------------------------------
        MIN_ROWS = 2
        series_data: Dict[str, list] = {}

        for ch in CHANNELS:
            raw_samples = history.get(ch, [])
            values_list = [s["value"] for s in raw_samples if "value" in s]

            # Back-fill with the current snapshot value if needed.
            fallback_val = float(snapshot_values.get(ch, 0.0))
            while len(values_list) < MIN_ROWS:
                values_list.insert(0, fallback_val)

            series_data[ch] = values_list

        # All channels must have the same length; use the shortest.
        n_rows = min(len(v) for v in series_data.values())
        # Trim to uniform length (take the most recent n_rows samples).
        for ch in CHANNELS:
            series_data[ch] = series_data[ch][-n_rows:]

        window_df = pd.DataFrame(series_data)

        # ------------------------------------------------------------------
        # 3. Run the ML classifier.
        # ------------------------------------------------------------------
        result = _DETECTOR.analyze(window_df)

        # ------------------------------------------------------------------
        # 4. Map detector output to the _rule_fallback() return contract.
        # ------------------------------------------------------------------
        raw_type = result.get("predicted_type", "nominal")

        # "nominal" from the ML side means low-confidence classification (this
        # code path only runs when threshold violations already exist, so a
        # "nominal" prediction just means the ML was not confident enough to
        # assign a specific anomaly type).  "unclassified_anomaly" similarly
        # means the isolation forest flagged anomaly but the RF couldn't
        # identify the type.  Both fall back to the generic label.
        if raw_type in ("nominal", "unclassified_anomaly") or raw_type not in KNOWN_ANOMALY_TYPES:
            anomaly_type = "threshold_violation"
        else:
            anomaly_type = raw_type

        # primary_subsystem — always derived from actual violated telemetry.
        subsystem = "OBC"  # safe default
        if violations:
            raw_sub = str(violations[0].get("subsystem", "OBC")).upper()
            subsystem = raw_sub if raw_sub in _KNOWN_SUBSYSTEMS else "OBC"

        # confidence — use model's type_confidence if present.
        raw_conf = result.get("type_confidence")
        confidence = float(raw_conf) if raw_conf is not None else 0.75
        # Clamp to [0.0, 1.0] in case of unexpected model output.
        confidence = max(0.0, min(1.0, confidence))

        # trend heuristic — same as rule engine: ≥2 violations → worsening.
        trend = "worsening" if len(violations) >= 2 else "stable"

        affected_params = [v["param"] for v in violations if "param" in v]

        summary = (
            f"ML classifier detected {anomaly_type} (confidence {confidence:.2f}) "
            f"from {len(violations)} violated parameter(s)"
        )

        return {
            "anomaly_type": anomaly_type,
            "primary_subsystem": subsystem,
            "severity": "MEDIUM",          # placeholder — caller uses crit_eval["severity"]
            "affected_params": affected_params,
            "trend": trend,
            "confidence": confidence,
            "summary": summary,
        }

    except Exception as exc:
        # Never let this raise into watcher.py.
        logger.debug("live_adapter: classify_from_live_telemetry caught exception: %s: %s",
                     type(exc).__name__, exc)
        return None
