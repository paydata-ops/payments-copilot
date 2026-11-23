# routing_agent.py - an agent that looks for approval-rate problems and, ONLY when
# the acquirer route itself is the cause, proposes moving that traffic to another route.
#
# Built with the Claude Agent SDK (pip install claude-agent-sdk).
#
# Safety, in plain words:
#   - Data access only through the existing payments-mcp server (payments_mcp.py),
#     which opens transactions.db read-only.
#   - ALL built-in tools are switched off (no Bash, no file editing, no web).
#     The only extra tool is propose_reroute, defined in this file.
#   - propose_reroute never changes anything real. If you approve, it writes one
#     "SIMULATION" line to routing_changes.log. That's all.
#   - The approval gate is a PreToolUse HOOK. A hook runs before every call of
#     propose_reroute no matter what the permission settings say, so even if someone
#     later pre-approves the tool or switches to "bypass permissions" mode, you are
#     still asked. If the hook itself crashes, the call is blocked.
#   - Second lock: propose_reroute itself refuses to write anything unless the hook
#     has just recorded YOUR approval for that exact proposal.
#   - Each run stops at MAX_STEPS steps or MAX_SPEND_USD dollars.
#   - Every run is written to routing_agent_runs.log.
#
# Run it from this folder with the .venv activated:   python routing_agent.py
# test_routing_agent.py runs it in a test mode where every proposal is rejected.

import asyncio
import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, SystemMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server, query, tool,
)

# ---- Settings you may change ----
MODEL = "claude-opus-5-5"
EFFORT = "high"            # how hard the model thinks: low / medium / high / xhigh / max
MAX_STEPS = 30             # most back-and-forth turns the agent may take in one run
MAX_SPEND_USD = 2.00       # stop the run when the estimated cost reaches this (can slightly overshoot)
DB_FILE = "transactions.db"              # the data the agent reads
CHANGES_LOG = "routing_changes.log"      # approved (simulated) routing changes
RUNS_LOG = "routing_agent_runs.log"      # one record per run
TASK = ("Investigate the last 60 days of card payments for approval-rate problems. "
        "For each problem, decide whether the acquirer route itself is the cause. "
        "Propose a reroute only for route-caused problems; flag everything else for a person.")
# ---------------------------------

load_dotenv()   # puts ANTHROPIC_API_KEY from .env into the environment
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)

# The agent's standing instructions. The analyst rules themselves come from
# analyst_prompt.txt, which payments-mcp hands over when describe_table is called.
SYSTEM_PROMPT = """You are a payments routing analyst. You can read card-payment data through the
payments-mcp tools and you can PROPOSE (never execute) a routing change with propose_reroute.

Start with describe_table and follow the ANALYST RULES it returns. Then use scan_anomalies and confirm
every finding with run_sql. Every number you state must come from a query.

When is a reroute allowed? Only when the acquirer route itself is the cause. ALL of these must be true,
each shown with a run_sql result:
  1. The problem is on one route: the same country and brand on the other routes is at its normal rate.
  2. The decline codes that rose point to the acquirer or network (for example 91 issuer/switch
     unavailable, 96 system error), not to the cardholder or issuer's decision (51, 05, 61...),
     and not to 3DS / authentication failures.
  3. The problem is still active in the most recent days (rerouting a finished problem recovers nothing).
  4. The route you move traffic to is healthy for that same segment right now.
If any of these is not true, do NOT call propose_reroute. Instead write "FLAG FOR HUMAN REVIEW", the
segment, the likely cause, the evidence and the $ impact.

Use ONE time window, consistently. Find the exact date the change started (from daily data) and use
that date as the boundary in every query and number for that problem: "before" is everything before
that date, "during" is that date onward. Never mix windows (for example one query from 11-11 and another
from 11-12, or a "last 7 days" next to a "since the start" figure). State the start date in the evidence.

For each reroute, call propose_reroute once, with:
  - expected_recovered_declines and expected_recovered_usd sized as the excess declines (and their $)
    over the segment's normal rate during the problem window, per day, and say per day in the evidence.
  - In the evidence, a part headed "What I can't see:" saying that the data does not show the target
    route's fees, capacity or contract terms, so a person must check those before any real change.
If a proposal is rejected, do not propose it again; list it as rejected in your summary.

Finish with a short summary in two parts: "Proposed reroutes" (with the decision you were told) and
"Flagged for human review"."""


