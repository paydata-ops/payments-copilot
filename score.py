# score.py - grades Claude on declines.csv, like an exam with an answer key.
# How to run (in the terminal, with (.venv) switched on):
#     python score.py prompt_v1.txt
# To test a new prompt version, just change the file name:
#     python score.py prompt_v2.txt

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime

import anthropic
from dotenv import load_dotenv

# ---- Settings you may change ----
MODEL = "claude-sonnet-5-5"   # which Claude answers the exam
DATA_FILE = "declines.csv"    # the exam + answer key
# ---------------------------------

load_dotenv()                  # reads your key from the .env file
client = anthropic.Anthropic() # opens the connection to Claude

# Which prompt file to test (default: prompt_v1.txt)
prompt_file = sys.argv[1] if len(sys.argv) > 1 else "prompt_v1.txt"
with open(prompt_file, encoding="utf-8") as f:
    system_prompt = f.read()


def read_rows(path):
    """Open the CSV. Excel sometimes saves in an older Windows format, so try both."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise SystemExit("Could not read " + path)


rows = read_rows(DATA_FILE)
print(f"Testing {prompt_file} on {len(rows)} declines with {MODEL}...\n")

results = []
input_tokens = output_tokens = 0

for number, row in enumerate(rows, start=2):  # start=2 so numbers match Excel rows
    # 1. Build the "exam question" for this decline (the answer column is NOT sent)
    question = (
        "<decline>\n"
        f"response_code: {row['response_code']}\n"
        f"card_brand: {row['card_brand']}\n"
        f"country: {row['country']}\n"
        f"3ds: {row['3ds']}\n"
        f"merchant_note: {row['merchant_note']}\n"
        "</decline>"
    )

    # 2. Send it to Claude with your prompt
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,  # room for Claude's thinking + the answer
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    input_tokens += response.usage.input_tokens
    output_tokens += response.usage.output_tokens

    # 3. Read Claude's JSON answer
    # Claude may "think" first; keep only the parts that are its written answer
    text = "".join(block.text for block in response.content if block.type == "text")
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        answer = json.loads(found.group(0))
        predicted = str(answer.get("category", "")).strip()
        reason = answer.get("reason", "")
    except Exception:
        predicted, reason = "UNREADABLE", text[:200]

    # 4. Compare with your correct answer
    correct = row["correct_category"].strip()
    is_right = predicted == correct
    results.append({
        "excel_row": number,
        "difficulty": (row.get("difficulty") or "n/a").strip(),
        "correct": correct,
        "claude_said": predicted,
        "right": "yes" if is_right else "no",
        "claude_reason": reason,
    })
    print(f"Row {number}: {'RIGHT' if is_right else 'WRONG'}")

# ---- 5. The report card ----
def score(group):
    right = sum(1 for r in group if r["right"] == "yes")
    return right, len(group)

right, total = score(results)
print("\n========== REPORT CARD ==========")
print(f"Overall: {right}/{total} ({right / total:.0%})")

for level in ("easy", "hard"):
    group = [r for r in results if r["difficulty"] == level]
    if group:
        r_, t_ = score(group)
        print(f"{level.capitalize()}: {r_}/{t_} ({r_ / t_:.0%})")

mixups = Counter((r["correct"], r["claude_said"]) for r in results if r["right"] == "no")
if mixups:
    print("\nMix-ups (correct -> what Claude said):")
    for (correct, said), count in mixups.most_common():
        print(f"  {correct} -> {said}: {count}")

    print("\nWrong answers:")
    for r in results:
        if r["right"] == "no":
            print(f"  Row {r['excel_row']} ({r['difficulty']}): correct={r['correct']}, "
                  f"Claude said {r['claude_said']} - {r['claude_reason']}")

print(f"\nTokens used: {input_tokens} in, {output_tokens} out "
      "(the Console's Usage page shows the cost)")

# ---- 6. Save results ----
version = prompt_file.rsplit(".", 1)[0]
details_file = f"results_{version}.csv"
with open(details_file, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    writer.writeheader()
    writer.writerows(results)

easy_r, easy_t = score([r for r in results if r["difficulty"] == "easy"])
hard_r, hard_t = score([r for r in results if r["difficulty"] == "hard"])
log_exists = False
try:
    open("score_log.csv").close()
    log_exists = True
except FileNotFoundError:
    pass
with open("score_log.csv", "a", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    if not log_exists:
        writer.writerow(["date", "prompt", "model", "overall", "easy", "hard"])
    writer.writerow([
        datetime.now().strftime("%Y-%m-%d %H:%M"), prompt_file, MODEL,
        f"{right}/{total}", f"{easy_r}/{easy_t}", f"{hard_r}/{hard_t}",
    ])

print(f"\nSaved: {details_file} (every answer) and score_log.csv (one line per run)")
