# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two prompt-engineering experiments on payments data, each with its own eval harness. Plain Python scripts (no package, no build, no linter); they call the Anthropic API via `anthropic` + `python-dotenv`, with the API key in `.env`. Scripts use relative paths, so run them from this folder with the `.venv` activated (`.venv\Scripts\Activate.ps1`).

The user is iterating on **prompts**, not code: the usual loop is edit a prompt file → rerun its eval → compare the log line. Keep scripts beginner-readable (they are heavily commented in plain language on purpose); user-tunable settings live in a `# ---- Settings you may change ----` block at the top of each script.

## Working with me

- I'm not a programmer. Before making any change, explain in plain language what you'll change and why. Ask before big changes (new files, restructuring a script, changing the test data or answer keys, anything that alters how scoring works).
- This is a portfolio project on synthetic data only. Never bring in, request, or use real or employer data.
- After any change, run the matching test and show me the actual result (score/report output), instead of just saying it's done: `score.py <prompt>` for Part A changes, `test_analyst.py` for Part B changes. These cost API money, so say which test you're running.

## Skills

- `exec-summary` (`.claude/skills/exec-summary/`): runs a fresh analyst investigation, re-checks the numbers against transactions.db, and builds a one-page PDF for a payments VP in `reports/` via `build_pdf.py` (needs matplotlib in `.venv`).

## Commands

```powershell
python test.py                     # smoke test: API key works
python score.py prompt_v5.txt      # Part A eval for one prompt version
python generate_data.py            # rebuild transactions.db (deterministic, seed 42)
python analyst.py                  # Part B interactive chat (type quit to exit)
python test_analyst.py             # Part B regression test (all questions in test_questions.csv)
```

There is no single-test flag; to run one case, temporarily trim `declines.csv` / `test_questions.csv` or call `analyst.ask("...")` from a Python shell. Every eval run makes real (billed) API calls.

## Part A — decline classifier

- `score.py <prompt file>` sends each row of `declines.csv` (answer columns withheld) to Claude with that prompt as the system prompt, parses the first `{...}` JSON in the reply, and compares `category` to `correct_category`.
- Outputs: `results_<prompt name>.csv` (overwritten per prompt version) and one appended line in `score_log.csv`.
- `prompt_v1..v5.txt` are successive versions; v5 is the current best (40/40). Prompts must keep the JSON output contract `{"category", "confidence", "reason"}` and the five category names, or scoring breaks.
- `declines - Original 30.csv` is the earlier 30-row exam, kept for reference; `declines.csv` (40 rows, `difficulty` easy/hard) is what's scored. CSVs are edited in Excel, hence the utf-8-sig/cp1252 fallback in `read_rows`.

## Part B — approval-rate analyst

- `generate_data.py` → `transactions.db` (single `transactions` table, ~60 days ending 2026-11-18). It plants three hidden problems in its "hidden problems" block; `test_questions.csv` expected answers depend on them, so changing the generator (or the END date) invalidates the test answers.
- `analyst.py` is a manual tool-use loop (`ask()`, max `MAX_STEPS`) with three tools: `describe_table`, `run_sql` (SELECT/WITH only, DB opened read-only via `mode=ro` URI), and `scan_anomalies` (Python-side z-score scan over every window ≤ `SCAN_MAX_WINDOW_DAYS` for country×brand, country×brand×route, country×merchant; also reports top-rising code and `still_active` from the last 3 days). Behavior rules for the analyst are in `analyst_prompt.txt`, not in code.
- `test_analyst.py` imports `analyst` (so it uses the same prompt, tools, model and client), asks each question, and has a Haiku grader return PASS/FAIL JSON vs `expected_answer`. Outputs: `test_results.csv` (overwritten) and an appended line in `test_log.csv` (no header: date, prompt file, score, tokens in, tokens out).
- `analyst.ask()` returns `(answer, tokens_in, tokens_out)`; `test_analyst.py` relies on that signature. Code under `if __name__ == "__main__"` in `analyst.py` must stay there so importing it doesn't start the chat.
