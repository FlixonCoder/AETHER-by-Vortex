import numpy as np
import pandas as pd

from generate_telemetry import CHANNELS


def _channel_features(x: np.ndarray, prefix: str) -> dict:
    x = np.asarray(x, dtype=float)
    t = np.arange(len(x))
    diffs = np.diff(x)
    slope = np.polyfit(t, x, 1)[0] if len(x) > 1 else 0.0
    return {
        f"{prefix}_mean": x.mean(),
        f"{prefix}_std": x.std(),
        f"{prefix}_min": x.min(),
        f"{prefix}_max": x.max(),
        f"{prefix}_range": x.max() - x.min(),
        f"{prefix}_net_drift": x[-1] - x[0],
        f"{prefix}_max_jerk": np.abs(diffs).max() if len(diffs) else 0.0,
        f"{prefix}_slope": slope,
    }


def episode_to_features(ep_df: pd.DataFrame) -> dict:
    feats = {}
    for ch in CHANNELS:
        feats.update(_channel_features(ep_df[ch].values, ch))
    return feats


def build_feature_table(raw_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for episode_id, ep in raw_df.groupby("episode_id"):
        ep = ep.sort_values("t")
        feats = episode_to_features(ep)
        feats["episode_id"] = episode_id
        feats["label"] = ep["label"].iloc[0]
        rows.append(feats)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    raw = pd.read_csv("data/lyra1_telemetry_raw_v2.csv")
    feat_df = build_feature_table(raw)
    feat_df.to_csv("data/lyra1_telemetry_features_v2.csv", index=False)
    print(f"Feature table: {feat_df.shape[0]} episodes x {feat_df.shape[1]} columns")
