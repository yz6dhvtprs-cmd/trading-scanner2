"""Information-free null forecasters for the swing-prediction harness.

The original harness printed `hit_rate` with nothing beside it, so 9 hits out
of 74 read as a weak-but-real edge. It is not: several forecasters that
contain no information at all clear that bar comfortably on the same points.
Scoring against a null is not a nicety here, it is the difference between the
headline being "modest skill" and "worse than doing nothing".

Every null below takes the same inputs the real predictor gets (as-of close,
ATR, side of the last confirmed swing) and returns the same
(direction, level) shape, so they can be scored by the identical code path.
"""
from __future__ import annotations

import math

import numpy as np


def null_predictions(close: np.ndarray, atr: np.ndarray,
                     prev_side: np.ndarray, seed: int = 0) -> dict:
    """Return {name: (direction array, level array)} for each null."""
    rng = np.random.default_rng(seed)
    alt = np.where(prev_side == "high", "down", "up")
    up = np.full(len(close), "up")
    coin = np.where(rng.random(len(close)) < 0.5, "up", "down")

    def band(direction, k):
        return np.where(direction == "up", close + k * atr, close - k * atr)

    return {
        "NULL level=close, dir=alternation": (alt, close.copy()),
        "NULL level=close, dir=always-up": (up, close.copy()),
        "NULL close+/-0.75ATR, dir=alternation": (alt, band(alt, 0.75)),
        "NULL close+/-1.0ATR, dir=alternation": (alt, band(alt, 1.0)),
        "NULL close+/-1.0ATR, dir=coin": (coin, band(coin, 1.0)),
        "NULL close*1.02, dir=always-up": (up, close * 1.02),
    }


def score(direction: np.ndarray, level: np.ndarray, true_side: np.ndarray,
          true_level: np.ndarray, hit_pct: float = 1.0) -> np.ndarray:
    """Boolean hit vector under the harness's own rule."""
    dir_ok = (direction == "up") == (true_side == "high")
    err = np.abs(level - true_level) / true_level * 100
    return dir_ok & (err <= hit_pct)


def mcnemar(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]:
    """Paired test on two hit vectors over the SAME points.

    Returns (a_only, b_only, p). A plain two-proportion z-test would be wrong
    here: both forecasters are scored on identical points, so the samples are
    paired, not independent.
    """
    a_only = int(np.sum(a & ~b))
    b_only = int(np.sum(~a & b))
    n = a_only + b_only
    if n == 0:
        return a_only, b_only, 1.0
    chi = (abs(a_only - b_only) - 1) ** 2 / n     # continuity-corrected
    p = math.erfc(math.sqrt(chi / 2.0))           # chi-square, 1 dof
    return a_only, b_only, p


def compare_table(model_hits: np.ndarray, nulls: dict, true_side: np.ndarray,
                  true_level: np.ndarray, hit_pct: float = 1.0) -> list[dict]:
    rows = []
    n = len(model_hits)
    for name, (d, lv) in nulls.items():
        h = score(d, lv, true_side, true_level, hit_pct)
        mo, no, p = mcnemar(model_hits, h)
        rows.append({"baseline": name, "null_hits": int(h.sum()),
                     "null_rate": round(float(h.mean()), 4),
                     "model_minus_null_pp": round(
                         100 * (model_hits.mean() - h.mean()), 2),
                     "model_only": mo, "null_only": no,
                     "mcnemar_p": round(p, 5),
                     "verdict": ("model worse" if h.mean() > model_hits.mean()
                                 and p < 0.05 else
                                 "model better" if model_hits.mean() > h.mean()
                                 and p < 0.05 else "indistinguishable")})
    rows.sort(key=lambda r: -r["null_rate"])
    _ = n
    return rows
