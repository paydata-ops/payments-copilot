# Payments Copilot

**AI assistants for card-payment operations: explain why payments are declined, find where approval rates dropped, and propose a fix that a person must approve.**

Built by a payments analytics lead with 9+ years in acquiring.

Built with Claude (Anthropic API and Claude Agent SDK) on fully synthetic payment data.

---

## The problem

When card payments start failing, a payments team has to answer three questions fast:

1. **Why was this payment declined?** Response codes are ambiguous. The same code can mean an issuer decision, a fraud rule, a 3DS failure or a technical fault.
2. **Where are we losing approvals, and how much is it costing?** A problem on one acquirer route, for one card brand, in one country, can hide inside healthy overall numbers.
3. **What should we do about it?** Moving traffic to another acquirer route fixes some problems and does nothing for others. The decision needs evidence, and a person should make it.

## What I built

| Part | What it does | Main files |
|---|---|---|
| **A. Decline classifier** | Labels a declined payment as one of five causes (`issuer_decline`, `fraud_rule`, `3ds_failure`, `routing_issue`, `insufficient_funds`) and gives a reason. | `prompt_v1–v5.txt`, `score.py` |
| **B. Approval-rate analyst** | Answers questions like "Which route caused the German Visa drop?" by querying the data itself, sizing the impact in $, and following written analyst rules. | `analyst.py`, `analyst_prompt.txt` |
| **MCP server** | Shares the analyst's three read-only data tools (and its rules) with Claude Desktop or any MCP client. | `payments_mcp.py` |
| **C. Routing agent** | Investigates the data on its own. It proposes an acquirer reroute **only** when the route itself is the cause, and flags everything else for a person. Nothing is executed. | `routing_agent.py` |
| **Exec summary** | A Claude Code skill that turns a fresh investigation into a one-page PDF for a payments VP. In the sample report, the actions, owners and dates in Next steps are illustrative. | `.claude/skills/exec-summary/`, `reports/` |

Each part has its own **automated test**. I improved the prompts by changing one thing at a time and rerunning the test.

## Results

All numbers below come from the project's logs and test results.

### A. Decline classifier: hard cases 13/18 → 18/18

The exam is `declines.csv`: 40 labeled declines, 18 of them marked hard. From `score_log.csv`:

| Prompt | What changed | Hard cases passed (of 18) |
|---|---|---|
| v1 | Plain instructions | 13 |
| v3 | Category definitions, plus rules for code 59, 3DS failures and codes 51/61 | 14–15 over 3 runs |
| v4 | Rule for code 1A (issuer asks for authentication) | 17 |
| **v5** | Rule for code 91 when 3DS is unavailable | **18, on 2 runs in a row** |

v2 was only scored on an earlier, smaller exam, so it's not shown.

### B. Approval-rate analyst: 5/5 on the regression test

`test_analyst.py` asks 5 questions about the data. A separate, smaller model (Claude Haiku) grades each answer against an answer key. From `test_log.csv`, the five runs while I tuned the analyst rules scored **3/5 → 5/5 → 4/5 → 5/5 → 5/5**. The latest run (`test_results.csv`) passed all five:

| Question | Expected | Result |
|---|---|---|
| Which route caused the German Visa drop? | ACQ_B, from 12 Nov, code 91 | PASS |
| Where did M003 experience a spike in declines? | France | PASS |
| How many issues are ongoing? | 2 | PASS |
| Which brand saw increased decline in Brazil? | Mastercard | PASS |
| Were the main issues caused by an increase in 05 declines? | No | PASS |

### C. Routing agent: found all 3 planted problems, acted only on the route problem

In a full run on 21 Nov (`routing_agent_runs.log`: 26 steps, $0.41, 260 seconds), the agent found all three problems planted in the data. It proposed a fix only for the one the route caused. It also flagged a fourth finding for review instead of acting on it. That finding was random variation, a false alarm.

| Finding | Planted? | Agent's decision | Impact (agent's figures) |
|---|---|---|---|
| DE + Visa on route ACQ_B: codes 91/96 (network errors), still running | Yes | **Proposed reroute ACQ_B → ACQ_A.** Approved by a person, recorded as a simulation only. | 72.2 excess declines, about $4,646 over 7 days (about $664/day) |
| FR, merchant M003: 3DS failures, still running | Yes | Flagged for human review (merchant 3DS setup, not the route) | 58.5 excess declines, about $3,718 |
| BR + Mastercard: more code 51 declines, 10-11 to 10-24, ended | Yes | Flagged for human review (cardholders, and already over) | 45 excess declines, about $3,195 |
| FR + Mastercard: more code 05 declines | **No, a false alarm** (random variation) | Flagged for human review; no action taken | 23.8 excess declines, about $1,711 |

### C. Routing agent: proposes nothing when nothing is wrong

