"""Regression test: one inbound command row must act at most once.

Repro for the 2026-09-09 incident (+13475798719): a single UNSUBSCRIBE row
was re-processed on later polls via the 2h stale-sweep (the UNSUBSCRIBE
branch wiped its own done-key, defeating the 10-min dedup), so the user got
"Unsubscribed" first and then "Not subscribed — nothing to stop." on later
polls; replayed SUBSCRIBE rows could even resurrect unsubscribed numbers.

Runs the REAL inbox.main() against a fake chat.db + temp config/state, with
notify.send_smart monkeypatched to a recorder. No network, no real sends.

Usage (project root, venv active):
    python scanner/test_inbox.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox as _inbox  # noqa: E402
import notify as _notify  # noqa: E402

NUM = "+15550001111"  # fictional NANP number used only in this test
COCOA = 978307200


def _cocoa_ns_now() -> int:
    return int((time.time() - COCOA) * 1e9)


def _make_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    con.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, "
                "handle_id INT, text TEXT, service TEXT, attributedBody BLOB, "
                "is_from_me INT, date INT)")
    con.execute("INSERT INTO handle (ROWID, id) VALUES (1, ?)", (NUM,))
    con.commit()
    return con


_next_rowid = [100]


def _add(con: sqlite3.Connection, body: str):
    _next_rowid[0] += 1
    con.execute("INSERT INTO message (ROWID, handle_id, text, service, "
                "attributedBody, is_from_me, date) VALUES (?, 1, ?, "
                "'iMessage', NULL, 0, ?)",
                (_next_rowid[0], body, _cocoa_ns_now()))
    con.commit()


def _run(inbox_mod, channel: str = "imessage") -> None:
    old_argv = sys.argv
    sys.argv = ["inbox.py", "--channels", channel]
    try:
        assert inbox_mod.main() == 0
    finally:
        sys.argv = old_argv


def _age_done(path: str, seconds: int = 3600):
    """Simulate production poll spacing: the 10-min same-text dedup expires
    between polls, so only true rowid idempotency can keep replays silent."""
    st = json.load(open(path))
    st["done"] = {k: v - seconds for k, v in st.get("done", {}).items()}
    json.dump(st, open(path, "w"))


def main() -> int:
    fails = []

    def check(name: str, cond: bool, extra: str = ""):
        print(("PASS " if cond else "FAIL ") + name +
              ("" if cond else f" [{extra}]"))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="inbox_test_")
    db_path = os.path.join(tmp, "chat.db")
    cfg_path = os.path.join(tmp, "config.json")
    st_path = os.path.join(tmp, "inbox_state.json")
    con = _make_db(db_path)
    json.dump({"sms_to": NUM, "imsg_to": "", "paused": {}, "blocked": {},
               "subscribers": {NUM: NUM}}, open(cfg_path, "w"))

    old = (_inbox.CHATDB, _inbox.CONFIG, _inbox.ISTATE, _notify.CONFIG,
           _notify.send_smart)
    sent: list = []
    _inbox.CHATDB, _inbox.CONFIG, _inbox.ISTATE = db_path, cfg_path, st_path
    _notify.CONFIG = cfg_path
    _notify.send_smart = lambda to, text, **k: sent.append((to, text)) or "ok"
    try:
        json.dump({"last_rowid": 0}, open(st_path, "w"))  # skip first-run guard
        cfg = lambda: json.load(open(cfg_path))  # noqa: E731

        # 1. genuine SUBSCRIBE (already subscribed -> confirmation, stays on)
        _add(con, "SUBSCRIBE")
        _run(_inbox)
        check("subscribe-keeps-member", NUM in cfg()["sms_to"])
        n1 = len(sent)

        # 2. genuine UNSUBSCRIBE -> removed + blocked, exactly one reply
        _add(con, "UNSUBSCRIBE")
        _run(_inbox)
        c2 = cfg()
        check("unsub-removes-member", NUM not in c2["sms_to"],
              c2["sms_to"])
        check("unsub-blocks", NUM in c2.get("blocked", {}))
        check("unsub-one-reply", len(sent) == n1 + 1, f"sent={len(sent)}")
        check("unsub-reply-text", "Unsubscribed" in sent[-1][1], sent[-1][1])

        # 3-5. three more polls, no new texts -> total silence (the bug:
        #      same UNSUBSCRIBE row re-processed -> "Not subscribed" spam).
        #      done-aging simulates real 5-min poll spacing past the dedup.
        _age_done(st_path)
        _run(_inbox)
        _age_done(st_path)
        _run(_inbox)
        _age_done(st_path)
        _run(_inbox)
        check("replay-silence", len(sent) == n1 + 1, f"sent={len(sent)}")
        check("stays-unsubscribed", NUM not in cfg()["sms_to"])

        # 6. genuine new SUBSCRIBE still works (not swallowed by idempotency)
        _add(con, "SUBSCRIBE")
        _run(_inbox)
        check("resub-works", NUM in cfg()["sms_to"])
        n6 = len(sent)
        check("resub-one-reply", n6 == n1 + 2, f"sent={n6}")

        # 7. burst: UNSUBSCRIBE then non-command chatter -> command still acts
        _add(con, "UNSUBSCRIBE")
        _add(con, "thanks!")
        _run(_inbox)
        check("burst-unsub-acts", NUM not in cfg()["sms_to"])
        check("burst-one-reply", len(sent) == n6 + 1, f"sent={len(sent)}")

        # 8. STOP behaves like UNSUBSCRIBE (no silent ignore)
        _add(con, "SUBSCRIBE")
        _run(_inbox)
        n8 = len(sent)
        _add(con, "STOP")
        _run(_inbox)
        check("stop-unsubscribes", NUM not in cfg()["sms_to"])
        check("stop-one-reply", len(sent) == n8 + 1, f"sent={len(sent)}")

        # 9. replay silence holds after STOP too
        _age_done(st_path)
        _run(_inbox)
        _age_done(st_path)
        _run(_inbox)
        check("post-stop-silence", len(sent) == n8 + 1, f"sent={len(sent)}")
    finally:
        (_inbox.CHATDB, _inbox.CONFIG, _inbox.ISTATE, _notify.CONFIG,
         _notify.send_smart) = old
        con.close()

    if fails:
        print(f"{len(fails)} failures: {fails}")
    else:
        print("all inbox replay checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
