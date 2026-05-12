"""Toast discovery script (v2).

Run this once to gather:
  1. Menu items matching our fundraiser patterns ("Chaniya" or "give $10 get $10")
     — captures (name, guid, count) per brand
  2. A catalog of every distinct appliedServiceCharges name seen
     — this is where Toast's "Round Up for Charity" lives; we're looking for a name like
       "Round Up", "Donation", "Charity", etc.
  3. Full check JSON for a couple of recent orders — so we can eyeball the exact field shape
  4. One sample payment per brand — confirms there's no round-up field on the Payment object

Usage:
    python toast_discover.py
"""

import json
import time
from datetime import datetime, date, timedelta, timezone

import requests
from dotenv import load_dotenv

# Load .env before importing toast_config (which reads os.environ at import time)
load_dotenv()

from toast_config import (  # noqa: E402
    CREDENTIALS,
    BRAND_STORES,
    ORDERS_BULK_URL,
    PAYMENTS_URL,
    PAYMENT_DETAIL_URL,
    PAGE_SIZE,
    assert_credentials_loaded,
)
from toast_auth import get_access_token  # noqa: E402


TARGET_NAME_PATTERNS = ["chaniya", "give $10 get $10"]
LOOKBACK_DAYS = 3
CHECK_SAMPLES = 2     # full check JSON dumps per brand
PAYMENT_SAMPLES = 1   # payment sample per brand


def matches_target(name):
    if not name:
        return False
    lowered = name.lower()
    return any(pattern in lowered for pattern in TARGET_NAME_PATTERNS)


