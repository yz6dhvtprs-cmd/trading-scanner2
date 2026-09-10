"""Inbox commands: SUBSCRIBE / PAUSE / UNSUBSCRIBE / RESUME via incoming texts.

Usage: python scanner/inbox.py --channels dry
Polls this Mac's Messages database (chat.db) for new inbound texts since the
last run (scanner/inbox_state.json) and manages scanner/config.json:
  SUBSCRIBE [SMS|IMESSAGE] -> add sender (default: the transport it came in on)
  PAUSE                    -> silence sender until tomorrow midnight
  RESUME                   -> lift a pause early
  UNSUBSCRIBE              -> remove sender; stays off until SUBSCRIBE again
Every command gets a confirmation text back on the same transport.
Requires Full Disk Access for the terminal running this (Apple locks chat.db).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notify import _recipients, _send_via, load_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "scanner", "config.json")
ISTATE = os.path.join(ROOT, "scanner", "inbox_state.json")


def norm(num: str) -> str:
    """Handle-id formats vary (+1906…, 1906…, emails) -> canonical E.164-ish."""
    n = num.strip()
    if "@" in n:
        return n.lower()
    d = "".join(c for c in n if c.isdigit())
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return n if n.startswith("+") else ("+" + d if d else n)
CHATDB = os.path.expanduser("~/Library/Messages/chat.db")
# Cocoa epoch (2001-01-01) -> Unix epoch offset, nanoseconds
COCOA = 978307200


def save_config(cfg: dict):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f)


def in_list(cfg: dict, num: str) -> str:
    if num in [d.strip() for d in cfg.get("imsg_to", "").split(",") if d.strip()]:
        return "iMessage"
    if num in [d.strip() for d in cfg.get("sms_to", "").split(",") if d.strip()]:
        return "SMS"
    return ""


def set_list(cfg: dict, num: str, svc: str):
    key = "imsg_to" if svc == "iMessage" else "sms_to"
    nums = [d.strip() for d in cfg.get(key, "").split(",") if d.strip()]
    if num not in nums:
        nums.append(num)
    cfg[key] = ",".join(nums)
    other = "sms_to" if svc == "iMessage" else "imsg_to"
    nums = [d.strip() for d in cfg.get(other, "").split(",")
            if d.strip() and d.strip() != num]
    cfg[other] = ",".join(nums)
    subs = cfg.get("subscribers", {})
    subs[num] = subs.get(num, num)
    cfg["subscribers"] = subs
    cfg.get("blocked", {}).pop(num, None)


def drop_list(cfg: dict, num: str):
    for key in ("imsg_to", "sms_to"):
        nums = [d.strip() for d in cfg.get(key, "").split(",")
                if d.strip() and d.strip() != num]
        cfg[key] = ",".join(nums)
    cfg.setdefault("blocked", {})[num] = dt.date.today().isoformat()


def _canon_cmd(body: str) -> str:
    """Canonical command word, or "" for non-command chatter. STOP/START
    map to UNSUBSCRIBE/SUBSCRIBE so they never silently do nothing."""
    w = (body or "").upper().split()
    if not w:
        return ""
    w = w[0]
    if w == "STOP":
        return "UNSUBSCRIBE"
    if w == "START":
        return "SUBSCRIBE"
    return w if w in ("SUBSCRIBE", "PAUSE", "RESUME", "UNSUBSCRIBE") else ""


def blob_text(blob) -> str:
    """RCS bodies often live in attributedBody (text column NULL). The blob
    is a binary plist wrapping an archiver stream; command keywords survive
    as plain ASCII — extract uppercase words."""
    import re
    if not blob:
        return ""
    try:
        s = bytes(blob).decode("utf-8", "ignore")
    except Exception:
        return ""
    words = re.findall(r"[A-Z]{3,}(?: [A-Z0-9]+)?", s)
    for w in words:
        if w.split()[0] in ("SUBSCRIBE", "PAUSE", "RESUME", "UNSUBSCRIBE",
                             "START", "STOP"):
            return w
    return ""


def fresh_inbound(since_rowid: int) -> list:
    """(rowid, sender, body, service) for inbound texts after since_rowid,
    plus a sweep of the last 2h for missed COMMAND texts (cursor may have
    been set past them). RCS bodies fall back to attributedBody."""
    import time
    cutoff_ns = int((time.time() - 2 * 3600 - COCOA) * 1e9)
    uri = f"file:{CHATDB}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT m.ROWID, h.id, m.text, m.service, m.attributedBody "
            "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE m.ROWID > ? AND m.is_from_me = 0 "
            "ORDER BY m.ROWID", (since_rowid,)).fetchall()
        sweep = con.execute(
            "SELECT m.ROWID, h.id, m.text, m.service, m.attributedBody "
            "FROM message m JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE m.is_from_me = 0 AND m.date > ? "
            "ORDER BY m.ROWID DESC LIMIT 200", (cutoff_ns,)).fetchall()
    finally:
        con.close()

    def body_of(b, blob):
        b = (b or "").strip()
        return b if b else blob_text(blob)

    # fresh rows carry a flag; sweep rows are stale: they may PAUSE/RESUME/
    # UNSUBSCRIBE (state that matters now) but never SUBSCRIBE (a stale
    # SUBSCRIBE would resurrect unsubscribed/blocked numbers on every sweep).
    out = [(r, s, body_of(b, blob), (svc or ""), True)
           for r, s, b, svc, blob in rows if body_of(b, blob)]
    seen = {r for r, _, _, _, _ in out}
    for r, s, b, svc, blob in sweep:
        body = body_of(b, blob)
        if not body or r in seen:
            continue
        first = body.upper().split()[0] if body.strip() else ""
        if first in ("PAUSE", "RESUME", "UNSUBSCRIBE", "STOP"):
            out.append((r, s, body, (svc or ""), False))
            seen.add(r)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="dry")
    a = ap.parse_args()
    dry = a.channels.strip() == "dry"
    try:
        st = json.load(open(ISTATE)) if os.path.exists(ISTATE) else {}
    except Exception:
        st = {}
    try:
        msgs = fresh_inbound(int(st.get("last_rowid", 0)))
    except Exception as e:
        print(f"inbox: chat.db unreadable ({e}) — grant Full Disk Access",
              flush=True)
        return 0
    if msgs and not st:
        # first run: establish cursor, don't backfill history
        st = {"last_rowid": max(r for r, _, _, _, _ in msgs),
              "seen": sorted({r for r, _, _, _, _ in msgs})[-5000:]}
        json.dump(st, open(ISTATE, "w"))
        print(f"inbox: cursor set, {len(msgs)} old texts ignored", flush=True)
        return 0
    cfg = load_config()
    paused = cfg.get("paused", {})
    today = dt.date.today().isoformat()
    import time as _t
    now_ep = _t.time()
    _d = st.get("done", {})
    done = {k: 0 for k in _d} if isinstance(_d, list) else dict(_d)
    seen_run = set()
    # Idempotency: every chat.db row acts at most once. The 2h stale-sweep
    # refetches handled rows, and relay duplicates resurface them, so without
    # this a handled UNSUBSCRIBE re-fires ("Not subscribed" spam) and a
    # handled SUBSCRIBE resurrects unsubscribed numbers. Seen rowids persist
    # in inbox_state.json; the cursor still advances over everything fetched.
    seen = set(st.get("seen", []))
    if msgs:
        st["last_rowid"] = max(st.get("last_rowid", 0),
                               max(r for r, _, _, _, _ in msgs))
    fresh_msgs = [m for m in msgs if m[0] not in seen]
    for r, _, _, _, _ in msgs:
        seen.add(r)
    # last-text-wins over COMMANDS only: each sender's newest command acts;
    # older rows in the burst are superseded (never replayed one by one),
    # and non-command chatter never swallows a pending command.
    latest = {}
    for rowid, sender, body, svc, fresh in fresh_msgs:
        s = norm(sender)
        if _canon_cmd(body) and (s not in latest or rowid > latest[s][0]):
            latest[s] = (rowid, body, svc, fresh)
    msgs = [(r, s, b, v, f) for s, (r, b, v, f) in latest.items()]
    for rowid, sender, body, svc, fresh in msgs:
        sender = norm(sender)
        if not body:
            continue
        if not sender.startswith("+1"):
            continue  # geo-fence: non-NANP senders get zero response
        if body.upper().split()[0] == "SUBSCRIBE" and not fresh:
            continue  # stale sweep find: never resurrect on old texts
        cmd_key = f"{sender}|{body.upper()}"
        if cmd_key in seen_run:
            continue  # carrier duplicate rows in one run
        seen_run.add(cmd_key)
        if cmd_key in done and now_ep - done[cmd_key] < 600:
            continue  # same text retried within 10 min: already answered
        done[cmd_key] = now_ep
        parts = body.upper().split()
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd == "STOP":
            cmd = "UNSUBSCRIBE"  # STOP unsubscribes, never silently ignored
        elif cmd == "START":
            cmd = "SUBSCRIBE"
        # house rule: inbound may be RCS/iMessage/SMS; outbound is ALWAYS SMS
        svc_in = "SMS"
        reply, rsvc = None, svc_in
        cur = in_list(cfg, sender)
        today = dt.date.today().isoformat()
        if cmd == "SUBSCRIBE" and cur:
            was_paused = paused.pop(sender, None)
            reply, rsvc = (f"Already subscribed via SMS. "
                           f"PAUSE pauses a day, UNSUBSCRIBE stops all."), "SMS"
            if was_paused:
                reply = f"Welcome back — pause lifted, alerts on. {reply}"
            top = []
            try:
                st10 = json.load(open(os.path.join(
                    ROOT, "scanner", "top10_state.json")))
                w = json.load(open(os.path.join(
                    ROOT, "scanner", "agent_watch.json")))
                top = [f"{t}{w[t]['dir']}" for t in st10.get("top10", [])[:10]
                       if t in w]
            except Exception:
                pass
            if top:
                reply += f" Current TOP10: {', '.join(top)}."
        elif cmd == "PAUSE" and (paused.get(sender, "") or "") >= today:
            reply, rsvc = (f"Already paused till {paused.get(sender)}. "
                           f"RESUME restarts now."), "SMS"
        elif cmd == "RESUME" and (paused.get(sender, "") or "") < today:
            reply, rsvc = "Already active — alerts on.", "SMS"
        elif cmd == "UNSUBSCRIBE" and not cur:
            reply, rsvc = "Not subscribed — nothing to stop.", "SMS"
        elif cmd == "SUBSCRIBE":
            want = "SMS"  # outbound is always SMS, no exceptions
            if cfg.get("blocked", {}).pop(sender, None) is not None:
                pass  # re-subscribe clears the block
            set_list(cfg, sender, want)
            paused.pop(sender, None)
            top = []
            try:
                st10 = json.load(open(os.path.join(
                    ROOT, "scanner", "top10_state.json")))
                w = json.load(open(os.path.join(
                    ROOT, "scanner", "agent_watch.json")))
                top = [f"{t}{w[t]['dir']}" for t in st10.get("top10", [])[:10]
                       if t in w]
            except Exception:
                pass
            reply = (f"Subscribed to trade alerts via {want}. "
                     f"PAUSE pauses a day, UNSUBSCRIBE stops all.")
            if top:
                reply += f" Current TOP10: {', '.join(top)}."
            rsvc = want
        elif cmd == "PAUSE":
            paused[sender] = (dt.date.today() +
                              dt.timedelta(days=1)).isoformat()
            reply = "Paused until tomorrow. RESUME restarts now."
        elif cmd == "RESUME":
            paused.pop(sender, None)
            reply = "Resumed. Alerts on."
        elif cmd == "UNSUBSCRIBE":
            drop_list(cfg, sender)
            paused.pop(sender, None)
            for k in [k for k in done
                      if k.startswith(sender + "|SUBSCRIBE") or
                      k.startswith(sender + "|START")]:
                del done[k]  # allow a future SUBSCRIBE through; keep this
                # UNSUBSCRIBE key so the same row never re-confirms next poll
            reply = "Unsubscribed. Text SUBSCRIBE to rejoin."
        if reply:
            print(f"inbox: {sender} -> {cmd} ({svc_in})", flush=True)
            if not dry:
                from notify import send_smart as _smart
                print("  ", _smart(sender, reply), flush=True)
            else:
                print("   dry: would smart-reply (iMessage->SMS)", flush=True)
    cfg["paused"] = {k: v for k, v in paused.items() if v >= today}
    if not dry:
        save_config(cfg)
        # last_rowid already advanced over everything fetched above (it must
        # never regress to a collapsed subset); persist acted rowids here.
        st["seen"] = sorted(seen)[-5000:]
        st["done"] = dict(sorted(done.items(), key=lambda kv: -kv[1])[:200])
        json.dump(st, open(ISTATE, "w"))
    else:
        print("dry: state/config untouched", flush=True)
    print(f"inbox poll {dt.datetime.now().strftime('%H:%M')}: "
          f"{len(msgs)} new inbound", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
