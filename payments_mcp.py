# payments_mcp.py - the "payments-mcp" server.
# It lets Claude Desktop (or any MCP app) use the analyst's three tools
# directly on transactions.db: describe_table, run_sql and scan_anomalies.
#
# The tool logic is NOT copied here: this file loads analyst.py and calls
# its functions. Change a tool in analyst.py and this server picks it up.
#
# Claude Desktop starts this file by itself (see claude_desktop_config.json).
# To check it starts by hand:   python payments_mcp.py   (it waits silently; Ctrl+C to stop)
#
# Note: in version 2 of the MCP Python SDK, "FastMCP" was renamed "MCPServer".
# Same thing, new name.
#
# IMPORTANT: never print() in this file. Claude Desktop talks to the server
# through the screen output, and stray text would break the connection.

import json
import os
import sqlite3
import threading

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# ---- Settings you may change ----
MAX_ROWS = 200   # most rows run_sql hands back from one query
# ---------------------------------

# analyst.py finds its files (transactions.db, analyst_prompt.txt) relative to the
# folder it runs from. Claude Desktop starts us from somewhere else, so first
# move into this project's folder, then load analyst.py.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)

import analyst  # noqa: E402  (must come after the folder change above)

# Open the database READ-ONLY (mode=ro): nothing sent through this server can
# change or delete data. The server runs each tool on a background thread, so
# the connection must allow that (check_same_thread=False), and the lock makes
# sure only one tool uses it at a time. analyst.py's functions use this connection.
analyst.db = sqlite3.connect(
    f"file:{os.path.join(PROJECT_DIR, 'transactions.db')}?mode=ro",
    uri=True, check_same_thread=False)
db_lock = threading.Lock()

# Ask analyst.run_sql for one row more than the limit. If it really returns
# that many, we know the answer was cut off and can say so.
analyst.MAX_ROWS = MAX_ROWS + 1

# Every tool only reads data, and asking twice gives the same answer.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

# ---- The analyst rules ----
# The rules live ONLY in analyst_prompt.txt (the same file analyst.py uses).
# This server reads that file; it never keeps its own copy. Edit the .txt file,
# and the rules reach Claude Desktop two ways:
#   1. describe_table hands them back with its answer. Claude Desktop always
#      uses tools, so this is the route that reliably works there. It reads the
#      file fresh each time, so edits apply without restarting Claude Desktop.
#   2. The server "instructions" below. Some apps (e.g. Claude Code) read these;
#      Claude Desktop reportedly ignores them. Read once when the server starts.
def load_rules():
    with open(analyst.PROMPT_FILE, encoding="utf-8") as f:
        return f.read().strip()


server = MCPServer(
    name="payments-mcp",
    instructions=(
        "Tools for investigating card-payment approval rates in a synthetic "
        "transactions database (about 60 days of card payments, read-only). "
        "Start with describe_table: it returns the columns, the date range and "
        "the analyst rules to follow. "
        "Approval rate = AVG(approved); a decline is approved = 0.\n\n"
        "ANALYST RULES (follow these in every answer):\n" + load_rules()
    ),
)


@server.tool(annotations=READ_ONLY)
def describe_table() -> str:
    """Shows what is in the transactions table: every column with its type, what the
    key fields mean (response_code '00' = approved, '3DS_FAIL' = 3DS challenge failed,
    approved = 1/0, acquirer_route = acquirer connection used) and the first and last
    transaction date, plus the ANALYST RULES you must follow when investigating
    and answering. Always call this first, before any other tool, so you know the
    rules, the real column names and the right date range."""
    with db_lock:
        table_info = analyst.describe_table()
    return (table_info + "\n\nANALYST RULES - follow every one of these in your "
            "investigation and your answer:\n" + load_rules())


@server.tool(annotations=READ_ONLY)
def run_sql(query: str) -> str:
    """Runs one read-only SQLite SELECT (or WITH ... SELECT) query on the
    transactions table and returns the column names and rows as JSON.
    Call describe_table first if you haven't: it returns the analyst rules.

    Use it to measure approval or decline rates, compare a segment (country, card
    brand, acquirer route, merchant, response code) before and after a date, size
    the impact in transactions or USD, or confirm a scan_anomalies finding.

    At most 200 rows come back; if more matched, the result says
    "row_limit_reached": true. So aggregate in SQL (GROUP BY, COUNT, AVG, SUM)
    rather than pulling raw transactions. Anything that would change data
    (INSERT, UPDATE, DELETE, DROP...) is refused."""
    with db_lock:
        result = analyst.run_sql(query)
    try:
        data = json.loads(result)
    except ValueError:
        return result            # an error message from analyst.py: pass it on as-is
    data["row_limit_reached"] = len(data["rows"]) > MAX_ROWS
    data["rows"] = data["rows"][:MAX_ROWS]
    data["row_limit"] = MAX_ROWS
    return json.dumps(data)


@server.tool(annotations=READ_ONLY)
def scan_anomalies() -> str:
    """Scans the whole history for approval problems, the way an analyst would
    sweep a dashboard: every country x card brand, country x brand x acquirer route
    and country x merchant segment, over every stretch of days, looking for windows
    where the decline rate or the code 05 (do not honor) rate was well above that
    segment's normal level. Catches problems still running AND ones that came and went.

    Returns the strongest findings first (at most 15), each with its date window,
    normal vs window rate, strength in standard deviations (z), excess declines,
    excess USD, the response code that rose most, and still_active (are the last
    3 days still elevated?). Use it for open-ended questions ("anything wrong?",
    "where are we losing approvals?"), then confirm each finding with run_sql.
    Call describe_table first if you haven't: it returns the analyst rules.
    Takes no inputs; may take a few seconds."""
    with db_lock:
        return analyst.scan_anomalies()


if __name__ == "__main__":
    server.run()   # talks over stdio, which is what Claude Desktop expects
