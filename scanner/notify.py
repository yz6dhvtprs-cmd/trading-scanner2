"""Free alert channels: telegram (phone push), mac banner, imessage, shortcut.

Config: scanner/config.json (see config.example.json) or env vars:
  TRADE_TG_TOKEN, TRADE_TG_CHAT, TRADE_IMSG_TO
No secrets are ever written to the repo; config.json is gitignored.

Test without sending anything:
    python scanner/notify.py --channel dry --message "hello"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "scanner", "config.json")


def load_config() -> dict:
    cfg: dict = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
    cfg.setdefault("tg_token", os.environ.get("TRADE_TG_TOKEN", ""))
    cfg.setdefault("tg_chat", os.environ.get("TRADE_TG_CHAT", ""))
    cfg.setdefault("imsg_to", os.environ.get("TRADE_IMSG_TO", ""))
    cfg.setdefault("sms_to", os.environ.get("TRADE_SMS_TO", ""))
    return cfg


def _recipients(cfg: dict) -> list:
    """(number, service) pairs: imsg_to -> iMessage, sms_to -> SMS relay."""
    out = [(d.strip(), "iMessage") for d in cfg.get("imsg_to", "").split(",")
           if d.strip()]
    out += [(d.strip(), "SMS") for d in cfg.get("sms_to", "").split(",")
            if d.strip()]
    return out


def send_telegram(token: str, chat: str, text: str) -> str:
    if not token or not chat:
        return "telegram: not configured (need token + chat id)"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        ok = json.load(r).get("ok", False)
    return f"telegram: {'sent' if ok else 'FAILED'}"


def mac_notify(title: str, text: str) -> str:
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{text}" with title "{title}"'],
                       check=True, timeout=15)
        return "mac banner: shown"
    except Exception as e:
        return f"mac banner: FAILED ({e})"


def _send_via(to: str, text: str, svc: str) -> str:
    """Send via Messages.app on the given service (free; SMS relay needs
    iPhone Text Message Forwarding). First run may ask for Automation
    permission in System Settings."""
    script = (f'tell application "Messages" to send "{text}" '
              f'to buddy "{to}" of (service 1 whose service type is {svc})')
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=30)
        return f"messages({svc}): sent to {to}"
    except Exception as e:
        return f"messages({svc}): FAILED to {to} ({e})"


def send_imessage(to: str, text: str) -> str:
    return _send_via(to, text, "iMessage")


def send_smart(to: str, text: str) -> str:
    """House rule: iMessage first, automatic SMS fallback on failure."""
    res = _send_via(to, text, "iMessage")
    if "FAILED" in res:
        res = _send_via(to, text, "SMS")
    return res


def run_shortcut(name: str, text: str) -> str:
    """Trigger a Mac Shortcut by name, passing the alert as input.
    Build once in Shortcuts.app: Receive Text input -> Send Message / Show
    Notification. Then alerts flow through Apple's free rails."""
    try:
        subprocess.run(["shortcuts", "run", name, "--input-text", text],
                       check=True, timeout=60)
        return f"shortcut '{name}': ran"
    except Exception as e:
        return f"shortcut '{name}': FAILED ({e})"


def alert(text: str, channels: list, cfg: dict,
          title: str = "Trade signal", only: str = "") -> list:
    """only: send solely to recipients whose name/number contains this."""
    results = []
    subs = cfg.get("subscribers", {})
    for ch in channels:
        if ch == "telegram":
            results.append(send_telegram(cfg["tg_token"], cfg["tg_chat"], text))
        elif ch == "mac":
            results.append(mac_notify(title, text[:150]))
        elif ch in ("imessage", "sms"):
            import datetime as _dt
            paused = cfg.get("paused", {})
            now = _dt.date.today().isoformat()
            numbers = sorted({d for d, _ in _recipients(cfg)})
            if only:
                o = only.lower()
                numbers = [d for d in numbers
                           if o in d or o in subs.get(d, "").lower()]
            numbers = [d for d in numbers
                       if paused.get(d, "") < now or paused.get(d, "") == ""]
            if not numbers:
                results.append("messages: not configured (need subscribers)")
            for dest in numbers:
                results.append(send_smart(dest, text))
        elif ch.startswith("shortcut:"):
            results.append(run_shortcut(ch.split(":", 1)[1], text))
        elif ch == "dry":
            results.append(f"dry: would send {len(text)} chars")
        else:
            results.append(f"unknown channel: {ch}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="dry",
                    help="comma-separated: telegram,mac,imessage,shortcut:NAME,dry")
    ap.add_argument("--message", default="test alert from trading-indicators")
    ap.add_argument("--to", default="",
                    help="send only to recipients matching this (name/number fragment)")
    a = ap.parse_args()
    for r in alert(a.message, [c.strip() for c in a.channel.split(",")],
                   load_config(), only=a.to):
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