On a copy of the data with the three planted problems removed, the agent **proposed nothing**, in all 4 test runs (2 on Opus, 2 on Sonnet). This is the test that shows it can be trusted: it doesn't invent reasons to act.

### C. Routing agent: automated safety test, 9/9 on every run

`test_routing_agent.py` rejects every proposal automatically and checks that:
- the approval gate can't be bypassed;
- on the real data the agent proposes exactly one reroute (DE + Visa off ACQ_B);
- on the clean data it proposes nothing;
- a rejected proposal never reaches the changes log.

From `routing_agent_test_runs.log` and the test output:

| Model | Checks passed | Cost per test run | Time per test run |
|---|---|---|---|
| Claude Opus 5.5 | 9/9, 9/9 | $0.72, $0.87 | 3.1 min, 3.6 min |
| Claude Sonnet 5.5 | 9/9, 9/9 | $0.27, $0.25 | 2.2 min, 2.1 min |

Sonnet passed every check at about a third of Opus's cost.

## How it's kept safe

- **Synthetic data only.** `generate_data.py` creates the whole dataset from a fixed random seed: 59,960 card payments from 2026-09-20 to 2026-11-18, with three planted problems for the tools to find. No real or employer data is used anywhere, and the database is not stored in this repository; you recreate it with one command.
- **Read-only data access.** The database is opened read-only, and the SQL tool only accepts `SELECT`/`WITH` queries.
- **The routing agent can't change anything:**
  - All of Claude's built-in tools (shell, file editing, web) are switched off. It can read data through three tools and propose a change through one.
  - A proposal is only a **simulation**. If approved, it writes one line to a log file.
  - **Human approval is enforced in code.** A `PreToolUse` hook asks a person before every proposal, and it still runs if someone bypasses permission settings. The proposal tool itself also refuses to write anything without a recorded approval. The reject and bypass cases are covered by the automated test. The approve path was tested by hand on the current version: proposals approved on 23 Nov reached the changes log as simulations.
  - Every proposal must state what the agent *can't* see: the target route's fees, capacity and contract terms.
  - Each run is capped at 30 steps and $2, and is written to a run log.
- **No secrets in the repository.** The API key lives in a local `.env` file, which is excluded from version control.

## How to run it

You need Python 3.13 and an Anthropic API key. Every test makes real, billed API calls.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
"ANTHROPIC_API_KEY=your-key-here" | Out-File -Encoding ascii .env
python generate_data.py            # creates transactions.db (same data every time)
```

| To do this | Run |
|---|---|
| Check the API key works | `python test.py` |
| Score a classifier prompt | `python score.py prompt_v5.txt` |
| Chat with the analyst | `python analyst.py` (type `quit` to exit) |
| Run the analyst regression test | `python test_analyst.py` |
| Run the routing agent (asks you y/n on each proposal) | `python routing_agent.py` |
| Run the routing agent safety test | `python test_routing_agent.py` (add `--model claude-sonnet-5-5` to try Sonnet) |
| Use the data tools from Claude Desktop | add `payments_mcp.py` as a local MCP server |

## For engineers

- **Prompts are the product.** Behavior lives in text files (`prompt_v*.txt`, `analyst_prompt.txt`), not code. The usual loop is: edit a prompt, rerun its test, compare the log line.
- **Classifier scoring.** `score.py` hides the answer columns, parses the first JSON object in each reply, and compares `category` to the key. It appends to `score_log.csv` and writes `results_<prompt>.csv`.
- **Analyst.** `analyst.py` is a hand-written tool-use loop (max 15 steps) with three tools:
  - `describe_table`, which also returns the analyst rules;
  - `run_sql`, read-only;
  - `scan_anomalies`, a z-score sweep over every window of up to 21 days for country × brand, country × brand × route and country × merchant. It skips segments under 30 payments a day and marks whether each finding is still active.
- **Grading.** `test_analyst.py` uses a Haiku grader that returns PASS/FAIL JSON against the answer key.
- **Routing agent.** Built on the Claude Agent SDK. Its data comes through the same MCP server (`payments_mcp.py --db <file>`). `generate_data.py --clean <file>` builds a copy with no planted problems, used as the agent's "nothing to do" test.

## Next steps

- **Test the approve path automatically.** Today the automated test only rejects; approving has been tested by hand.
- **Check the two newest rules in the test.** "One time window" and "state what you can't see" are in the agent's instructions, but the test doesn't grade them yet.
- **Bigger, repeated evaluations.** The analyst test has 5 questions, and each routing-agent model was run twice. More questions and more runs would show how much results vary. They would also show how often the agent raises false alarms like the FR + Mastercard one.
- **Choose the routing agent's model.** Sonnet matched Opus on every check at about a third of the cost. A few more runs would settle it.
