# test_routing_agent.py - checks routing_agent.py in TEST MODE: every proposal it makes is
# automatically REJECTED (never approved) and recorded. Nothing is ever approved.
#
# What it checks:
#   4. The approval gate is in place: propose_reroute can't run without an approval decision.
#      (Run first. 4a-4c are free; 4d makes one tiny API call.)
#   1. On the current data it proposes exactly one reroute (DE + Visa off ACQ_B),
#      and nothing for M003 / France or Brazil.
#   2. On a copy of the data with all three planted problems removed, it proposes nothing.
#   3. A rejected proposal never reaches the changes log.
#
# Checks 1 and 2 are full agent runs on the agent's default model (Opus): about $1 and 4 minutes in total.
# Run from this folder with the .venv activated:   python test_routing_agent.py
# To try checks 1 and 2 with another model (the agent's default is NOT changed):
#   python test_routing_agent.py --model claude-sonnet-5-5

import asyncio
import os
import re
import subprocess
import sys

import routing_agent as agent

REAL_CHANGES_LOG = agent.CHANGES_LOG   # routing_changes.log - must never change during the test

# ---- Settings you may change ----
CLEAN_DB = "transactions_clean.db"               # data with the planted problems removed
TEST_CHANGES_LOG = "routing_changes_test.log"    # where test runs WOULD write approved changes
TEST_RUNS_LOG = "routing_agent_test_runs.log"    # one record per test run (proposals + decisions)
GATE_TEST_MODEL = "claude-haiku-5-5"             # cheap model for the live gate check (4d)
# Model for the full runs (checks 1 and 2): the agent's own default unless --model is given
TEST_MODEL = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else agent.MODEL
# ---------------------------------

results = []   # (check name, passed?, details)


def check(name, passed, details=""):
    results.append((name, passed, details))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"\n       {details}" if details else ""))


def always_reject(tool_input):
    """The test's 'human': says no to every proposal."""
    print(f"  (test mode) REJECTED proposal: {tool_input.get('segment')} "
          f"{tool_input.get('from_route')} -> {tool_input.get('to_route')}")
    return False


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def propose_attempts(record):
    return [t for t in record["tools_used"] if t["tool"] == agent.PROPOSE_TOOL]


def mentions(proposal, words):
    """True if the proposal's segment/routes contain any of these whole words (e.g. 'DE', 'VISA')."""
    text = " ".join(str(proposal.get(k, "")) for k in ("segment", "from_route", "to_route")).upper()
    found = set(re.findall(r"[A-Z0-9_]+", text))
    return any(w in found for w in words)


