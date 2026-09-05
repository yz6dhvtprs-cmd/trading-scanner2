# Universe methodology v1

## Rule (deterministic, re-runnable)

1. Start from US-listed common stocks (no ETFs, no ADRs, no warrants).
2. Rank by market capitalization (shares outstanding × price, snapshot date recorded).
3. Liquidity filter: keep only names whose 90-day median daily dollar volume
   (close × volume) is in the top quartile of the pre-filter list.
4. Take the top 50 of what remains.
5. Hard exclusion: remove $META if present, promote rank #51.
6. Rebalance quarterly (Jan/Apr/Jul/Oct). Version each snapshot: `universe_YYYY-QN.csv`.

## Why this shape

- Cap rank gives the "top 50 by market cap" leg.
- Dollar-volume filter gives the "liquidity" leg (tradeable spreads, options availability).
- META exclusion is a hard constraint, applied AFTER ranking so the universe stays at 50.

## Refresh (required — the shipped CSV is a template)

```
pip install pandas yfinance
python universe/fetch_universe.py --out universe/universe_live.csv
```

The script records `snapshot_date`, `source`, and per-ticker `market_cap`
and `dollar_vol_90d` so any snapshot is auditable. Never trade off
`universe_v1.csv` — it is a structural template (see header row).