# What the current run did, for the run log. run_agent() resets these at the start of each run.
run = {"tools_used": [], "proposals": []}
approved_waiting = []   # proposals you approved that propose_reroute hasn't written yet


def proposal_key(p):
    """What identifies one proposal: which traffic, from where, to where."""
    return (p.get("segment"), p.get("from_route"), p.get("to_route"))


# ---- The one new tool: propose_reroute ----
PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "segment": {"type": "string", "description": "Traffic to move, e.g. 'DE + Visa'"},
        "from_route": {"type": "string", "description": "Route the traffic is on now, e.g. 'ACQ_B'"},
        "to_route": {"type": "string", "description": "Route to move it to, e.g. 'ACQ_A'"},
        "evidence": {"type": "string", "description": "Why the route itself is the cause, with the query numbers"},
        "expected_recovered_declines": {"type": "number", "description": "Declines expected to be recovered per day"},
        "expected_recovered_usd": {"type": "number", "description": "USD expected to be recovered per day"},
    },
    "required": ["segment", "from_route", "to_route", "evidence",
                 "expected_recovered_declines", "expected_recovered_usd"],
}


@tool("propose_reroute",
      "Records a PROPOSED routing change for a segment whose acquirer route is itself the cause of "
      "declines. A person must approve it first. Nothing is executed: an approved proposal is only "
      "written to a log as a simulation. Never use it for issuer, cardholder, 3DS or merchant problems.",
      PROPOSE_SCHEMA)
async def propose_reroute(args):
    # Second lock: write NOTHING unless the approval hook recorded a "yes" for this proposal.
    key = proposal_key(args)
    if key not in approved_waiting:
        return {"content": [{"type": "text", "text":
                "Refused: there is no human approval for this proposal. Nothing was recorded."}],
                "is_error": True}
    approved_waiting.remove(key)   # one approval = one log line

    line = {"time": datetime.now().isoformat(timespec="seconds"),
            "status": "SIMULATION - approved, not executed", **args}
    with open(CHANGES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return {"content": [{"type": "text", "text":
            f"Approved by the human. Recorded as a SIMULATION in {CHANGES_LOG}. No real routing was changed."}]}


routing_tools = create_sdk_mcp_server(name="routing", tools=[propose_reroute])

# Tool names as the agent sees them: mcp__<server>__<tool>
READ_TOOLS = ["mcp__payments-mcp__describe_table",
              "mcp__payments-mcp__run_sql",
              "mcp__payments-mcp__scan_anomalies"]
PROPOSE_TOOL = "mcp__routing__propose_reroute"


# ---- Human approval ----
def ask_human(tool_input):
    """Shows the proposal and waits for you to type y or n. Returns True only for y."""
    print("\n" + "=" * 70)
    print("PROPOSED ROUTING CHANGE (simulation only - nothing real will change)")
    print(f"  Segment:            {tool_input.get('segment')}")
    print(f"  Move from route:    {tool_input.get('from_route')}")
    print(f"  Move to route:      {tool_input.get('to_route')}")
    print(f"  Recovered declines: {tool_input.get('expected_recovered_declines')} per day")
    print(f"  Recovered $:        {tool_input.get('expected_recovered_usd')} per day")
    print(f"  Evidence:           {tool_input.get('evidence')}")
    print("=" * 70)
    return input("Approve? (y/n): ").strip().lower() == "y"


decide = ask_human   # who makes the approval decision; the test swaps in "always reject"


async def approval_gate(hook_input, tool_use_id, context):
    """PreToolUse hook: runs before EVERY propose_reroute call, whatever the permission settings."""
    tool_input = hook_input["tool_input"]
    # The decision may wait for your typing; a side thread keeps the agent connection alive meanwhile
    approved = bool(await asyncio.to_thread(decide, tool_input))
    run["proposals"].append({**tool_input, "decision": "approved" if approved else "rejected"})

    if approved:
        approved_waiting.append(proposal_key(tool_input))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "allow",
                                       "permissionDecisionReason": "Approved by the human."}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason":
                                       "The human REJECTED this proposal. Do not propose it again; "
                                       "list it as rejected in your summary."}}


