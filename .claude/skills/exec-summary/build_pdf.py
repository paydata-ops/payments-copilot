# build_pdf.py - lays out the one-page exec summary PDF.
# Text (headline, issue paragraphs, next steps) comes from the spec JSON that Claude writes.
# Chart numbers come straight from transactions.db (read-only), so charts match the data.
#
# Run (from the project folder):
#     .venv\Scripts\python.exe .claude\skills\exec-summary\build_pdf.py reports\exec_summary_spec.json --png
# --png  also saves a picture of the page (for checking the layout).
#
# In any text from the spec:  [[like this]] is drawn in red (a decrease / something worse),
#                             **like this** is drawn in bold.

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                     # draw to files, no window
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# ---- Settings you may change ----
DB_FILE = "transactions.db"
MAX_CHARTED_ISSUES = 3        # issues beyond this get one line each
PAGE_SIZE = (8.5, 11)         # inches (US Letter)
RED = "#c0392b"               # decreases / ongoing issues
GREY = "#7f8c8d"              # stopped issues
TEXT = "#1a1a1a"
TREND_COLOR = "#2c3e50"
# ---------------------------------


def usd(x):
    x = float(x)
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}K"
    return f"${x:,.0f}"


def day(s):
    return date.fromisoformat(s)


def short(d):
    return f"{d.day} {d:%b}"


# ---------- read the spec and the database ----------
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    raise SystemExit("Usage: build_pdf.py <spec.json> [--png]")
spec_path = Path(args[0])
spec = json.loads(spec_path.read_text(encoding="utf-8"))

db = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
columns = {c[1] for c in db.execute("PRAGMA table_info(transactions)")}

daily = db.execute("SELECT txn_date, AVG(approved) FROM transactions "
                   "GROUP BY 1 ORDER BY 1").fetchall()
trend_days = [day(d) for d, _ in daily]
trend_rate = [r * 100 for _, r in daily]

# Ongoing first, then biggest $ first
issues = sorted(spec["issues"], key=lambda i: (i["status"] != "ongoing", -float(i["excess_usd"])))
charted, extra = issues[:MAX_CHARTED_ISSUES], issues[MAX_CHARTED_ISSUES:]


def where_clause(filters):
    bad = set(filters) - columns
    if bad:
        raise SystemExit(f"Unknown filter column(s) in spec: {', '.join(bad)}")
    return " AND ".join(f"{c} = ?" for c in filters), list(filters.values())


def segment_series(issue):
    """Daily decline rate for the issue's segment, and its normal rate outside the window."""
    where, params = where_clause(issue["filters"])
    start, end = day(issue["from"]), day(issue["to"])
    rows = db.execute(f"SELECT txn_date, COUNT(*), SUM(1 - approved) FROM transactions "
                      f"WHERE {where} GROUP BY 1 ORDER BY 1", params).fetchall()
    days = [day(d) for d, _, _ in rows]
    rate = [x / n * 100 for _, n, x in rows]
    out = [(n, x) for d, n, x in rows if not (start <= day(d) <= end)]
    n_out = sum(n for n, _ in out)
    normal = sum(x for _, x in out) / n_out * 100 if n_out else None
    return days, rate, normal


def segment_share(issue):
    """Excess $ as a % of the segment's total $ volume during the issue window."""
    where, params = where_clause(issue["filters"])
    volume = db.execute(f"SELECT SUM(amount_usd) FROM transactions WHERE {where} "
                        f"AND txn_date BETWEEN ? AND ?", params + [issue["from"], issue["to"]]
                        ).fetchone()[0] or 0
    return float(issue["excess_usd"]) / volume * 100 if volume else None


def color(issue):
    return RED if issue["status"] == "ongoing" else GREY


def date_axis(ax):
    ax.set_xlim(trend_days[0], trend_days[-1] + timedelta(days=1))   # same dates on every chart
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.tick_params(labelsize=6.5)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


