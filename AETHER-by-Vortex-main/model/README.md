# LYRA-1 Anomaly Detection & Recovery Trigger — v2

Rebuilt against the current [AETHER-by-Vortex](https://github.com/FlixonCoder/AETHER-by-Vortex)
`config.py`, which now defines **20 telemetry channels** and **10 primary
causal anomaly scenarios** (up from the original 5).

## What changed from v1

- **Telemetry channels: 9 → 20** — now includes `battery_soc_pct`,
  `solar_current_a`/`solar_power_w`, `reaction_wheel_rpm`, `gyro_bias_dps`,
  `rw_current_a`, `bus_power_w`/`bus_voltage_v`, `downlink_snr_db`,
  `rssi_dbm`, `packet_loss_pct`, `gps_fix`, `gps_satellites`, `cpu_usage_pct`,
  matching `TELEMETRY_PARAMS` in the repo exactly.
- **Anomaly classes: 5 → 10** — `battery_undervoltage`, `solar_array_degradation`,
  `rw_saturation`, `gyro_drift`, `battery_overtemperature`, `obc_memory_overflow`,
  `comms_degradation`, `gps_loss`, `power_bus_overcurrent`, `solar_thermal_excursion`.
- Config.py also defines 4 **legacy alias** scenario names (`attitude_drift`,
  `memory_overflow`, `thermal_excursion`, `comms_loss`) that carry *identical*
  deltas to 4 of the 10 primary scenarios above — they're the same physical
  fault under an old label for backward compatibility. Training on both
  copies would ask the classifier to separate two statistically identical
  signals, which is meaningless and only hurts accuracy — so this model
  trains on the 10 primary scenarios and `recovery_protocols.py` maps a
  legacy alias name back to its primary scenario automatically.

## Metrics

Measured on a held-out 25% test split (750 episodes), synthetic data,
CPU-only (no GPU used anywhere in this pipeline):

| Metric | Value |
|---|---|
| **Binary anomaly detection accuracy** (Isolation Forest) | **97.2%** |
| **Binary anomaly detection ROC-AUC** | **0.994** |
| **Anomaly-type classification accuracy** (Random Forest, 10 classes) | **100%**\* |
| **Feature extraction latency** | **~1.4 ms** per telemetry window |
| **Model inference latency** (both stages) | **~40.5 ms** per telemetry window |
| **Total end-to-end latency** | **~42 ms** per telemetry window |
| **Combined model size on disk** | **5.7 MB** |
| **GPU required** | **No — pure CPU, scikit-learn only** |

\* 100% is expected on synthetic scenarios that are cleanly separable by
design (see caveat below) — read it as "the pipeline correctly separates
these 10 well-defined fault signatures," not as a guarantee on messier
real-world telemetry.

### Is it lightweight or heavy?

**Lightweight.** For context on what "~42ms and 5.7MB" means in practice:
- No GPU, no deep learning framework — just scikit-learn tree ensembles.
- 5.7MB total is smaller than a single typical image file; easily bundled
  with a FastAPI service or flashed alongside flight software on
  resource-constrained hardware.
- ~42ms per detection cycle is roughly 1/20th of a single telemetry tick
  at your `TELEMETRY_INTERVAL_S = 2` setting — comfortably real-time, with
  headroom to run detection far more often than your telemetry rate
  requires.
- The 40ms is dominated by the number of trees (300 in the Isolation
  Forest + 400 in the Random Forest) evaluated on a single sample; if you
  need it faster, cutting `n_estimators` to ~100 each would roughly halve
  inference time with only a marginal accuracy cost — not done here since
  42ms is already well within any reasonable real-time budget.

### Caveat on the accuracy numbers

Both models are trained and tested on **synthetic** telemetry generated
from the same deltas defined in `config.py`. That means:
- The pipeline (feature extraction → 2-stage model → recovery lookup)
  is validated end-to-end and works correctly.
- The 97–100% figures reflect how separable *this synthetic data* is —
  they are not a claim about performance on messier real satellite
  telemetry, sensor noise patterns you haven't modeled, or novel faults
  outside these 10 scenarios. Isolation Forest's unsupervised design
  helps here (it can still flag *unknown* anomaly types as anomalous even
  without a matching class), but its type-classification will still say
  "closest known class" rather than "new failure mode" until you add an
  explicit novelty/unknown-class handling step.

## Files

Same structure as v1: `generate_telemetry.py`, `features.py`,
`recovery_protocols.py`, `train.py`, `inference.py`, `models/` (with `_v2`
suffixed artifacts so this doesn't clobber your v1 model if you kept it).

## Quick start

```bash
pip install scikit-learn pandas numpy joblib
python generate_telemetry.py
python features.py
python train.py       # prints accuracy, latency, model size
python inference.py   # smoke test on one fresh episode per class
```

## Integration (unchanged from v1)

```python
from inference import AnomalyDetector

detector = AnomalyDetector()
result = detector.analyze(window)  # window: DataFrame with the 20 CHANNELS as columns

if result["is_anomaly"]:
    trigger_recovery(result["predicted_type"], result["recovery_actions"],
                      severity=result["severity"])
```
