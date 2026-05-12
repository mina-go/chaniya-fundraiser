"""Toast fundraiser connector.

Returns total contribution to the Chaniya fundraiser from Toast POS for a given window:
  - Menu item sales: selections whose displayName contains "chaniya" or "give $10 get $10"
                     (case-insensitive substring match across all stores)
  - Round-up donations: appliedServiceCharges where serviceChargeCategory == "FUNDRAISING_CAMPAIGN"

Money math notes:
  - Menu items use `price` (post-discount, pre-tax). If a discount was applied to the check,
    the prorated portion is already reflected. Tax goes to the government, not the fund.
  - Round-ups use `chargeAmount` minus any `refundDetails.refundAmount`.
  - Voided selections and voided/deleted checks are skipped entirely.
  - All amounts are summed in Decimal then converted to float at the output boundary.

The function `fetch_total(window_start, window_end)` is the public entry point used by
aggregate.py. Run this file directly for a quick smoke test (see __main__).
"""

# Make the project root importable whether this file is run via `python sources/toast.py`
# or `python -m sources.toast`. Without this, running as a script puts only `sources/`
# on sys.path, which can't find `toast_auth.py` and `toast_config.py` at the project root.
import os as _os
import sys as _sys
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests

from toast_auth import get_access_token
from toast_config import (
    BRAND_STORES,
    CREDENTIALS,
    ORDERS_BULK_URL,
    PAGE_SIZE,
    assert_credentials_loaded,
)

logger = logging.getLogger(__name__)


# Toast's serviceChargeCategory value for fundraising round-ups.
FUNDRAISING_CATEGORY = "FUNDRAISING_CAMPAIGN"


# ---------- helpers ----------

def _iso_timestamp(dt: datetime) -> str:
    """Format a datetime in the shape Toast's ordersBulk endpoint expects (UTC, millisecond precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _matches_menu_item(name) -> bool:
    """Return True if a menu item name belongs to the Chaniya fundraiser.

    Two patterns:
      - Any name containing "chaniya" (case-insensitive). Covers "Chaniya Taro XLB",
        "Chaniya Taro Milk Tea", "Chaniya Campaign: give $10 get $10".
      - Any name containing both "give $10" and "get $10". Covers Kizuki's
        "give $10 & get $10 voucher", where the `&` breaks a contiguous substring match.
    """
    if not name:
        return False
    lowered = name.lower()
    if "chaniya" in lowered:
        return True
    if "give $10" in lowered and "get $10" in lowered:
        return True
    return False


def _make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return s


def _fetch_orders_for_store(session, store, start_dt, end_dt):
    """Yield every order for one store within the [start_dt, end_dt) window."""
    page = 1
    while True:
        params = {
            "startDate": _iso_timestamp(start_dt),
            "endDate":   _iso_timestamp(end_dt),
            "page":      page,
            "pageSize":  PAGE_SIZE,
        }
        headers_backup = session.headers.copy()
        session.headers["Toast-Restaurant-External-ID"] = store["guid"]
        resp = session.get(ORDERS_BULK_URL, params=params)
        session.headers = headers_backup.copy()

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            logger.warning("  %s: 429 on page %s, sleeping %ss", store["name"], page, wait)
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RuntimeError(
                f"{store['name']}: HTTP {resp.status_code} on page {page}: {resp.text[:300]}"
            )

        data = resp.json()
        batch = data if isinstance(data, list) else data.get("orders", [])
        if not batch:
            return
        for order in batch:
            yield order
        page += 1


def _selection_contribution(sel) -> Decimal:
    """Net contribution (post-discount, post-refund) of one selection, or 0 if it doesn't apply."""
    if sel.get("voided"):
        return Decimal(0)
    if not _matches_menu_item(sel.get("displayName")):
        return Decimal(0)
    price = Decimal(str(sel.get("price") or 0))
    refund = Decimal(str((sel.get("refundDetails") or {}).get("refundAmount") or 0))
    return price - refund


def _service_charge_contribution(sc) -> Decimal:
    """Net round-up contribution of one appliedServiceCharge, or 0 if it's not a fundraiser."""
    if sc.get("serviceChargeCategory") != FUNDRAISING_CATEGORY:
        return Decimal(0)
    gross = Decimal(str(sc.get("chargeAmount") or 0))
    refund = Decimal(str((sc.get("refundDetails") or {}).get("refundAmount") or 0))
    return gross - refund


