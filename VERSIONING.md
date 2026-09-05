# Versioning with git + GitHub (free)

I could not run git from this session (the git call was approval-blocked),
so history starts with you pasting the block below. After that, every change
I make can be committed as a version and rolled back with `git log` / `git revert`.

## First-time setup (paste once)

```bash
cd /Users/vismaypatel/trading-indicators
git init
git add README.md universe/ strategies/ grading.md skill.md scanner/ \
        backtest/ review/ INDICATORS.md VERSIONING.md .gitignore
git commit -m "v0.1: universe, 3 strategies, backtest, skill, review loop"
```

Create an empty FREE public repo on github.com (e.g. `trading-indicators`),
then:

```bash
git branch -M main
git remote add origin https://github.com/<you>/trading-indicators.git
git push -u origin main
```

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
