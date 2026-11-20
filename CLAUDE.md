# Builders Buildathon 2026 — Team Playbook (Anurag & Tanush)

## Project Overview
- **Competition:** Builders Buildathon (automated scoring on unseen test cases; no pitch or judges).
- **Hard Deadline:** Sunday, Nov 22, 2026 at 12:00 PM ET (feature freeze at 9:00 AM ET).
- **Track:** [FILL IN AT 6 PM ET — RWX / Builders / Machina / Databricks]
- **Scoring Rule:** [FILL IN AT 6 PM ET — paste exact metric formula and tie-breakers]

## Hard Constraints (Verify Before Every Submission)
- **Runtime limit:** [FILL IN AT 6 PM ET]
- **Memory / API / model limits:** [FILL IN AT 6 PM ET]
- **Required output format:** [FILL IN AT 6 PM ET]
- Never hardcode answers from sample test cases or modify validation/test datasets.
- Never read, print, or commit secrets or `.env` files.

## Commands & Verification
- **Activate Mac virtual env:** `source .venv/bin/activate`
- **Run solution:** `python main.py`
- **Run local evaluation:** `python eval.py` (update this filename if the challenge kit provides its own test script)
- Always run the local evaluation command after code edits to confirm the score and verify nothing broke before marking a task done.

## Repo Layout
- `main.py` — Core solution code.
- `eval.py` — Local scoring & error-slicing script mirroring the official metric.
- `experiments.csv` — Log of every tested version (`version,commit_hash,owner,hypothesis_or_change,local_score,leaderboard_score,runtime_sec,keep_or_revert`).
- `requirements.txt` — Keep updated whenever a new Python package is installed.

## Working Style & Team Rules
- We are a two-person team newer to Git/GitHub. Explain what changed and why in 2–4 plain-English bullet points after edits.
- Before editing multiple files or making big architectural changes, outline a short step-by-step plan and wait for confirmation.
- Make one targeted change at a time so we can measure whether it improved the score.
- Do not run `git commit`, `git push`, or destructive git commands on our behalf—we handle commits and syncing manually through Cursor's Source Control tab.
- Keep `CLAUDE.md` under 200 lines; if we correct the same mistake twice, propose a one-line rule to add here.