# ---------- text with red / bold parts, wrapped to the box ----------
def rich(ax, text, y=1.0, size=8, weight="normal", line=1.4):
    """Draws text inside ax (axes coordinates), wrapping at the box width.
    [[...]] = red, **...** = bold. Returns the y just below the last line."""
    renderer = fig.canvas.get_renderer()
    box = ax.get_window_extent(renderer)

    chars, red, bold, i = [], False, False, 0          # (character, red?, bold?)
    while i < len(text):
        if text.startswith("[[", i):
            red, i = True, i + 2
        elif text.startswith("]]", i):
            red, i = False, i + 2
        elif text.startswith("**", i):
            bold, i = not bold, i + 2
        else:
            chars.append((text[i], red, bold))
            i += 1

    words, current = [], []
    for ch, r, b in chars:
        if ch in " \n":
            if current:
                words.append(current)
                current = []
            if ch == "\n":
                words.append("\n")
        else:
            current.append((ch, r, b))
    if current:
        words.append(current)

    def width(s, w):
        t = ax.text(0, 0, s, fontsize=size, weight=w, transform=ax.transAxes)
        result = t.get_window_extent(renderer).width / box.width
        t.remove()
        return result

    space = width("x x", weight) - width("xx", weight)
    line_h = size * line * fig.dpi / 72 / box.height
    x = 0
    for word in words:
        if word == "\n":
            x, y = 0, y - line_h
            continue
        runs = []                                        # pieces of the word with one style
        for ch, r, b in word:
            if runs and runs[-1][1:] == (r, b):
                runs[-1][0] += ch
            else:
                runs.append([ch, r, b])
        pieces = []
        for s, r, b in runs:
            w = "bold" if b else weight
            pieces.append((s, r, w, width(s, w)))
        if x > 0 and x + sum(p[3] for p in pieces) > 1:
            x, y = 0, y - line_h
        for s, r, w, wd in pieces:
            ax.text(x, y, s, fontsize=size, weight=w, color=RED if r else TEXT,
                    va="top", transform=ax.transAxes)
            x += wd
        x += space
    return y - line_h


# ---------- lay out the page ----------
plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["text.parse_math"] = False         # "$" is money here, not a math formula
fig = plt.figure(figsize=PAGE_SIZE)
rows = 4 + len(charted)                          # title, headline, $ bars, issues..., bottom
heights = [0.45, 0.6, 0.25 + 0.3 * len(issues)] + [1.25] * len(charted) + [2.0]
grid = fig.add_gridspec(rows, 2, height_ratios=heights, width_ratios=[1.55, 1],
                        left=0.07, right=0.95, top=0.96, bottom=0.04, hspace=0.6, wspace=0.18)

# Title
ax = fig.add_subplot(grid[0, :]); ax.axis("off")
end_day = day(spec["data_through"])
ax.text(0, 0.75, spec.get("title", "Approval-rate summary"), fontsize=17, weight="bold", va="center")
ax.text(0, 0.1, f"Data through {short(end_day)} {end_day.year} · synthetic data",
        fontsize=8, color="#555", va="center")

# Headline ($ first)
ax = fig.add_subplot(grid[1, :]); ax.axis("off")
rich(ax, spec["headline"], size=10, weight="bold")

# $ impact by issue, full width, with scale
ax = fig.add_subplot(grid[2, :])
values = [float(i["excess_usd"]) for i in issues]
bars = ax.barh(range(len(issues)), values, color=[color(i) for i in issues], height=0.7)
for b, issue, v in zip(bars, issues, values):
    y = b.get_y() + b.get_height() / 2
    share = segment_share(issue)
    scale = f" · {share:.1f}% of segment $ in window" if share is not None else ""
    ax.text(b.get_width(), y, f"  {usd(v)}{scale}", va="center", fontsize=7.5)
    ax.text(max(values) * 0.015, y, issue["name"], va="center", fontsize=7, color="white",
            weight="bold")
ax.invert_yaxis()
ax.set_xlim(0, max(values) * 1.9)
ax.set_title("Excess declines in $ by issue (red = ongoing, grey = stopped)", fontsize=8.5, loc="left")
ax.set_xticks([]); ax.set_yticks([])
for side in ("top", "right", "bottom", "left"):
    ax.spines[side].set_visible(False)

