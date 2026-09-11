"""Small numpy learners. No scikit-learn on this box and pip cannot reach
PyPI, so everything is written out longhand.

Two model families, both returning calibrated probabilities:
  LogitL2  - ridge-penalised logistic regression, fit by Newton/IRLS.
  StumpGBM - gradient boosting on depth-1 trees (decision stumps), logistic
             loss. Captures the threshold effects (ADX>20, RSI zones) that a
             linear model cannot.

Both standardise inputs using TRAIN statistics only; the scaler is carried on
the fitted object so test data can never leak its mean/std back into the fit.
"""
from __future__ import annotations

import numpy as np


def _sigmoid(z: np.ndarray) -> np.ndarray:
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    e = np.exp(z[~pos])
    out[~pos] = e / (1.0 + e)
    return out


class Scaler:
    def fit(self, x: np.ndarray) -> "Scaler":
        self.mu = np.nanmean(x, axis=0)
        sd = np.nanstd(x, axis=0)
        self.sd = np.where(sd < 1e-12, 1.0, sd)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mu) / self.sd
        return np.clip(np.nan_to_num(z, nan=0.0), -8, 8)


class LogitL2:
    """Ridge logistic regression via iteratively reweighted least squares."""

    def __init__(self, lam: float = 1.0, iters: int = 40):
        self.lam, self.iters = lam, iters

    def fit(self, x: np.ndarray, y: np.ndarray,
            w: np.ndarray | None = None) -> "LogitL2":
        self.scaler = Scaler().fit(x)
        z = self.scaler.transform(x)
        z = np.hstack([np.ones((len(z), 1)), z])
        n, d = z.shape
        w = np.ones(n) if w is None else w / w.mean()
        beta = np.zeros(d)
        pen = self.lam * np.eye(d)
        pen[0, 0] = 0.0                     # never penalise the intercept
        for _ in range(self.iters):
            p = _sigmoid(z @ beta)
            s = np.clip(p * (1 - p), 1e-6, None) * w
            grad = z.T @ ((y - p) * w) - pen @ beta
            hess = (z * s[:, None]).T @ z + pen
            try:
                step = np.linalg.solve(hess, grad)
            except np.linalg.LinAlgError:
                break
            beta += step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.beta = beta
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        z = self.scaler.transform(x)
        return _sigmoid(np.hstack([np.ones((len(z), 1)), z]) @ self.beta)


class StumpGBM:
    """Gradient boosting on decision stumps, logistic loss, shrinkage.

    Splits are searched on quantile bins so cost is O(bins * features) per
    round rather than O(n log n) per feature.
    """

    def __init__(self, rounds: int = 150, lr: float = 0.06, bins: int = 24,
                 min_leaf: int = 200, subsample: float = 0.7, seed: int = 0):
        self.rounds, self.lr, self.bins = rounds, lr, bins
        self.min_leaf, self.subsample, self.seed = min_leaf, subsample, seed

    def fit(self, x: np.ndarray, y: np.ndarray,
            w: np.ndarray | None = None) -> "StumpGBM":
        rng = np.random.default_rng(self.seed)
        n, d = x.shape
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        w = np.ones(n) if w is None else w / w.mean()
        # fixed quantile cut points from TRAIN only
        self.cuts = [np.unique(np.nanquantile(
            x[:, j], np.linspace(0.05, 0.95, self.bins))) for j in range(d)]
        p0 = np.clip(np.average(y, weights=w), 1e-4, 1 - 1e-4)
        self.base = float(np.log(p0 / (1 - p0)))
        fx = np.full(n, self.base)
        self.trees: list[tuple[int, float, float, float]] = []
        for _ in range(self.rounds):
            p = _sigmoid(fx)
            g = (y - p) * w                                   # gradient
            hcurv = np.clip(p * (1 - p), 1e-6, None) * w      # hessian
            mask = rng.random(n) < self.subsample
            best = None
            for j in range(d):
                cuts = self.cuts[j]
                if len(cuts) == 0:
                    continue
                col = x[mask, j]
                gm, hm = g[mask], hcurv[mask]
                # bucket by cut index, then take prefix sums = both sides
                idx = np.searchsorted(cuts, col)
                gs = np.bincount(idx, weights=gm, minlength=len(cuts) + 1)
                hs = np.bincount(idx, weights=hm, minlength=len(cuts) + 1)
                cnt = np.bincount(idx, minlength=len(cuts) + 1)
                gl, hl, nl = np.cumsum(gs)[:-1], np.cumsum(hs)[:-1], \
                    np.cumsum(cnt)[:-1]
                gr, hr = gs.sum() - gl, hs.sum() - hl
                nr = cnt.sum() - nl
                valid = (nl >= self.min_leaf) & (nr >= self.min_leaf)
                if not valid.any():
                    continue
                gain = np.where(valid, gl ** 2 / (hl + 1.0)
                                + gr ** 2 / (hr + 1.0), -np.inf)
                k = int(np.argmax(gain))
                if best is None or gain[k] > best[0]:
                    best = (gain[k], j, float(cuts[k]),
                            float(gl[k] / (hl[k] + 1.0)),
                            float(gr[k] / (hr[k] + 1.0)))
            if best is None:
                break
            _, j, thr, vl, vr = best
            vl, vr = np.clip(vl, -4, 4), np.clip(vr, -4, 4)
            self.trees.append((j, thr, vl, vr))
            fx += self.lr * np.where(x[:, j] <= thr, vl, vr)
        return self

    def _raw(self, x: np.ndarray) -> np.ndarray:
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        fx = np.full(len(x), self.base)
        for j, thr, vl, vr in self.trees:
            fx += self.lr * np.where(x[:, j] <= thr, vl, vr)
        return fx

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return _sigmoid(self._raw(x))


class Calibrator:
    """Isotonic (pool-adjacent-violators) probability calibration.

    Fitted on a held-out slice of TRAIN, never on test. Falls back to the
    identity when there is too little data to fit a monotone map.
    """

    def fit(self, p: np.ndarray, y: np.ndarray) -> "Calibrator":
        if len(p) < 200:
            self.x = self.y = None
            return self
        o = np.argsort(p)
        px, py = p[o].astype(float), y[o].astype(float)
        # PAVA
        vals, wts = list(py), [1.0] * len(py)
        i = 0
        while i < len(vals) - 1:
            if vals[i] > vals[i + 1]:
                tw = wts[i] + wts[i + 1]
                nv = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / tw
                vals[i:i + 2] = [nv]
                wts[i:i + 2] = [tw]
                if i > 0:
                    i -= 1
            else:
                i += 1
        fitted, k = np.empty(len(py)), 0
        for v, wt in zip(vals, wts):
            fitted[k:k + int(wt)] = v
            k += int(wt)
        self.x, self.y = px, fitted
        return self

    def transform(self, p: np.ndarray) -> np.ndarray:
        if self.x is None:
            return p
        return np.clip(np.interp(p, self.x, self.y), 1e-4, 1 - 1e-4)
