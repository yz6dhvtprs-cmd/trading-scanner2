# Versioning with git + GitHub (free)

Status: repo initialized, `v0.1` committed (`1e32c3e`). Apple's git is broken
on this machine (no Xcode CLI tools), so we version with `dulwich`
(pure-Python git, in `.venv`): `python -m dulwich <cmd>`.

## Push to GitHub (one-time, needs you)

Create an empty FREE public repo on github.com (e.g. `trading-indicators`),
then:

```bash
cd /Users/vismaypatel/trading-indicators
.venv/bin/python -m dulwich push https://github.com/<you>/trading-indicators.git
```

(Use a personal access token as password when prompted.)

## Ongoing convention

- One commit per step: `git add <files I changed>` + short message
  (`v0.2: programmable exits + target sweep`, `v0.3: ADX filter`, …).
- Experiments on branches (`git checkout -b exp/adx-filter`); merge only
  after TEST-confirmed expectancy. Nothing that fails validation reaches main.
- Tag validated states: `git tag -a v0.2-tested -m "retest-long 3R confirmed"`.
- Go back anytime: `git log --oneline` to find the version,
  `git revert <commit>` to undo it (safe — never rewrites history).

## What is versioned

Code, specs, skill, rubric, loop, notes, universe snapshots, results CSVs,
Saturday reports. NOT versioned (see .gitignore): `.venv/`, `__pycache__/`.
