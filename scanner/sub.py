"""Manual subscription admin: add/remove/list/pause/resume numbers.

Usage (Terminal):
  python scanner/sub.py list
  python scanner/sub.py add +16506987800 [name] [sms|imessage]
  python scanner/sub.py remove +16506987800
  python scanner/sub.py pause +16506987800
  python scanner/sub.py resume +16506987800
Edits scanner/config.json directly. Transport defaults to SMS.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "scanner", "config.json")


def load():
    return json.load(open(CONFIG))


def save(cfg):
    json.dump(cfg, open(CONFIG, "w"))


def canon(num: str) -> str:
    n = num.strip()
    if "@" in n:
        return n.lower()
    d = "".join(c for c in n if c.isdigit())
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return n


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in (
            "list", "add", "remove", "pause", "resume"):
        print(__doc__.strip().splitlines()[0])
        print("commands: list | add NUM [name] [sms|imessage] | remove NUM | "
              "pause NUM | resume NUM")
        return 1
    cmd = sys.argv[1]
    cfg = load()
    if cmd == "list":
        print("sms :", cfg.get("sms_to", ""))
        print("imsg:", cfg.get("imsg_to", ""))
        print("paused:", cfg.get("paused", {}))
        print("blocked:", cfg.get("blocked", {}))
        print("names:", cfg.get("subscribers", {}))
        return 0
    num = canon(sys.argv[2])
    if cmd == "add":
        name = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith(
            "+") and sys.argv[3] not in ("sms", "imessage") else num
        svc = next((a for a in sys.argv[3:] if a in ("sms", "imessage")), "sms")
        key = "imsg_to" if svc == "imessage" else "sms_to"
        nums = [d.strip() for d in cfg.get(key, "").split(",") if d.strip()]
        if num not in nums:
            nums.append(num)
        cfg[key] = ",".join(nums)
        cfg.get("blocked", {}).pop(num, None)
        cfg.get("paused", {}).pop(num, None)
        cfg.setdefault("subscribers", {})[num] = name
        save(cfg)
        print(f"added {num} ({name}) via {svc.upper()}")
    elif cmd == "remove":
        for key in ("imsg_to", "sms_to"):
            cfg[key] = ",".join(d.strip() for d in cfg.get(key, "").split(",")
                                if d.strip() and d.strip() != num)
        cfg.setdefault("blocked", {})[num] = "manual"
        cfg.get("paused", {}).pop(num, None)
        save(cfg)
        print(f"removed + blocked {num}")
    elif cmd == "pause":
        import datetime as dt
        cfg.setdefault("paused", {})[num] = (
            dt.date.today() + dt.timedelta(days=1)).isoformat()
        save(cfg)
        print(f"paused {num} till tomorrow")
    elif cmd == "resume":
        cfg.get("paused", {}).pop(num, None)
        save(cfg)
        print(f"resumed {num}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