# Backstop for anything the permission system still asks about: every tool other than
# the 3 read-only ones is refused, and propose_reroute only passes if the hook approved it.
async def ask_permission(tool_name, tool_input, context):
    if tool_name == PROPOSE_TOOL and proposal_key(tool_input) in approved_waiting:
        return PermissionResultAllow(updated_input=tool_input)
    return PermissionResultDeny(message=f"{tool_name} is not allowed here without human approval.")


# ---- The agent's settings ----
def build_options(model=MODEL, effort=EFFORT, max_steps=MAX_STEPS, max_spend=MAX_SPEND_USD,
                  allowed_tools=None, permission_mode="default"):
    return ClaudeAgentOptions(
        model=model,
        effort=effort,
        system_prompt=SYSTEM_PROMPT,
        tools=[],                     # switch off ALL built-in tools (Bash, Read, Edit, Web...)
        mcp_servers={
            "payments-mcp": {"type": "stdio", "command": sys.executable,
                             "args": [os.path.join(PROJECT_DIR, "payments_mcp.py"), "--db", DB_FILE]},
            "routing": routing_tools,
        },
        strict_mcp_config=True,       # use ONLY the two servers above, ignore any other MCP config
        setting_sources=[],           # ignore personal/project Claude settings and CLAUDE.md files
        allowed_tools=READ_TOOLS if allowed_tools is None else allowed_tools,   # these run without asking
        permission_mode=permission_mode,
        can_use_tool=ask_permission,
        # THE approval gate: runs before every propose_reroute call; if it fails, the call is blocked
        hooks={"PreToolUse": [HookMatcher(matcher=PROPOSE_TOOL, hooks=[approval_gate],
                                          on_failure="block", timeout=3600)]},
        max_turns=max_steps,
        max_budget_usd=max_spend,
        cwd=PROJECT_DIR,
    )


# can_use_tool needs the prompt to be sent as a stream; this wraps our one task message
async def task_stream(task):
    yield {"type": "user", "message": {"role": "user", "content": task}}


async def run_agent(decider=None, db_file=None, changes_log=None, runs_log=None,
                    mode="manual", task=TASK, quiet=False, **option_overrides):
    """Runs the agent once and returns the run record. The test passes its own decider and files."""
    global decide, DB_FILE, CHANGES_LOG, RUNS_LOG
    decide = decider or ask_human
    DB_FILE = db_file or DB_FILE
    CHANGES_LOG = changes_log or CHANGES_LOG
    RUNS_LOG = runs_log or RUNS_LOG
    run.clear()
    run.update({"tools_used": [], "proposals": []})
    approved_waiting.clear()
    show = (lambda *a: None) if quiet else print

    options = build_options(**option_overrides)
    started = time.time()
    final = None
    show(f"Routing agent - model {options.model}, data {DB_FILE}, max {options.max_turns} steps, "
         f"max ${options.max_budget_usd:.2f}\n")

    async for message in query(prompt=task_stream(task), options=options):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            # Proof of least privilege: the full tool list the agent was given
            run["tools_available"] = message.data.get("tools", [])
            show("Tools the agent can use:", ", ".join(run["tools_available"]), "\n")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    run["tools_used"].append({"tool": block.name, "input": block.input})
                    show(f"  -> {block.name.split('__')[-1]} {json.dumps(block.input)[:150]}")
                elif isinstance(block, TextBlock) and block.text.strip():
                    show(block.text)
        elif isinstance(message, ResultMessage):
            final = message

    # ---- Write the run record ----
    record = {
        "run_time": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "mode": mode,
        "model": options.model,
        "data": DB_FILE,
        "how_it_ended": final.subtype if final else "no result",   # success / error_max_turns / error_max_budget_usd
        "steps": final.num_turns if final else None,
        "total_cost_usd": round(final.total_cost_usd or 0, 4) if final else None,
        "duration_seconds": round(time.time() - started, 1),
        "tools_available": run.get("tools_available"),
        "tools_used": run["tools_used"],
        "proposals": run["proposals"],
        "final_summary": final.result if final else None,
    }
    with open(RUNS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    show("\n" + "-" * 70)
    show(f"Ended: {record['how_it_ended']} | steps: {record['steps']} | "
         f"cost: ${record['total_cost_usd']} | time: {record['duration_seconds']}s")
    show(f"Proposals: {len(run['proposals'])} | run record added to {RUNS_LOG}")
    return record


if __name__ == "__main__":
    asyncio.run(run_agent())
