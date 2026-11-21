---
name: exec-summary
description: Runs a fresh approval-rate investigation with the analyst and turns it into a one-page PDF executive summary for a payments VP ($ impact first, ranked issues with charts, and next steps as Handling or Decision needed). Use when the user asks for an exec summary, VP summary, or one-pager of the approval-rate findings.
---

# Exec summary for a payments VP

Produce a one-page PDF that a payments VP can read in two minutes. Data is
synthetic (transactions.db); never use real or employer data.

## Steps

1. **Tell the user** this runs a fresh analyst investigation (billed API calls)
   and roughly how long it takes, then go ahead.

2. **Run a fresh investigation** from the project folder:

   ```powershell
   .venv\Scripts\python.exe -c "import analyst; analyst.ask('Scan all the data for approval-rate problems. For each issue give the segment (country, brand, acquirer route, merchant), start date, end date or whether it is still ongoing, normal vs problem decline rate, the response codes that rose, excess declines, excess USD, and the likely cause.')"
   ```

   Keep the analyst's full answer; it is the source of the findings.

3. **Check the numbers** with read-only SQLite queries on transactions.db
   (open with `file:transactions.db?mode=ro`):
   - Overall approval rate: last 7 days vs the rest of the period, plus the
     date range of the data.
   - For each issue: re-query its decline rate in the window vs outside it, and
     whether the last 3 days are still elevated (ongoing vs stopped).
   - Excess $ = (declines in window − window transactions × normal decline
     rate) × the segment's average transaction amount.
   - **Scale** for every $ figure: an issue's excess $ as a % of that segment's
     total $ volume in the issue window; the headline total as a % of all $
     processed in the period; the ongoing weekly cost as a % of the last 7
     days' total $ volume.
   - Drop any issue the queries don't confirm. If the analyst's numbers and
     yours differ, use yours and mention it to the user.

   Every number in the summary must come from the analyst's answer or one of
   these queries. Never estimate or round into new numbers.

4. **Ask the user about the next steps before building.** List one proposed
   action per issue (or one combined action where issues share a cause) and ask,
   in a single message, for each action's:
   - **status**: Handling (and what is actually being done) or Decision needed
   - **owner**: the team or person
   - **date**: when it will be done / decided by

   Then stop and wait for the reply. Use only what the user gives. **Never
   invent a status, owner or date.** Anything not provided goes into the spec
   as a visible placeholder: `"type": "[status?]"`, `"owner": "[owner?]"`,
   `"by": "[date?]"`. Mention the remaining placeholders when showing the result.
   `"by"` takes a date (`"2026-11-20"`, shown as "20 Nov") or the user's own
   wording (`"week of 23 Nov"`, shown as given). If the user gives two dates
   for one action, put the earlier step's date in `what` and the later one in `by`.

5. **Write the spec** to `reports/exec_summary_spec.json` (format below) and
   build the PDF:

   ```powershell
   .venv\Scripts\python.exe .claude\skills\exec-summary\build_pdf.py reports\exec_summary_spec.json --png
   ```

   The PDF is saved as `reports/exec_summary_<data end date>.pdf`, plus a `.png`
   picture of the same page. **Open the PNG and check the layout before
   showing the user**: text running off the page or into a chart, overlapping
   labels, garbled `$` amounts, or anything spilling past one page. If something
   is wrong, shorten the spec text or adjust the widths in build_pdf.py, rebuild,
   and look again.

6. **Show the user** the PDF path and the summary text (headline, issue
   paragraphs, next steps), so they can review it without opening the file,
   and list any `[status?]`, `[owner?]` or `[date?]` placeholders still on the page.

## What goes on the page (in this order)

1. **Headline: $ first.** Total excess $ lost (with % of all $ processed), the
   weekly $ still being lost by ongoing issues (with % of weekly volume), and
   then the approval rate recently vs normal.
2. **$ impact bar chart** (full width): excess $ per issue, ranked, with each
   bar labelled with its % of segment volume (build_pdf.py computes this).
3. **Issues, ranked**: ongoing first, then by excess $ (largest first). Each
   issue gets a short paragraph (2–3 sentences) that **opens with the $ lost and
   its % of segment volume**, then covers where (country, brand, route,
   merchant), when (start date; "ongoing" or end date), the decline rate vs
   normal, and why (the response codes that rose). Next to it: a small chart
   of that segment's daily decline rate vs its normal level.
4. **Next steps** (bottom left) with the daily approval-rate chart for all
   traffic (bottom right, lined up with the issue charts). Each step is one of:
   - **Handling** (the default): what is being done, by whom, and by when, e.g.
     "Handling: rerouting German Visa traffic off ACQ_B; incident opened with
     ACQ_B. Payments Ops, by 19 Nov." For ongoing issues add the weekly $ at
     stake.
   - **Decision needed**: only for things outside the team's authority, such
     as a contract change with an acquirer or a budget. Say what the VP must
     decide and by when.

   Status, owner and date always come from the user (step 4), never from
   Claude; missing ones show as `[status?]`, `[owner?]`, `[date?]` in bold. The
   page says "No decision needed from you" only when every status is known and
   none is a decision.

## Style

- The reader knows payments: response codes (91, 05, 51), acquirer routes,
  3DS and issuer terms are fine without explanation.
- No statistics jargon: no z-scores, "standard deviations" or "significance".
  Say "normal rate" and "problem-period rate".
- **Mark decreases in red** by wrapping them in `[[...]]`. That covers anything
  that got worse: $ lost, weekly $ at stake, approval-rate drops, and
  problem-period decline rates. Leave normal/baseline figures black. `**...**`
  makes text bold.
- Every $ figure gets a sense of scale (see step 3).
- Dates as "12 Nov". Money as "$48.2K". Rates to one decimal ("31.4%").
- It must fit on one page. If there are more than 3 issues, chart and write
  paragraphs for the top 3 and list the rest as one line each.

## Spec format (reports/exec_summary_spec.json)

The `//` comments below are explanations only; the real file must be plain JSON.

```
{
  "title": "Approval-rate summary",
  "data_through": "2026-11-18",
  "headline": "[[$11.7K]] lost to excess declines (0.29% of $4.0M processed). ...",
  "issues": [
    {
      "name": "DE Visa on ACQ_B",
      "status": "ongoing",            // or "stopped"
      "from": "2026-11-12",
      "to": "2026-11-18",
      "filters": {"country": "DE", "card_brand": "Visa", "acquirer_route": "ACQ_B"},
      "paragraph": "[[$4.7K]] lost since 12 Nov, [[35.3%]] of this segment's $13.4K volume. ...",
      "excess_declines": 72,
      "excess_usd": 4733
    }
  ],
  "next_steps": [
    {"type": "handling", "what": "rerouting German Visa traffic off ACQ_B; incident opened with ACQ_B",
     "owner": "Payments Ops", "by": "2026-11-19", "at_stake": "[[~$4.7K/week]]"},
    {"type": "decision", "what": "approve <contract change / budget>", "by": "2026-11-21",
     "at_stake": "<optional>"},
    {"type": "[status?]", "what": "fixing M003's 3DS challenge flow",
     "owner": "[owner?]", "by": "[date?]"}     // nothing provided by the user yet
  ]
}
```

`filters` uses transactions table column names; build_pdf.py uses it to draw
each issue's decline-rate chart and compute its % of segment volume directly
from transactions.db (read-only). The daily approval-rate trend is also drawn
from the database, so charts can't drift from the data.
