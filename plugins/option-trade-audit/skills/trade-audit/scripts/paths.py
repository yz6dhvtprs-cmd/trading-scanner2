"""Where the audit keeps its files. Everything lands in one data directory
(default ./trade-audit-data, override with TRADE_AUDIT_DIR). It holds your
order history and P&L: keep it out of version control."""
import os

CACHE = os.path.abspath(os.environ.get("TRADE_AUDIT_DIR", "trade-audit-data"))
ACCTS = [a for a in os.environ.get("TRADE_AUDIT_ACCTS", "").split(",") if a] or sorted(
    d for d in (os.listdir(CACHE) if os.path.isdir(CACHE) else [])
    if os.path.isdir(os.path.join(CACHE, d)) and d not in ("px",))
ORDERS = CACHE
PX = os.path.join(CACHE, "px")
PX_H1 = os.path.join(PX, "h1")
LOTS = os.path.join(CACHE, "lots.csv")
EPISODES = os.path.join(CACHE, "episodes.csv")
FEATURES = os.path.join(CACHE, "features.csv")
ANALYSIS_TXT = os.path.join(CACHE, "analysis_out.txt")
ANALYSIS_MD = os.path.join(CACHE, "ANALYSIS.md")
PRIORS = os.path.join(CACHE, "trade_priors.json")


def ensure():
    for d in (CACHE, PX, PX_H1):
        os.makedirs(d, exist_ok=True)
    return CACHE
