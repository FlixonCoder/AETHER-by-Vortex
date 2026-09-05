import json
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
import joblib
import os


def main():
    feat_df = pd.read_csv("data/lyra1_telemetry_features_v2.csv")
    feature_cols = [c for c in feat_df.columns if c not in ("episode_id", "label")]

    X = feat_df[feature_cols].values
    y = feat_df["label"].values
    y_binary = (feat_df["label"] != "nominal").astype(int).values

    X_train, X_test, y_train, y_test, yb_train, yb_test = train_test_split(
        X, y, y_binary, test_size=0.25, random_state=42, stratify=y
    )

    # ---- Stage 1: Isolation Forest, nominal-only training ----
    iso = IsolationForest(n_estimators=300, contamination=0.05, random_state=42)
    iso.fit(X_train[yb_train == 0])

    iso_scores_test = -iso.score_samples(X_test)
    iso_pred_test = (iso.predict(X_test) == -1).astype(int)
    auc = roc_auc_score(yb_test, iso_scores_test)
    binary_report = classification_report(yb_test, iso_pred_test, target_names=["nominal", "anomaly"], output_dict=True)
    binary_accuracy = (iso_pred_test == yb_test).mean()

    print("=== Stage 1: Isolation Forest (binary anomaly detection) ===")
    print(f"ROC-AUC: {auc:.4f}   Accuracy: {binary_accuracy:.4f}")
    print(classification_report(yb_test, iso_pred_test, target_names=["nominal", "anomaly"]))

    # ---- Stage 2: Random Forest, 10-class + nominal ----
    rf = RandomForestClassifier(n_estimators=400, max_depth=12, random_state=42, class_weight="balanced")
    rf.fit(X_train, y_train)
    rf_pred_test = rf.predict(X_test)
    rf_accuracy = (rf_pred_test == y_test).mean()

    print("\n=== Stage 2: Random Forest (10-class anomaly type classification) ===")
    print(f"Accuracy: {rf_accuracy:.4f}")
    print(classification_report(y_test, rf_pred_test))
    labels_order = sorted(feat_df["label"].unique())
    cm = confusion_matrix(y_test, rf_pred_test, labels=labels_order)
    print("Confusion matrix (rows=true, cols=pred):", labels_order)
    print(cm)

    importances = pd.Series(rf.feature_importances_, index=feature_cols)
    top_features = importances.sort_values(ascending=False).head(15)
    print("\nTop 15 most important features:")
    print(top_features)

    # ---- Save models ----
    joblib.dump(iso, "models/isolation_forest_v2.joblib")
    joblib.dump(rf, "models/random_forest_classifier_v2.joblib")
    with open("models/feature_columns_v2.json", "w") as f:
        json.dump(feature_cols, f)

    # ---- Latency benchmark: full inference (feature vector already built) ----
    n_runs = 2000
    sample = X_test[:1]
    # warm up
    for _ in range(20):
        iso.predict(sample)
        rf.predict_proba(sample)
    t0 = time.perf_counter()
    for _ in range(n_runs):
        iso.predict(sample)
        _ = iso.score_samples(sample)
        rf.predict_proba(sample)
    t1 = time.perf_counter()
    per_call_ms = (t1 - t0) / n_runs * 1000

    # also benchmark feature extraction itself (episode -> feature vector)
    from features import episode_to_features
    from generate_telemetry import make_episode
    rng = np.random.default_rng(1)
    ep = make_episode("battery_undervoltage", rng)
    t0 = time.perf_counter()
    for _ in range(n_runs):
        episode_to_features(ep)
    t1 = time.perf_counter()
    feature_extraction_ms = (t1 - t0) / n_runs * 1000

    total_ms = per_call_ms + feature_extraction_ms

    model_size_bytes = (
        os.path.getsize("models/isolation_forest_v2.joblib")
        + os.path.getsize("models/random_forest_classifier_v2.joblib")
    )

    print(f"\n=== Latency & Footprint ===")
    print(f"Feature extraction: {feature_extraction_ms:.4f} ms/window")
    print(f"Model inference (both stages): {per_call_ms:.4f} ms/window")
    print(f"Total end-to-end: {total_ms:.4f} ms/window")
    print(f"Combined model size on disk: {model_size_bytes/1024/1024:.2f} MB")

    metrics = {
        "isolation_forest_roc_auc": float(auc),
        "isolation_forest_binary_accuracy": float(binary_accuracy),
        "isolation_forest_report": binary_report,
        "random_forest_accuracy": float(rf_accuracy),
        "random_forest_report": classification_report(y_test, rf_pred_test, output_dict=True),
        "confusion_matrix_labels": labels_order,
        "confusion_matrix": cm.tolist(),
        "top_features": top_features.to_dict(),
        "latency_ms": {
            "feature_extraction": feature_extraction_ms,
            "model_inference_both_stages": per_call_ms,
            "total_end_to_end": total_ms,
        },
        "model_size_bytes": model_size_bytes,
        "n_channels": 20,
        "n_anomaly_classes": 10,
        "training_episodes": int(feat_df.shape[0]),
    }
    with open("models/metrics_v2.json", "w") as f:
        json.dump(metrics, f, indent=2)


if __name__ == "__main__":
    main()
