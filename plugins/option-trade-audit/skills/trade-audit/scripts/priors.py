"""Summarize the audited trade history into `<TRADE_AUDIT_DIR>/trade_priors.json`.

    python3 priors.py

One row per bucket: {n, win, pf}. No dollar amounts — the file is what
the trade-check skill (`gate.py`) consults at decision time, so re-running the audit (new
trades paged in, `run.sh`) moves the gate's thresholds without any code change.
The bucket edges live in trade-check/scripts/gate.py and are imported here so the
history and a live candidate are always labelled the same way.
"""
import datetime as dt
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "trade-check", "scripts"))
from paths import FEATURES, PRIORS  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from gate import (label_dte, label_ext, label_hour, label_money, label_prem,  # noqa: E402
                        label_rangepos, label_reentry, label_rsi, label_seq, label_trend,
                        label_vix)

OUT = os.environ.get("TRADE_PRIORS", PRIORS)
BOOLS = ["above_e20", "above_e50", "spy_above_e20", "spy_above_e50"]


def stats(g):
    w, l = g[g.pnl > 0], g[g.pnl <= 0]
    pf = (w.pnl.sum() / -l.pnl.sum()) if len(l) and l.pnl.sum() < 0 else float("inf")
    return {"n": int(len(g)), "win": round(float((g.pnl > 0).mean()), 3),
            "pf": (round(float(pf), 2) if np.isfinite(pf) else None)}


def family(F, keyfn):
    out = {}
    keys = F.apply(keyfn, axis=1)
    for k, g in F.groupby(keys):
        if k is None or (isinstance(k, float) and np.isnan(k)) or "None" in str(k):
            continue
        out[str(k)] = stats(g)
    return out


def main():
    F = pd.read_csv(FEATURES, parse_dates=["d0", "dx"])
    F = F[F.status != "open"].copy()
    for c in BOOLS:
        F[c] = F[c].astype(str).eq("True")
    F["hold_min"] = pd.to_numeric(F.hold_min, errors="coerce")
    F["dirn"] = np.where(F.sign == 1, "bullish", "bearish")
    F["trend"] = [label_trend(a, b) for a, b in zip(F.above_e20, F.above_e50)]
    F["spy_regime"] = np.select(
        [(F.spy_above_e20 & F.spy_above_e50), (~F.spy_above_e20 & ~F.spy_above_e50)],
        ["SPY up", "SPY down"], "SPY mixed")
    fam = {
        "dte": family(F, lambda r: label_dte(r.dte_entry)),
        "moneyness": family(F[F.dte_entry > 1], lambda r: label_money(r.otm_pct * 100)),
        "moneyness_0_1dte": family(F[F.dte_entry <= 1],
                                   lambda r: label_money((r.otm_h if pd.notna(r.get("otm_h")) else r.otm_pct) * 100)),
        "premium": family(F, lambda r: label_prem(r.open_cost)),
        "vix": family(F, lambda r: label_vix(r.vix)),
        "hour": family(F, lambda r: label_hour(r.entry_hour)),
        "seq": family(F, lambda r: label_seq(r.seq_in_day)),
        "adds": family(F, lambda r: "single" if r.n_open_orders == 1
                       else ("avg down" if r.avg_down > 0 else "add up")),
        "dir_x_spy": family(F, lambda r: f"{r.dirn}|{r.spy_regime}"),
        "dir_x_trend": family(F, lambda r: f"{r.dirn}|{r.trend}"),
        "dir_x_rsi": family(F, lambda r: f"{r.dirn}|{label_rsi(r.rsi)}"),
        "dir_x_ext": family(F, lambda r: f"{r.dirn}|{label_ext(r.dist20_atr)}"),
        "earnings": family(F, lambda r: r.earn_ctx if isinstance(r.get("earn_ctx"), str) else None),
        "reentry_after_loss": family(F[F.prev_pnl < -200],
                                     lambda r: label_reentry(r.min_since_prev_exit)),
        "hold": family(F, lambda r: str(r.hold_b)),
    }
    if "pos_in_range_h" in F:
        sub = F[F.pos_in_range_h.notna()]
        fam["range_pos"] = family(sub, lambda r: f"{r.dirn}|{label_rangepos(r.pos_in_range_h)}")
    doc = {"generated": dt.date.today().isoformat(),
           "source": "option-trade-audit plugin (order history, FIFO reconstructed)",
           "window": [str(F.d0.min().date()), str(F.d0.max().date())],
           "n": int(len(F)), "min_n_to_use": 20,
           "overall": stats(F), "buckets": fam}
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {len(F)} positions, "
          f"{sum(len(v) for v in fam.values())} buckets across {len(fam)} families")


if __name__ == "__main__":
    main()
