# analyst.py - the Approval-Rate Analyst: ask a question in plain English,
# Claude writes SQL, runs it on transactions.db, and answers.
#
# Run:   python analyst.py
# Then type a question, e.g.  Why did Visa approvals in DE drop last week?
# Type  quit  to stop.

import json
import math
import sqlite3

import anthropic
from dotenv import load_dotenv

# ---- Settings you may change ----
MODEL = "claude-sonnet-5-5"
PROMPT_FILE = "analyst_prompt.txt"   # Claude's instructions (you edit this one)
MAX_ROWS = 200                       # most rows Claude can see from one query
MAX_STEPS = 15                       # most tool uses per question (safety limit)

# Anomaly scan rules (your business rules - change them here)
SCAN_MIN_Z = 3.0          # how unusual a window must be, in standard deviations
SCAN_MIN_DAILY_TXNS = 30  # segments smaller than this per day are skipped
SCAN_MAX_WINDOW_DAYS = 21 # longest stretch the scan looks for
SCAN_TOP_N = 15           # how many findings to show Claude, strongest first
# ---------------------------------

load_dotenv()
client = anthropic.Anthropic()

with open(PROMPT_FILE, encoding="utf-8") as f:
    system_prompt = f.read()

# Open the database READ-ONLY, so Claude can look but never change or delete data
db = sqlite3.connect("file:transactions.db?mode=ro", uri=True)


# ---------- 1. The tools: what Claude is allowed to do ----------
# Each tool has a name, a description (Claude reads this to decide when to use it),
# and the inputs it needs.
TOOLS = [
    {
        "name": "describe_table",
        "description": "Shows the columns of the transactions table, their meaning, "
                       "and the date range of the data. Use this first if unsure.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scan_anomalies",
        "description": "Systematically scans every country x brand, country x brand x route "
                       "and country x merchant segment, day by day, for stretches where the "
                       "decline rate or the code 05 rate was above that segment's normal rate. "
                       "Finds problems that are still running AND ones that started and ended. "
                       "Returns findings strongest first, with the time window, normal vs window "
                       "rate, strength in standard deviations (z), excess declines, excess USD "
                       "and the response code that rose most. Use it for any open-ended scan, "
                       "then confirm findings with run_sql.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_sql",
        "description": "Runs one read-only SQLite SELECT query on the transactions table "
                       f"and returns up to {MAX_ROWS} rows. Aggregate in SQL "
                       "(GROUP BY, AVG, COUNT) instead of pulling raw rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single SELECT statement."}
            },
            "required": ["query"],
        },
    },
]


# ---------- 2. What actually happens when Claude uses a tool ----------
def describe_table():
    columns = db.execute("PRAGMA table_info(transactions)").fetchall()
    first, last = db.execute("SELECT MIN(txn_date), MAX(txn_date) FROM transactions").fetchone()
    return json.dumps({
        "columns": [c[1] + " " + c[2] for c in columns],
        "notes": "response_code '00' = approved; '3DS_FAIL' = 3DS challenge failed; "
                 "approved = 1/0; acquirer_route = acquirer connection used",
        "date_range": [first, last],
    })


def run_sql(query):
    if not query.strip().lower().startswith(("select", "with")):
        return "Error: only SELECT queries are allowed."
    try:
        cursor = db.execute(query)
        headers = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(MAX_ROWS)
        return json.dumps({"columns": headers, "rows": rows})
    except Exception as error:          # bad SQL: tell Claude so it can fix it
        return f"SQL error: {error}"


