# generate_data.py - creates a practice database of made-up card transactions.
# Run once:  python generate_data.py
# It creates transactions.db in this folder (about 60 days, ~60,000 transactions).
# The data is synthetic. It contains a few hidden problems for your analyst bot to find.

import random
import sqlite3
import sys
from datetime import date, timedelta

random.seed(42)  # same "random" data every time, so results are repeatable

# Normal run:  python generate_data.py                    -> transactions.db, with the hidden problems
# Test copy:   python generate_data.py --clean FILE.db     -> same kind of data, NO hidden problems
PLANT_PROBLEMS = "--clean" not in sys.argv
OUT_FILE = "transactions.db" if PLANT_PROBLEMS else sys.argv[sys.argv.index("--clean") + 1]

END = date(2026, 11, 18)
START = END - timedelta(days=59)

BRANDS = [("Visa", 0.55), ("Mastercard", 0.35), ("Amex", 0.07), ("Discover", 0.03)]
COUNTRIES = [("US", 0.30), ("GB", 0.15), ("DE", 0.15), ("FR", 0.10),
             ("BR", 0.10), ("IT", 0.08), ("ES", 0.07), ("JP", 0.05)]
MERCHANTS = [("M001", "travel"), ("M002", "travel"), ("M003", "gaming"),
             ("M004", "retail"), ("M005", "retail"), ("M006", "retail"),
             ("M007", "saas"), ("M008", "saas"), ("M009", "food_delivery"),
             ("M010", "food_delivery")]
ROUTES = [("ACQ_A", 0.5), ("ACQ_B", 0.35), ("ACQ_C", 0.15)]

# Normal decline mix (response code, share of declines)
NORMAL_DECLINES = [("05", 0.35), ("51", 0.30), ("14", 0.05), ("54", 0.06),
                   ("57", 0.06), ("59", 0.05), ("61", 0.05), ("91", 0.04), ("96", 0.04)]


def pick(options):
    r, total = random.random(), 0
    for value, weight in options:
        total += weight
        if r <= total:
            return value
    return options[-1][0]


def three_ds_status(country):
    if country in ("GB", "DE", "FR", "IT", "ES"):   # Europe: SCA applies
        return pick([("Frictionless", 0.7), ("Challenge Passed", 0.25), ("Challenge Failed", 0.05)])
    return pick([("N/A", 0.8), ("Frictionless", 0.2)])


rows = []
txn_id = 0
day = START
while day <= END:
    days_to_end = (END - day).days
    for _ in range(random.randint(900, 1100)):
        txn_id += 1
        brand = pick(BRANDS)
        country = pick(COUNTRIES)
        merchant, category = random.choice(MERCHANTS)
        route = pick(ROUTES)
        amount = round(random.lognormvariate(3.8, 0.9), 2)
        tds = three_ds_status(country)

        decline_rate = 0.13
        code_mix = NORMAL_DECLINES

        # --- hidden problems (no peeking if you want a real test!) ---
        if PLANT_PROBLEMS:
            if brand == "Visa" and country == "DE" and route == "ACQ_B" and days_to_end <= 6:
                decline_rate = 0.45
                code_mix = [("91", 0.55), ("96", 0.30), ("05", 0.15)]
            if merchant == "M003" and country == "FR" and days_to_end <= 20:
                tds = pick([("Challenge Failed", 0.35), ("Challenge Passed", 0.40), ("Frictionless", 0.25)])
            if brand == "Mastercard" and country == "BR" and 25 <= days_to_end <= 39:
                decline_rate = 0.24
                code_mix = [("51", 0.6), ("05", 0.3), ("61", 0.1)]
        # ---------------------------------------------------------------

        if tds == "Challenge Failed":
            approved, code = 0, "3DS_FAIL"
        elif random.random() < decline_rate:
            approved, code = 0, pick(code_mix)
        else:
            approved, code = 1, "00"

        rows.append((txn_id, day.isoformat(), brand, country, merchant, category,
                     route, amount, tds, code, approved))
    day += timedelta(days=1)

db = sqlite3.connect(OUT_FILE)
db.execute("DROP TABLE IF EXISTS transactions")
db.execute("""
    CREATE TABLE transactions (
        txn_id INTEGER PRIMARY KEY,
        txn_date TEXT,          -- YYYY-MM-DD
        card_brand TEXT,
        country TEXT,           -- cardholder country, ISO code
        merchant_id TEXT,
        merchant_category TEXT,
        acquirer_route TEXT,    -- which acquirer connection processed it
        amount_usd REAL,
        three_ds TEXT,          -- 3DS outcome
        response_code TEXT,     -- '00' = approved
        approved INTEGER        -- 1 = approved, 0 = declined
    )""")
db.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
db.commit()
db.close()
print(f"Created {OUT_FILE} with {len(rows):,} transactions "
      f"from {START} to {END}" + ("." if PLANT_PROBLEMS else ", WITHOUT the hidden problems."))
