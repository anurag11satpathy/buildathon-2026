# Builders Buildathon 2026 — RWX: Blast Radius (Anurag & Tanush)

## Project Overview
- **Goal:** Given standalone unified diffs against `sqlglot` (pinned at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), select which pytest tests to run and assign them across up to 8 workers.
- **Metric (Lower is Better):** `sum(busiest_worker_sec + 2 * num_workers + 300 * missed_flag) + program_CPU_sec`.
- **Baseline to Beat:** 1614.1 s (starter runs all tests across 3 workers).
- **Hard Deadline:** Sunday, Nov 22, 2026 at 11:00 AM ET (feature freeze 9:00 AM ET). Final tiebreaker: within 1.0 s counts as tied, earlier submission wins.

## Strict Submission & Runtime Rules (Never Violate)
- **Inputs at runtime:** `/data/commits/<id>.patch` and `/data/tests.json`.
- **Output format:** Print EXACTLY one JSON line per commit to `stdout`: `{"commit": "k7q2", "workers": [[12, 845], [3, 77]]}`. Use `"workers": []` to run nothing (0 s cost).
- **Stdout rule:** ALL logs/debug prints MUST go to `stderr`. Any extra `stdout` or > 2 MB `stdout` rejects the run.
- **Validation rules:** Max 8 workers per commit, no duplicate test IDs within a commit, only valid test IDs from `/data/tests.json`.
- **Hardware limits (Run):** 2 cores, 4 GB RAM, NO network, read-only root disk, 64 MB empty `/tmp`, user `nobody`, max 256 processes, 3 min batch cap (4.5 min in final).
- **Hardware limits (Build):** 4 cores, 8 GB RAM, 10 min cap. Only download from PyPI, npm, Docker Hub, fixed-tag GitHub releases, or `sqlglot@eb11aec`.
- **Repo limit:** Public GitHub repo max **20 MB** total. No Git LFS or submodules. Commit precomputed maps as compact plain files so Docker builds in seconds.
- **Anti-cheat rule:** NO lookup tables mapping patches or edit types to broken tests. Static/dynamic maps built from pinned `sqlglot@eb11aec` (per-test coverage, import graphs, AST/symbol maps, fixtures) ARE allowed.
- **Known traps:** A comment-only change is NOT promised to be harmless. Final includes unseen change types. A miss costs +300 s, so skew conservative when uncertain and pack fast borderline tests into idle worker slack for free.

## Repo Layout & Commands
- `selector.py` — Main solution code.
- `Dockerfile` — Container build recipe at repo root.
- `Makefile` & `score.py` — Local evaluation harness (`make sample` runs Docker on the 30 sample commits in `sample/`).
- `SCORING.md` — Official scoring specification.
- `experiments.csv` — Log of every tested version (`version,commit_hash,owner,hypothesis_or_change,local_score,leaderboard_score,missed_commits,runtime_sec,keep_or_revert`).
- **Always run `make sample`** after edits to verify local score, 0 missed commits, and low CPU time before finishing a task.

## Team Working Rules
- We are a two-person team newer to Git/GitHub. Explain what changed and why in 2–4 plain-English bullet points after edits.
- Before editing multiple files or making big architectural changes, outline a short step-by-step plan and wait for our confirmation.
- Make one targeted change at a time so we can measure whether it improved the score.
- NEVER run `git commit`, `git push`, or destructive git commands—we handle Git manually in Cursor's Source Control tab.

## Current State (Handoff to Tanush)
- **Built & Verified:** 1–8 worker bin-packing + `data/map.json.gz` (0.32 MB) + `selector.py` safety fallbacks.
- **Current Scores:** 329.5 s local (`make sample`, 0 misses) | **639.8 s live leaderboard** (0.44 s CPU, **1 missed commit** = ~339.3 s actual test time).
- **Priority #1:** Find and eliminate the 1 hidden missed commit (-300 s) + add free slack-packing into idle worker time.
- **Priority #2:** Tighten the module-level rule for `4d63`, `ktz3`, `6ze8`, and `gbav` (~145 s of our 329.5 s local cost) without adding misses.