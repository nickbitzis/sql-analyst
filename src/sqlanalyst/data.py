"""Build the sample database: two years of orders for Trailhead, a made-up outdoor gear store.

Everything is generated from a fixed seed, so every copy of the database is
identical and the eval answers never change. A few patterns are planted on
purpose so the questions have something to find: a holiday spike, a region
that grows faster than the rest, and a category with a high return rate.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,          -- West, Mountain, Midwest, South, Northeast
    segment TEXT NOT NULL,         -- consumer or business
    signup_date TEXT NOT NULL      -- ISO date
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL       -- current list price in USD
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,      -- ISO date
    status TEXT NOT NULL,          -- completed, returned, or cancelled
    channel TEXT NOT NULL          -- web, app, or store
);
CREATE TABLE order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,      -- price actually charged per unit, after discount
    PRIMARY KEY (order_id, product_id)
);
"""

CATALOG = {
    "Tents": [("Ridge 2P Tent", 289), ("Basecamp 4P Tent", 449), ("Ultralight 1P Tent", 379), ("Family Cabin Tent", 529)],
    "Sleeping": [("Down Bag 20F", 249), ("Synthetic Bag 30F", 129), ("Insulated Pad", 149), ("Foam Pad", 45), ("Camp Pillow", 35)],
    "Packs": [("Daypack 22L", 89), ("Trek Pack 55L", 239), ("Hydration Vest", 119), ("Kids Pack 15L", 49)],
    "Footwear": [("Trail Runner", 139), ("Hiking Boot", 189), ("Approach Shoe", 159), ("Camp Sandal", 69), ("Wool Socks 3-Pack", 32)],
    "Apparel": [("Rain Shell", 199), ("Puffy Jacket", 229), ("Fleece Pullover", 99), ("Hiking Pant", 85), ("Sun Hoodie", 59), ("Base Layer Top", 75)],
    "Cooking": [("Backpacking Stove", 79), ("Cook Set", 65), ("Water Filter", 49), ("Insulated Mug", 28), ("Camp Chair", 89)],
    "Navigation": [("GPS Watch", 349), ("Headlamp", 45), ("Compass", 29), ("Satellite Messenger", 299)],
}
REGIONS = {"West": 0.28, "Mountain": 0.16, "Midwest": 0.18, "South": 0.22, "Northeast": 0.16}
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Jamie", "Avery", "Quinn", "Drew", "Rowan", "Parker", "Reese", "Skyler", "Emery"]
LAST = ["Rivera", "Chen", "Patel", "Novak", "Okafor", "Lindqvist", "Moreau", "Tanaka", "Haddad", "Kowalski", "Brennan", "Duarte", "Fischer", "Nakamura", "Osei", "Varga"]
START, END = date(2024, 1, 1), date(2025, 12, 31)
# Relative order volume by month. Spring and early summer for the hiking season, and a holiday peak.
SEASON = [0.6, 0.6, 0.8, 1.0, 1.2, 1.3, 1.2, 1.1, 0.9, 0.9, 1.6, 2.0]


def build(path: str | Path, seed: int = 7) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    products = []
    for cat, items in CATALOG.items():
        for name, price in items:
            products.append((len(products) + 1, name, cat, float(price)))
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", products)

    customers = []
    regions, weights = list(REGIONS), list(REGIONS.values())
    for cid in range(1, 1201):
        signup = START - timedelta(days=365) + timedelta(days=rng.randrange((END - START).days + 300))
        customers.append((cid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choices(regions, weights)[0], "business" if rng.random() < 0.12 else "consumer", signup.isoformat()))
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

    orders, items = [], []
    day = START
    while day <= END:
        months_in = (day.year - START.year) * 12 + day.month - 1
        base = 9 * SEASON[day.month - 1] * (1 + 0.012 * months_in)
        for _ in range(_poisson(rng, base)):
            eligible = [c for c in rng.sample(customers, 12) if c[4] <= day.isoformat()]
            if not eligible:
                continue
            # The Mountain region grows about three times faster than the others.
            weights_c = [1 + (0.06 * months_in if c[2] == "Mountain" else 0) for c in eligible]
            cust = rng.choices(eligible, weights_c)[0]
            oid = len(orders) + 1
            lines = rng.sample(products, rng.choices([1, 2, 3, 4], [0.5, 0.3, 0.15, 0.05])[0])
            status = "completed"
            if any(p[2] == "Footwear" for p in lines) and rng.random() < 0.16:
                status = "returned"  # fit problems: footwear comes back far more often
            elif rng.random() < 0.04:
                status = "returned"
            elif rng.random() < 0.03:
                status = "cancelled"
            channel = rng.choices(["web", "app", "store"], [0.5, 0.2 + 0.006 * months_in, 0.3])[0]
            orders.append((oid, cust[0], day.isoformat(), status, channel))
            for pid, _, cat, price in lines:
                qty = 1 if cat in ("Tents", "Navigation") else rng.choices([1, 2, 3], [0.75, 0.2, 0.05])[0]
                discount = 0.2 if day.month == 11 and day.day >= 24 else (0.1 if cust[3] == "business" else 0.0)
                items.append((oid, pid, qty, round(price * (1 - discount), 2)))
        day += timedelta(days=1)
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?)", items)
    con.commit()
    con.close()
    return path


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth's method is fine for the small rates used here.
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
