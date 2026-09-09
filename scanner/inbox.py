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


def fresh_inbound(since_rowid: int) -> list:
    """(rowid, sender, body, service) for inbound texts after since_rowid."""
    uri = f"file:{CHATDB}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT m.ROWID, h.id, m.text, m.service FROM message m "
            "JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.text IS NOT NULL "
            "ORDER BY m.ROWID", (since_rowid,)).fetchall()
    finally:
        con.close()
    return [(r, s, (b or "").strip(), (svc or "")) for r, s, b, svc in rows]


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
        st = {"last_rowid": max(r for r, _, _, _ in msgs)}
        json.dump(st, open(ISTATE, "w"))
        print(f"inbox: cursor set, {len(msgs)} old texts ignored", flush=True)
        return 0
    cfg = load_config()
    paused = cfg.get("paused", {})
    today = dt.date.today().isoformat()
    for rowid, sender, body, svc in msgs:
        if not body:
            continue
        parts = body.upper().split()
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        svc_in = "SMS" if "sms" in svc.lower() else "iMessage"
        reply, rsvc = None, svc_in
        if cmd == "SUBSCRIBE":
            want = "SMS" if arg == "SMS" else "iMessage" if arg == "IMESSAGE" \
                else svc_in
            if cfg.get("blocked", {}).pop(sender, None) is not None:
                pass  # re-subscribe clears the block
            set_list(cfg, sender, want)
            paused.pop(sender, None)
            reply = (f"Subscribed to trade alerts via {want}. "
                     f"PAUSE pauses a day, UNSUBSCRIBE stops all.")
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
            reply = "Unsubscribed. Text SUBSCRIBE to rejoin."
        if reply:
            print(f"inbox: {sender} -> {cmd} ({svc_in})", flush=True)
            if not dry:
                print("  ", _send_via(sender, reply, rsvc), flush=True)
            else:
                print(f"   dry: would reply via {rsvc}", flush=True)
    cfg["paused"] = {k: v for k, v in paused.items() if v >= today}
    save_config(cfg)
    if msgs:
        st["last_rowid"] = max(r for r, _, _, _ in msgs)
        json.dump(st, open(ISTATE, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
