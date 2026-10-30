"""Stash one page of `get_option_orders` output into the audit cache.

The MCP call itself has to come from the agent (it is not an HTTP API we hold a
token for), so the loop is: call `get_option_orders`, let the oversized result
spill to a tool-result file, then

    python3 ingest_orders.py <acct> <result_file>

which copies it in as the next page and prints the date range, the filled count
and the `next` cursor to pass to the following call. Repeat until NONE.
"""
import json, os, shutil, sys

sys.path.insert(0, os.path.dirname(__file__))
from paths import ORDERS  # noqa: E402

def main(acct, src):
    d = os.path.join(ORDERS, acct)
    os.makedirs(d, exist_ok=True)
    n = len([f for f in os.listdir(d) if f.startswith("page_")]) + 1
    dst = os.path.join(d, f"page_{n:03d}.json")
    shutil.copy(src, dst)
    j = json.load(open(dst))
    o = j["data"]["orders"]
    dates = sorted(x["created_at"][:10] for x in o) or ["-", "-"]
    print(f"{acct} page {n}: {len(o)} orders {dates[0]}..{dates[-1]} "
          f"filled={sum(1 for x in o if x['state'] == 'filled')} "
          f"next={j['data'].get('next') or 'NONE'}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