def scan_anomalies():
    """Checks every segment and every time window, using prefix sums so it is fast."""
    days = [r[0] for r in db.execute("SELECT DISTINCT txn_date FROM transactions ORDER BY 1")]
    day_index = {d: i for i, d in enumerate(days)}
    total_days = len(days)
    segment_types = [["country", "card_brand"],
                     ["country", "card_brand", "acquirer_route"],
                     ["country", "merchant_id"]]
    findings = []

    for cols in segment_types:
        col_list = ", ".join(cols)
        segments = {}
        for row in db.execute(
                f"SELECT {col_list}, txn_date, COUNT(*), SUM(1 - approved), "
                f"SUM(response_code = '05'), SUM(amount_usd) FROM transactions "
                f"GROUP BY {col_list}, txn_date"):
            key = row[:len(cols)]
            day, n, declines, code05, usd = row[len(cols):]
            s = segments.setdefault(key, {"n": [0] * total_days, "declines": [0] * total_days,
                                          "code05": [0] * total_days, "usd": 0})
            i = day_index[day]
            s["n"][i], s["declines"][i], s["code05"][i] = n, declines, code05
            s["usd"] += usd

        for key, s in segments.items():
            total_n = sum(s["n"])
            if total_n / total_days < SCAN_MIN_DAILY_TXNS:
                continue
            avg_ticket = s["usd"] / total_n
            for metric, counts in (("decline rate", s["declines"]), ("code 05 rate", s["code05"])):
                cum_n, cum_x = [0], [0]
                for i in range(total_days):
                    cum_n.append(cum_n[-1] + s["n"][i])
                    cum_x.append(cum_x[-1] + counts[i])
                best = None
                for start in range(total_days):
                    for end in range(start, min(total_days, start + SCAN_MAX_WINDOW_DAYS)):
                        n = cum_n[end + 1] - cum_n[start]
                        x = cum_x[end + 1] - cum_x[start]
                        n_out, x_out = total_n - n, cum_x[total_days] - x
                        if n == 0 or n_out < 200:
                            continue
                        normal = max(x_out / n_out, 0.5 / n_out)   # rate outside the window
                        expected = normal * n
                        z = (x - expected) / math.sqrt(n * normal * (1 - normal))
                        if best is None or z > best[0]:
                            best = (z, start, end, n, x, expected, normal)
                if best and best[0] >= SCAN_MIN_Z:
                    z, start, end, n, x, expected, normal = best
                    findings.append({
                        "segment": dict(zip(cols, key)), "metric": metric,
                        "from": days[start], "to": days[end], "days": end - start + 1,
                        "txns_per_day": round(n / (end - start + 1)),
                        "normal_rate_pct": round(normal * 100, 1),
                        "window_rate_pct": round(x / n * 100, 1),
                        "z": round(z, 1), "excess_declines": round(x - expected),
                        "excess_usd": round((x - expected) * avg_ticket),
                    })

    findings.sort(key=lambda f: -f["z"])
    shown = findings[:SCAN_TOP_N]

    # For each finding shown, which response code rose the most inside the window?
    for f in shown:
        where = " AND ".join(f"{c} = ?" for c in f["segment"])
        params = list(f["segment"].values())
        rows = db.execute(
            f"SELECT response_code, "
            f"SUM(txn_date BETWEEN ? AND ?), SUM(txn_date NOT BETWEEN ? AND ?) "
            f"FROM transactions WHERE {where} GROUP BY 1",
            [f["from"], f["to"], f["from"], f["to"]] + params).fetchall()
        n_in = sum(r[1] for r in rows) or 1
        n_out = sum(r[2] for r in rows) or 1
        rises = [(r[0], r[1] / n_in - r[2] / n_out) for r in rows if r[0] != "00"]
        code, rise = max(rises, key=lambda r: r[1]) if rises else ("none", 0)
        f["code_that_rose_most"] = f"{code} (+{rise * 100:.1f} pts)"

        # Is it still happening? Compare the last 3 days of data with the normal rate.
        rate_col = "1 - approved" if f["metric"] == "decline rate" else "response_code = '05'"
        n3, x3 = db.execute(
            f"SELECT COUNT(*), SUM({rate_col}) FROM transactions "
            f"WHERE txn_date >= date(?, '-2 days') AND {where}",
            [days[-1]] + params).fetchone()
        normal = f["normal_rate_pct"] / 100
        z3 = ((x3 or 0) - normal * n3) / math.sqrt(n3 * normal * (1 - normal)) if n3 else 0
        f["last_3_days_rate_pct"] = round((x3 or 0) / n3 * 100, 1) if n3 else None
        f["still_active"] = "yes" if z3 >= 2 else "no"

    return json.dumps({
        "rules": {"min_z": SCAN_MIN_Z, "min_daily_txns": SCAN_MIN_DAILY_TXNS,
                  "max_window_days": SCAN_MAX_WINDOW_DAYS},
        "segments_flagged": len(findings), "shown": len(shown), "findings": shown,
        "note": "Many windows are tested, so some findings are random noise. "
                "Higher z and a clear code shift make a real problem more likely. "
                "'to' is where the strongest stretch ends, NOT proof the problem ended: "
                "use still_active (last 3 days vs normal) to judge whether it is ongoing.",
    })


def use_tool(name, tool_input):
    if name == "scan_anomalies":
        return scan_anomalies()
    if name == "describe_table":
        return describe_table()
    if name == "run_sql":
        return run_sql(tool_input["query"])
    return f"Error: unknown tool {name}"


# ---------- 3. The loop: Claude asks for tools until it can answer ----------
def ask(question):
    messages = [{"role": "user", "content": question}]
    tokens_in = tokens_out = 0

    for step in range(MAX_STEPS):
        response = client.messages.create(
            model=MODEL, max_tokens=4000, system=system_prompt,
            tools=TOOLS, messages=messages,
        )
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens

        # Keep Claude's whole reply in the conversation history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Claude is done: print its written answer
            answer = "".join(b.text for b in response.content if b.type == "text")
            print("\n" + answer)
            print(f"\n[{step + 1} steps, tokens: {tokens_in} in / {tokens_out} out]")
            return answer, tokens_in, tokens_out

        # Claude asked for one or more tools: run them and send back the results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                shown = block.input.get("query", "") if block.input else ""
                print(f"  > Claude uses {block.name} {shown}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": use_tool(block.name, block.input),
                })
        messages.append({"role": "user", "content": results})

    print(f"\nStopped after {MAX_STEPS} steps without a final answer.")
    return "NO ANSWER (step limit reached)", tokens_in, tokens_out


# ---------- 4. The chat: type questions until you type quit ----------
# (This part only runs when you start analyst.py directly, not when the test uses it.)
if __name__ == "__main__":
    print("Approval-Rate Analyst ready. Ask a question (or type quit).")
    while True:
        question = input("\nYou: ").strip()
        if question.lower() in ("quit", "exit"):
            break
        if question:
            ask(question)
