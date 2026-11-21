# test_analyst.py - regression test for the analyst.
# Asks every question in test_questions.csv, then a second Claude (the "grader")
# checks each answer against your expected answer.
#
# Run:   python test_analyst.py
# Edit the questions/answers in test_questions.csv (opens in Excel).

import csv
import json
import re
from datetime import datetime

import analyst                      # reuses your analyst: same prompt, tools and settings

GRADER_MODEL = "claude-haiku-5-5"   # a small, cheap model is enough for grading

GRADER_PROMPT = """You grade a payments analyst's answer against the expected answer.
PASS if the answer clearly states every fact in the expected answer.
Extra correct detail is fine. FAIL if a fact is missing, wrong, or contradicted.
Reply with JSON only: {"grade": "PASS" or "FAIL", "why": "<one sentence>"}"""


def grade(question, expected, answer):
    response = analyst.client.messages.create(
        model=GRADER_MODEL, max_tokens=2000, system=GRADER_PROMPT,  # 2000 = room for thinking + verdict
        messages=[{"role": "user", "content":
                   f"<question>{question}</question>\n"
                   f"<expected>{expected}</expected>\n"
                   f"<answer>{answer}</answer>"}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        result = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        return result.get("grade", "FAIL"), result.get("why", "")
    except Exception:
        return "FAIL", "Grader reply unreadable: " + text[:100]


with open("test_questions.csv", encoding="utf-8-sig", newline="") as f:
    tests = list(csv.DictReader(f))

results = []
total_in = total_out = 0
for number, test in enumerate(tests, start=1):
    print(f"\n===== Question {number}: {test['question']}")
    answer, tokens_in, tokens_out = analyst.ask(test["question"])
    total_in += tokens_in
    total_out += tokens_out
    verdict, why = grade(test["question"], test["expected_answer"], answer)
    print(f"-> {verdict}: {why}")
    results.append({"question": test["question"], "expected": test["expected_answer"],
                    "grade": verdict, "why": why, "answer": answer})

passed = sum(r["grade"] == "PASS" for r in results)
print("\n========== TEST REPORT ==========")
for r in results:
    print(f"{r['grade']}  {r['question']}")
print(f"\nPassed {passed}/{len(results)}")
print(f"Analyst tokens: {total_in} in / {total_out} out")

with open("test_results.csv", "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    writer.writeheader()
    writer.writerows(results)
with open("test_log.csv", "a", encoding="utf-8-sig", newline="") as f:
    f.write(f"{datetime.now():%Y-%m-%d %H:%M},{analyst.PROMPT_FILE},{passed}/{len(results)},"
            f"{total_in},{total_out}\n")
print("Saved: test_results.csv (every answer) and test_log.csv (one line per run)")