def _process_order(order):
    """Return (menu_item_total, round_up_total) for one order."""
    menu_total = Decimal(0)
    round_up_total = Decimal(0)
    if order.get("deleted"):
        return menu_total, round_up_total
    for check in order.get("checks", []):
        if check.get("voided") or check.get("deleted"):
            continue
        for sel in check.get("selections", []):
            menu_total += _selection_contribution(sel)
        for sc in check.get("appliedServiceCharges", []):
            round_up_total += _service_charge_contribution(sc)
    return menu_total, round_up_total


def _fetch_brand(cred, start_dt, end_dt):
    """Walk every store in a brand and sum fundraiser contributions."""
    brand = cred["brand"]
    logger.info("Authenticating %s...", brand)
    token = get_access_token(cred["clientId"], cred["clientSecret"], cred["userAccessType"])
    session = _make_session(token)

    summary = {
        "menu_item_sales": Decimal(0),
        "round_ups": Decimal(0),
        "by_store": {},
    }

    for store in BRAND_STORES[brand]:
        store_menu = Decimal(0)
        store_round = Decimal(0)
        n_orders = 0
        for order in _fetch_orders_for_store(session, store, start_dt, end_dt):
            m, r = _process_order(order)
            store_menu += m
            store_round += r
            n_orders += 1
        summary["menu_item_sales"] += store_menu
        summary["round_ups"] += store_round
        summary["by_store"][store["store_code"]] = {
            "name": store["name"],
            "n_orders": n_orders,
            "menu_item_sales": float(store_menu),
            "round_ups": float(store_round),
        }
        logger.info(
            "  %s/%s: %s orders, menu=$%.2f, round-ups=$%.2f",
            brand, store["store_code"], n_orders, store_menu, store_round,
        )

    return summary


# ---------- public API ----------

def fetch_total(window_start: datetime, window_end: datetime) -> dict:
    """Pull Toast's contribution to the fundraiser for the [window_start, window_end) interval.

    Args:
        window_start: timezone-aware datetime; start of the campaign window (inclusive).
        window_end:   timezone-aware datetime; end of the campaign window (exclusive).

    Returns:
        Source connector envelope expected by aggregate.py:
            {
                "source": "toast",
                "campaign_total": float (USD),
                "currency": "USD",
                "fetched_at_utc": ISO 8601 string,
                "raw_breakdown": {
                    "menu_item_sales": float,
                    "round_ups": float,
                    "by_brand": { brand: { menu_item_sales, round_ups, by_store } }
                }
            }
    """
    assert_credentials_loaded()

    grand_menu = Decimal(0)
    grand_round = Decimal(0)
    by_brand = {}

    for cred in CREDENTIALS:
        s = _fetch_brand(cred, window_start, window_end)
        by_brand[cred["brand"]] = {
            "menu_item_sales": float(s["menu_item_sales"]),
            "round_ups": float(s["round_ups"]),
            "by_store": s["by_store"],
        }
        grand_menu += s["menu_item_sales"]
        grand_round += s["round_ups"]

    total = grand_menu + grand_round
    return {
        "source": "toast",
        "campaign_total": float(total),
        "currency": "USD",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_breakdown": {
            "menu_item_sales": float(grand_menu),
            "round_ups": float(grand_round),
            "by_brand": by_brand,
        },
    }


# ---------- standalone smoke test ----------

if __name__ == "__main__":
    import json
    import sys
    from datetime import timedelta

    from dotenv import load_dotenv
    from zoneinfo import ZoneInfo

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()

    pt = ZoneInfo("America/Los_Angeles")

    if "--quick" in sys.argv:
        # Last 24 hours, useful for fast iteration.
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        print(f"[quick mode] window: {start.isoformat()} → {end.isoformat()}")
    else:
        # Full campaign: May 1 00:00 PT through May 31 23:59 PT (interval is [May 1, June 1) in PT).
        start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=pt)
        end   = datetime(2026, 6, 1, 0, 0, 0, tzinfo=pt)
        print(f"[full campaign] window: {start.isoformat()} → {end.isoformat()}")

    result = fetch_total(start, end)
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    print(f"\nGrand total: ${result['campaign_total']:.2f}")
    print(f"  menu items: ${result['raw_breakdown']['menu_item_sales']:.2f}")
    print(f"  round-ups:  ${result['raw_breakdown']['round_ups']:.2f}")