async def main():
    # Fresh, empty test changes log; remember the real one so we can show it never changed
    open(TEST_CHANGES_LOG, "w", encoding="utf-8").close()
    real_log_before = read(REAL_CHANGES_LOG)

    # ================= 4. The approval gate is in place =================
    print("\n=== Check 4: the approval gate ===")
    options = agent.build_options()
    check("4a. propose_reroute is NOT pre-approved",
          agent.PROPOSE_TOOL not in options.allowed_tools,
          f"pre-approved tools: {options.allowed_tools}")

    gate = options.hooks.get("PreToolUse", []) if options.hooks else []
    hooked = [m for m in gate if m.matcher == agent.PROPOSE_TOOL and agent.approval_gate in m.hooks]
    check("4b. A PreToolUse approval hook is attached to propose_reroute, and blocks if it fails",
          bool(hooked) and hooked[0].on_failure == "block")

    # Call the tool directly, skipping the gate entirely: it must refuse and write nothing.
    agent.CHANGES_LOG = TEST_CHANGES_LOG
    agent.approved_waiting.clear()
    reply = await agent.propose_reroute.handler({
        "segment": "DE + Visa", "from_route": "ACQ_B", "to_route": "ACQ_A",
        "evidence": "direct call with no approval", "expected_recovered_declines": 1,
        "expected_recovered_usd": 1})
    check("4c. Called directly without an approval decision, propose_reroute refuses and writes nothing",
          reply.get("is_error") is True and read(TEST_CHANGES_LOG) == "",
          reply["content"][0]["text"])

    # Live check: pre-approve the tool AND switch off all permission prompts ("bypassPermissions").
    # The old can_use_tool gate would be skipped here; the hook must still stop it.
    print("  Live gate check (small model, permissions bypassed on purpose)...")
    rec = await agent.run_agent(
        decider=always_reject, changes_log=TEST_CHANGES_LOG, runs_log=TEST_RUNS_LOG,
        mode="test - gate check, permissions bypassed", quiet=True,
        task=("This is a test of the approval step. Call propose_reroute exactly once with segment "
              "'TEST + Visa', from_route 'ACQ_X', to_route 'ACQ_Y', evidence 'gate test', "
              "expected_recovered_declines 0, expected_recovered_usd 0. Call no other tool. "
              "Then report what happened in one sentence."),
        model=GATE_TEST_MODEL, effort="low", max_steps=4, max_spend=0.25,
        allowed_tools=agent.READ_TOOLS + [agent.PROPOSE_TOOL], permission_mode="bypassPermissions")
    attempts = propose_attempts(rec)
    check("4d. Even with the tool pre-approved and permissions bypassed, the hook asked for a decision "
          "on every attempt, and nothing was written",
          len(attempts) >= 1 and len(rec["proposals"]) == len(attempts) and read(TEST_CHANGES_LOG) == "",
          f"attempts: {len(attempts)}, decisions recorded: {len(rec['proposals'])}, "
          f"cost ${rec['total_cost_usd']}")

    # ================= 1. Current data =================
    print(f"\n=== Check 1: current data (full run, {TEST_MODEL}) ===")
    rec1 = await agent.run_agent(decider=always_reject, db_file="transactions.db",
                                 changes_log=TEST_CHANGES_LOG, runs_log=TEST_RUNS_LOG,
                                 mode="test - auto-reject, current data", quiet=True,
                                 model=TEST_MODEL)
    props = rec1["proposals"]
    print(f"  run ended: {rec1['how_it_ended']}, {rec1['steps']} steps, ${rec1['total_cost_usd']}")
    for p in props:
        print(f"  proposal: {p['segment']} | {p['from_route']} -> {p['to_route']} | {p['decision']}")
    one_right = (len(props) == 1 and mentions(props[0], ["DE"]) and mentions(props[0], ["VISA"])
                 and props[0]["from_route"].strip().upper() == "ACQ_B")
    check("1a. Exactly one proposal: DE + Visa off ACQ_B", one_right,
          f"{len(props)} proposal(s)")
    wrong = [p for p in props if mentions(p, ["M003", "FR", "FRANCE", "BR", "BRAZIL"])]
    check("1b. Nothing proposed for M003 / France or Brazil", not wrong,
          f"{len(wrong)} wrong proposal(s)" if wrong else "")
    check("1c. Every propose_reroute attempt got a recorded decision, all rejected",
          len(propose_attempts(rec1)) == len(props) and all(p["decision"] == "rejected" for p in props),
          f"attempts: {len(propose_attempts(rec1))}, decisions: {len(props)}")

    # ================= 2. Clean data =================
    print(f"\n=== Check 2: data with the planted problems removed (full run, {TEST_MODEL}) ===")
    if not os.path.exists(CLEAN_DB):
        subprocess.run([sys.executable, "generate_data.py", "--clean", CLEAN_DB], check=True)
    rec2 = await agent.run_agent(decider=always_reject, db_file=CLEAN_DB,
                                 changes_log=TEST_CHANGES_LOG, runs_log=TEST_RUNS_LOG,
                                 mode="test - auto-reject, clean data", quiet=True,
                                 model=TEST_MODEL)
    print(f"  run ended: {rec2['how_it_ended']}, {rec2['steps']} steps, ${rec2['total_cost_usd']}")
    for p in rec2["proposals"]:
        print(f"  proposal: {p['segment']} | {p['from_route']} -> {p['to_route']} | {p['decision']}")
    check("2.  No proposals on clean data",
          len(rec2["proposals"]) == 0 and len(propose_attempts(rec2)) == 0,
          f"{len(rec2['proposals'])} proposal(s)")

    # ================= 3. Rejections never reach the log =================
    print("\n=== Check 3: rejected proposals never logged ===")
    rejected = sum(p["decision"] == "rejected" for r in (rec, rec1, rec2) for p in r["proposals"])
    check("3.  Rejected proposals never reached a changes log",
          rejected >= 1 and read(TEST_CHANGES_LOG) == "" and read(REAL_CHANGES_LOG) == real_log_before,
          f"{rejected} proposal(s) rejected; test changes log empty: {read(TEST_CHANGES_LOG) == ''}; "
          f"real routing_changes.log unchanged: {read(REAL_CHANGES_LOG) == real_log_before}")

    # ================= Summary =================
    passed = sum(ok for _, ok, _ in results)
    cost = sum((r["total_cost_usd"] or 0) for r in (rec, rec1, rec2))
    minutes = sum(r["duration_seconds"] for r in (rec, rec1, rec2)) / 60
    print("\n" + "-" * 70)
    print(f"RESULT ({TEST_MODEL}): {passed}/{len(results)} checks passed | API cost about ${cost:.2f} | "
          f"time {minutes:.1f} min | "
          f"run records in {TEST_RUNS_LOG}")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