# One row per charted issue: paragraph left, decline-rate chart right
for row, issue in enumerate(charted, start=3):
    start, end = day(issue["from"]), day(issue["to"])
    tag = "ONGOING" if issue["status"] == "ongoing" else f"STOPPED {short(end)}"
    ax = fig.add_subplot(grid[row, 0]); ax.axis("off")
    ax.text(0, 1.02, f"{row - 2}. {issue['name']}", fontsize=10, weight="bold", va="top")
    ax.text(1, 1.02, f"{tag} · {usd(issue['excess_usd'])}", fontsize=8, weight="bold",
            color=color(issue), va="top", ha="right")
    rich(ax, issue["paragraph"], y=0.8, size=8, line=1.35)

    days, rate, normal = segment_series(issue)
    ax = fig.add_subplot(grid[row, 1])
    ax.plot(days, rate, color=color(issue), lw=1.1)
    ax.axvspan(start, end + timedelta(days=1), color=color(issue), alpha=0.12)
    if normal is not None:
        ax.axhline(normal, color="#333", ls="--", lw=0.8)
        ax.text(trend_days[0], normal, f" normal {normal:.1f}%", fontsize=6, va="bottom",
                bbox=dict(facecolor="white", edgecolor="none", pad=0.5, alpha=0.8))
    ax.set_title("Daily decline rate (%)", fontsize=7, loc="left")
    date_axis(ax)

# Bottom row: next steps left, daily approval trend right (lined up with the issue charts)
bottom = rows - 1
ax = fig.add_subplot(grid[bottom, 1])
ax.plot(trend_days, trend_rate, color=TREND_COLOR, lw=1.1)
for i in issues:
    ax.axvspan(day(i["from"]), day(i["to"]) + timedelta(days=1), color=color(i), alpha=0.13)
ax.set_title("Daily approval rate (%), all traffic", fontsize=7, loc="left")
date_axis(ax)

ax = fig.add_subplot(grid[bottom, 0]); ax.axis("off")
ax.text(0, 1.02, "Next steps", fontsize=10, weight="bold", va="top")
# Status, owner and date come from the user. Anything missing is shown as a bold
# placeholder ([status?], [owner?], [date?]) - never filled in.
def by_date(s):
    text = str(s.get("by") or "").strip()
    if not text or text == "[date?]":
        return "**[date?]**"
    try:
        return short(day(text))          # "2026-11-19" -> "19 Nov"
    except ValueError:
        return text                      # e.g. "week of 23 Nov", shown as given


def owner(s):
    o = str(s.get("owner") or "").strip()
    return o if o and o != "[owner?]" else "**[owner?]**"


steps = spec["next_steps"]
decisions = [s for s in steps if s.get("type") == "decision"]
handling = [s for s in steps if s.get("type") == "handling"]
unknown = [s for s in steps if s.get("type") not in ("decision", "handling")]
y = 0.88
if not decisions and not unknown:
    y = rich(ax, "No decision needed from you: everything below is being handled.", y=y, size=8)
    y -= 0.03
for label, group in (("Decision needed:", decisions), ("Handling:", handling),
                     ("[status?]", unknown)):
    for s in group:
        stake = f" ({s['at_stake']})" if s.get("at_stake") else ""
        y = rich(ax, f"**{label}** {s['what']}. {owner(s)}, by {by_date(s)}.{stake}", y=y, size=8)
        y -= 0.03
if extra:
    y = rich(ax, "**Other issues:** " + "; ".join(
        f"{i['name']} ({i['status']}, {usd(i['excess_usd'])})" for i in extra), y=y, size=8)
    y -= 0.03
# Optional small note under the steps (e.g. "actions in this sample are illustrative")
if spec.get("next_steps_note"):
    y = rich(ax, "**Note:** " + spec["next_steps_note"], y=y, size=7)

# ---------- save ----------
out = spec_path.parent / f"exec_summary_{spec['data_through']}.pdf"
fig.savefig(out)
print(f"Saved {out}")
if "--png" in sys.argv:
    png = out.with_suffix(".png")
    fig.savefig(png, dpi=110)
    print(f"Saved {png}")
