"""Reconstruct option round-trips and position episodes from cached get_option_orders pages.
Outputs lots.csv (FIFO-matched round trips, with ep_id) and episodes.csv (flat-to-flat positions)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from paths import CACHE, ACCTS, PX, PX_H1, EPISODES, FEATURES, LOTS  # noqa: E402
ROOT = CACHE
import json, glob, collections, csv
from datetime import date, datetime, timedelta
TODAY = date.fromisoformat(os.environ.get("TRADE_AUDIT_ASOF", date.today().isoformat()))
def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))

def load(acct):
    seen = {}
    for f in sorted(glob.glob(f"{ROOT}/{acct}/page_*.json")):
        for o in json.load(open(f))["data"]["orders"]:
            seen[o["id"]] = o
    return list(seen.values())

fills, nonfilled = [], collections.defaultdict(list)
for acct in ACCTS:
    for o in load(acct):
        for leg in o["legs"]:
            exs = leg.get("executions") or []
            if not exs:
                nonfilled[(acct, leg["option_id"])].append(dict(created=o["created_at"], state=o["state"], effect=leg["position_effect"], price=float(o["price"] or 0), qty=float(o["quantity"])))
            for ex in exs:
                fills.append(dict(acct=acct, order_id=o["id"], sym=o["chain_symbol"], option_id=leg["option_id"],
                    expiry=leg["expiration_date"], strike=float(leg["strike_price"]), otype=leg["option_type"],
                    side=leg["side"], effect=leg["position_effect"], price=float(ex["price"]), qty=float(ex["quantity"]),
                    ts=ex["timestamp"], created=o["created_at"], order_type=f'{o.get("type")}/{o.get("trigger")}',
                    tif=o.get("time_in_force"), nlegs=len(o["legs"]), state=o["state"], agent=o.get("placed_agent")))
fills.sort(key=lambda f: (f["acct"], f["option_id"], f["ts"]))
by_key = collections.defaultdict(list)
for f in fills: by_key[(f["acct"], f["option_id"])].append(f)

lots, episodes, orphans = [], [], collections.Counter()
ep_seq = 0
for (acct, oid), fs in by_key.items():
    openq = collections.deque(); ep = None
    def pos(): return sum(l["dir"] * l["qty"] for l in openq)
    def new_ep(f, d):
        global ep_seq
        ep_seq += 1
        return dict(ep_id=ep_seq, acct=acct, sym=f["sym"], option_id=oid, expiry=f["expiry"], strike=f["strike"], otype=f["otype"],
                    dir="long" if d == 1 else "short", first_open=f["ts"], first_created=f["created"], last_open=f["ts"], last_close=None,
                    open_qty=0.0, open_cost=0.0, close_qty=0.0, close_proceeds=0.0, n_open_orders=set(), n_close_orders=set(),
                    max_qty=0.0, pnl=0.0, expired_qty=0.0, status="open", multileg=0, order_types=set(), first_fill_price=f["price"],
                    avg_down=0, adds_higher=0, first_close=None)
    for f in fs:
        if f["effect"] == "open":
            d = 1 if f["side"] == "buy" else -1
            if ep is None: ep = new_ep(f, d)
            else:
                if f["price"] * d < ep["first_fill_price"] * d: ep["avg_down"] += 1
                elif f["price"] * d > ep["first_fill_price"] * d: ep["adds_higher"] += 1
            openq.append(dict(qty=f["qty"], price=f["price"], ts=f["ts"], order_id=f["order_id"], dir=d))
            ep["open_qty"] += f["qty"]; ep["open_cost"] += f["qty"] * f["price"] * 100; ep["last_open"] = f["ts"]
            ep["n_open_orders"].add(f["order_id"]); ep["max_qty"] = max(ep["max_qty"], abs(pos()))
            ep["multileg"] += (f["nlegs"] > 1); ep["order_types"].add(f["order_type"])
        else:
            want = 1 if f["side"] == "sell" else -1
            rem = f["qty"]
            while rem > 1e-9 and openq and openq[0]["dir"] == want:
                l = openq[0]; q = min(rem, l["qty"])
                pnl = (f["price"] - l["price"]) * q * 100 * l["dir"]
                lots.append(dict(ep_id=ep["ep_id"] if ep else 0, acct=acct, sym=f["sym"], option_id=oid, expiry=f["expiry"], strike=f["strike"],
                                 otype=f["otype"], dir="long" if l["dir"] == 1 else "short", qty=q, open_ts=l["ts"], open_price=l["price"],
                                 close_ts=f["ts"], close_price=f["price"], pnl=round(pnl, 2), how="trade", open_order=l["order_id"], close_order=f["order_id"]))
                l["qty"] -= q; rem -= q
                if l["qty"] <= 1e-9: openq.popleft()
                if ep is not None: ep["pnl"] += pnl
            if rem > 1e-9: orphans[acct] += 1
            if ep is not None:
                ep["close_qty"] += f["qty"] - rem; ep["close_proceeds"] += (f["qty"] - rem) * f["price"] * 100
                ep["n_close_orders"].add(f["order_id"]); ep["last_close"] = f["ts"]; ep["order_types"].add(f["order_type"])
                if ep["first_close"] is None: ep["first_close"] = f["ts"]
                if abs(pos()) < 1e-9:
                    ep["status"] = "closed"; episodes.append(ep); ep = None
    if openq:
        exp = date.fromisoformat(fs[0]["expiry"])
        if exp < TODAY:
            for l in openq:
                pnl = (0 - l["price"]) * l["qty"] * 100 * l["dir"]
                lots.append(dict(ep_id=ep["ep_id"] if ep else 0, acct=acct, sym=fs[0]["sym"], option_id=oid, expiry=fs[0]["expiry"], strike=fs[0]["strike"],
                                 otype=fs[0]["otype"], dir="long" if l["dir"] == 1 else "short", qty=l["qty"], open_ts=l["ts"], open_price=l["price"],
                                 close_ts=f"{exp.isoformat()}T20:00:00.000000Z", close_price=0.0, pnl=round(pnl, 2), how="expired", open_order=l["order_id"], close_order=""))
                if ep is not None: ep["pnl"] += pnl; ep["expired_qty"] += l["qty"]
            if ep is not None:
                ep["status"] = "expired"; ep["last_close"] = f"{exp.isoformat()}T20:00:00.000000Z"; episodes.append(ep)
        elif ep is not None:
            ep["status"] = "open"; episodes.append(ep)

for e in episodes:
    e["n_open_orders"] = len(e["n_open_orders"]); e["n_close_orders"] = len(e["n_close_orders"])
    e["order_types"] = "|".join(sorted(e["order_types"]))
    e["entry_vwap"] = round(e["open_cost"] / (e["open_qty"] * 100), 4) if e["open_qty"] else 0
    e["exit_vwap"] = round(e["close_proceeds"] / (e["close_qty"] * 100), 4) if e["close_qty"] else 0
    e["pnl"] = round(e["pnl"], 2); e["open_cost"] = round(e["open_cost"], 2); e["close_proceeds"] = round(e["close_proceeds"], 2)
    e["ret"] = round(e["pnl"] / e["open_cost"], 4) if e["open_cost"] else 0
    t0 = ts(e["first_open"]); e["dte_entry"] = (date.fromisoformat(e["expiry"]) - t0.date()).days
    e["entry_wait_s"] = round((t0 - ts(e["first_created"])).total_seconds())
    e["hold_min"] = round((ts(e["last_close"]) - t0).total_seconds() / 60, 1) if e["last_close"] else ""
    e["first_close_min"] = round((ts(e["first_close"]) - t0).total_seconds() / 60, 1) if e["first_close"] else ""
    e["scale_in_min"] = round((ts(e["last_open"]) - t0).total_seconds() / 60, 1)
    # cancelled/rejected orders around the episode: before entry (chasing) and during (indecision)
    nf = nonfilled.get((e["acct"], e["option_id"]), [])
    lo = t0 - timedelta(hours=2); hi = ts(e["last_close"]) if e["last_close"] else ts(e["last_open"]) + timedelta(hours=2)
    e["cancels_before"] = sum(1 for o in nf if lo <= ts(o["created"]) < t0 and o["effect"] == "open")
    e["cancels_during"] = sum(1 for o in nf if t0 <= ts(o["created"]) <= hi)
    e["cancel_close_above"] = sum(1 for o in nf if t0 <= ts(o["created"]) <= hi and o["effect"] == "close" and e["exit_vwap"] and o["price"] > e["exit_vwap"] * 1.001)
    for k in ("first_created", "first_close", "last_open"): pass

keys = list(episodes[0].keys())
with open(f"{ROOT}/lots.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(lots[0].keys())); w.writeheader(); w.writerows(lots)
with open(f"{ROOT}/episodes.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=keys); w.writeheader(); w.writerows(episodes)
print("fills", len(fills), "lots", len(lots), "episodes", len(episodes), "orphan closes", dict(orphans))
for acct in ACCTS:
    es = [e for e in episodes if e["acct"] == acct and e["status"] != "open"]
    print(f"{acct}: episodes={len(es)} pnl={sum(e['pnl'] for e in es):,.0f} win={sum(1 for e in es if e['pnl']>0)/len(es):.1%} avg_down_eps={sum(1 for e in es if e['avg_down'])} cancels_before={sum(e['cancels_before'] for e in es)} cancels_during={sum(e['cancels_during'] for e in es)}")