def make_session(token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return s


def iso_timestamp(dt):
    """Format a UTC datetime in the shape Toast's ordersBulk endpoint expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def fetch_recent_orders(session, store):
    """Fetch all orders from the last LOOKBACK_DAYS for a store."""
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)

    orders = []
    page = 1
    while page <= 20:  # safety cap
        params = {
            "startDate": iso_timestamp(start_dt),
            "endDate":   iso_timestamp(end_dt),
            "page":      page,
            "pageSize":  PAGE_SIZE,
        }
        headers_backup = session.headers.copy()
        session.headers["Toast-Restaurant-External-ID"] = store["guid"]
        resp = session.get(ORDERS_BULK_URL, params=params)
        session.headers = headers_backup.copy()

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"    rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        batch = data if isinstance(data, list) else data.get("orders", [])
        if not batch:
            break
        orders.extend(batch)
        page += 1

    return orders


def extract_menu_items(orders):
    """Return dict: (displayName, item_guid) -> count, for selections whose displayName matches our patterns.

    Note: Toast puts the visible item label on selection.displayName, not selection.item.name —
    selection.item is just a GUID pointer with no name field of its own.
    """
    seen = {}
    for order in orders:
        for check in order.get("checks", []):
            for sel in check.get("selections", []):
                name = sel.get("displayName") or ""
                guid = (sel.get("item") or {}).get("guid")
                if matches_target(name):
                    seen[(name, guid)] = seen.get((name, guid), 0) + 1
    return seen


def catalog_service_charges(orders):
    """Group every appliedServiceCharge by (name, category) and sum net amounts.

    Returns dict: (name, category) -> {'count': N, 'gross': $, 'refunded': $, 'net': $}
    """
    catalog = {}
    for order in orders:
        for check in order.get("checks", []):
            for sc in check.get("appliedServiceCharges", []):
                name = sc.get("name") or "(unnamed)"
                category = sc.get("serviceChargeCategory") or "(uncategorized)"
                gross = sc.get("chargeAmount") or 0
                refund = (sc.get("refundDetails") or {}).get("refundAmount") or 0
                key = (name, category)
                entry = catalog.setdefault(key, {"count": 0, "gross": 0.0, "refunded": 0.0, "net": 0.0})
                entry["count"] += 1
                entry["gross"] += gross
                entry["refunded"] += refund
                entry["net"] += (gross - refund)
    return catalog


def first_n_checks(orders, n):
    out = []
    for order in orders:
        for check in order.get("checks", []):
            out.append(check)
            if len(out) >= n:
                return out
    return out


def fetch_sample_payments(session, store, limit=PAYMENT_SAMPLES):
    headers_backup = session.headers.copy()
    session.headers["Toast-Restaurant-External-ID"] = store["guid"]
    samples = []
    for days_back in range(1, 8):
        d = date.today() - timedelta(days=days_back)
        business_date = int(d.strftime("%Y%m%d"))
        resp = session.get(PAYMENTS_URL, params={"paidBusinessDate": business_date})
        if not resp.ok:
            continue
        data = resp.json()
        if not data:
            continue
        if isinstance(data[0], str):
            for guid in data[:limit]:
                r = session.get(PAYMENT_DETAIL_URL.format(guid=guid))
                if r.ok:
                    samples.append(r.json())
                if len(samples) >= limit:
                    break
        else:
            samples.extend(data[:limit])
        if samples:
            break
    session.headers = headers_backup.copy()
    return samples


def main():
    assert_credentials_loaded()

    for cred in CREDENTIALS:
        brand = cred["brand"]
        print("\n" + "=" * 70)
        print(f"BRAND: {brand}")
        print("=" * 70)

        try:
            token = get_access_token(
                cred["clientId"], cred["clientSecret"], cred["userAccessType"]
            )
        except Exception as e:
            print(f"  auth failed: {e}")
            continue

        session = make_session(token)
        store = BRAND_STORES[brand][0]
        print(f"Scanning {store['name']} (last {LOOKBACK_DAYS} days)...")

        try:
            orders = fetch_recent_orders(session, store)
        except Exception as e:
            print(f"  ordersBulk failed: {e}")
            continue

        print(f"  pulled {len(orders)} orders")

        # 1. Menu item matches
        print(f"\n--- {brand}: matching menu items ---")
        items = extract_menu_items(orders)
        if not items:
            print(f"  no items matching {TARGET_NAME_PATTERNS} found in this window")
        for (name, guid), count in items.items():
            print(f"    name={name!r}  guid={guid}  (seen {count}x)")

        # 2. Catalog appliedServiceCharges grouped by (name, category), with totals.
        #    Round-ups should appear with serviceChargeCategory == "FUNDRAISING_CAMPAIGN".
        print(f"\n--- {brand}: appliedServiceCharges breakdown ---")
        catalog = catalog_service_charges(orders)
        if not catalog:
            print("  no service charges on any check in this window")
        else:
            for (name, category), info in sorted(catalog.items(), key=lambda kv: -kv[1]["count"]):
                marker = "  <-- ROUND-UP" if category == "FUNDRAISING_CAMPAIGN" else ""
                print(f"    name={name!r}  category={category}  count={info['count']}  "
                      f"gross=${info['gross']:.2f}  refunded=${info['refunded']:.2f}  "
                      f"net=${info['net']:.2f}{marker}")

            # Summary line for fundraising round-ups specifically
            fc_net = sum(i["net"] for (_, c), i in catalog.items() if c == "FUNDRAISING_CAMPAIGN")
            fc_count = sum(i["count"] for (_, c), i in catalog.items() if c == "FUNDRAISING_CAMPAIGN")
            if fc_count > 0:
                print(f"\n  → FUNDRAISING_CAMPAIGN total at {store['name']} "
                      f"(last {LOOKBACK_DAYS} days): ${fc_net:.2f} from {fc_count} charges")
            else:
                print(f"\n  → No FUNDRAISING_CAMPAIGN charges found at {store['name']} "
                      f"in last {LOOKBACK_DAYS} days")

        # 3. Dump a few full check structures so we can eyeball where round-ups live
        print(f"\n--- {brand}: sample check JSON ({CHECK_SAMPLES} max) ---")
        for i, check in enumerate(first_n_checks(orders, CHECK_SAMPLES), 1):
            print(f"\n  --- {brand} check sample {i} ---")
            print(json.dumps(check, indent=2, default=str))

        # 4. One sample payment (round-up not expected here — for completeness)
        print(f"\n--- {brand}: sample payment ({PAYMENT_SAMPLES}) ---")
        for p in fetch_sample_payments(session, store):
            print(json.dumps(p, indent=2, default=str))


if __name__ == "__main__":
    main()
