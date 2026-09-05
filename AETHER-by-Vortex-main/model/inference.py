"""
AETHER v2 anomaly detection inference pipeline - matches the current
AETHER-by-Vortex config.py (20 channels, 10 primary anomaly scenarios).
"""
import json

import joblib
import numpy as np
import pandas as pd

from generate_telemetry import CHANNELS
from features import episode_to_features
from recovery_protocols import get_recovery_protocol

MODEL_DIR = "models"


class AnomalyDetector:
    def __init__(self, model_dir: str = MODEL_DIR):
        self.iso = joblib.load(f"{model_dir}/isolation_forest_v2.joblib")
        self.rf = joblib.load(f"{model_dir}/random_forest_classifier_v2.joblib")
        with open(f"{model_dir}/feature_columns_v2.json") as f:
            self.feature_cols = json.load(f)

    def _vectorize(self, window: pd.DataFrame) -> np.ndarray:
        missing = [c for c in CHANNELS if c not in window.columns]
        if missing:
            raise ValueError(f"Telemetry window missing channels: {missing}")
        feats = episode_to_features(window)
        return np.array([[feats[c] for c in self.feature_cols]])

    def analyze(self, window: pd.DataFrame) -> dict:
        X = self._vectorize(window)
        anomaly_score = float(-self.iso.score_samples(X)[0])
        is_anomaly = bool(self.iso.predict(X)[0] == -1)

        pred_type = "nominal"
        type_confidence = None
        if is_anomaly:
            probs = self.rf.predict_proba(X)[0]
            classes = self.rf.classes_
            top_idx = int(np.argmax(probs))
            pred_type = classes[top_idx]
            type_confidence = float(probs[top_idx])
            if pred_type == "nominal":
                pred_type = "unclassified_anomaly"

        protocol = get_recovery_protocol(pred_type)
        return {
            "is_anomaly": is_anomaly,
            "anomaly_score": round(anomaly_score, 4),
            "predicted_type": pred_type,
            "type_confidence": round(type_confidence, 4) if type_confidence is not None else None,
            "severity": protocol["severity"],
            "recovery_actions": protocol["actions"],
        }


if __name__ == "__main__":
    from generate_telemetry import make_episode, ANOMALY_SCENARIOS
    rng = np.random.default_rng(7)
    detector = AnomalyDetector()
    for label in ["nominal"] + list(ANOMALY_SCENARIOS.keys()):
        ep = make_episode(label, rng)
        result = detector.analyze(ep)
        print(f"\nTrue label: {label}")
        print(json.dumps(result, indent=2))
