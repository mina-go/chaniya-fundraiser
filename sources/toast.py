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

# Transient-error retry policy for Toast API page requests.
# Toast occasionally returns a one-off 5xx mid-pagination; retrying the page
# almost always succeeds and avoids discarding the whole multi-store pull.
MAX_RETRIES = 4          # total attempts per page before giving up
RETRY_BACKOFF_BASE = 3   # seconds; wait = RETRY_BACKOFF_BASE * 2^(attempt-1)
REQUEST_TIMEOUT = 30     # seconds per HTTP request

# Menu-item categories for the per-item breakdown in totals.json / snapshots.
# A selection that passes _matches_menu_item is bucketed via _categorize_menu_item.
# "other" catches anything that matches the broad filter but doesn't fit a named
# category — e.g. a future fundraiser item we haven't added a rule for. If "other"
# is ever nonzero in production it's a signal to investigate which new item appeared.
MENU_CATEGORY_MILK_TEA = "Chaniya Taro Milk Tea"
MENU_CATEGORY_XLB      = "Chaniya Taro XLB"
MENU_CATEGORY_VOUCHER  = "give $10 get $10"
MENU_CATEGORY_OTHER    = "other"

MENU_CATEGORIES = (
    MENU_CATEGORY_MILK_TEA,
    MENU_CATEGORY_XLB,
    MENU_CATEGORY_VOUCHER,
    MENU_CATEGORY_OTHER,
)


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


def _categorize_menu_item(name) -> str:
    """Bucket a fundraiser menu item into one of the named MENU_CATEGORIES.

    Assumes the name has already passed _matches_menu_item. Order matters:
    the voucher check runs before milk-tea / XLB so that the SD item literally
    named "Chaniya Campaign: give $10 get $10" lands in the voucher bucket
    instead of being miscategorized by a chaniya-prefix coincidence.
    """
    lowered = (name or "").lower()
    if "give $10" in lowered and "get $10" in lowered:
        return MENU_CATEGORY_VOUCHER
    if "milk tea" in lowered:
        return MENU_CATEGORY_MILK_TEA
    if "xlb" in lowered:
        return MENU_CATEGORY_XLB
    return MENU_CATEGORY_OTHER


def _empty_category_totals() -> dict:
    """Fresh dict with every named MENU_CATEGORY initialized to Decimal(0)."""
    return {cat: Decimal(0) for cat in MENU_CATEGORIES}


def _make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return s


def _fetch_page(session, store, params, page):
    """Fetch one page of orders for a store, retrying transient errors with backoff.

    Retryable: HTTP 429 (rate limit), HTTP 5xx (Toast server hiccups), and
    network-level errors (timeouts, connection resets). Non-retryable: 2xx
    (success) and 4xx (real client errors — retrying won't help).

    Raises RuntimeError only if every attempt is exhausted.
    """
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        headers_backup = session.headers.copy()
        session.headers["Toast-Restaurant-External-ID"] = store["guid"]
        try:
            resp = session.get(ORDERS_BULK_URL, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            # Network-level failure (timeout, connection reset, DNS). Treat as transient.
            session.headers = headers_backup.copy()
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "  %s: network error on page %s (attempt %s/%s): %s — retrying in %ss",
                    store["name"], page, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"{store['name']}: network error on page {page} after "
                f"{MAX_RETRIES} attempts: {exc}"
            )
        session.headers = headers_backup.copy()

        # Success or non-retryable client error (4xx) — stop retrying.
        if resp.status_code < 500 and resp.status_code != 429:
            break

        # Transient (429 or 5xx) — back off and retry, unless attempts are exhausted.
        if attempt < MAX_RETRIES:
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", str(RETRY_BACKOFF_BASE * attempt)))
            else:
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning(
                "  %s: HTTP %s on page %s (attempt %s/%s) — retrying in %ss",
                store["name"], resp.status_code, page, attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)

    if not resp.ok:
        raise RuntimeError(
            f"{store['name']}: HTTP {resp.status_code} on page {page} after "
            f"{MAX_RETRIES} attempts: {resp.text[:300]}"
        )
    return resp


def _fetch_orders_for_store(session, store, start_dt, end_dt):
    """Yield every order for one store within the [start_dt, end_dt) window.

    Each page is fetched via _fetch_page, which retries transient errors (429,
    5xx, network blips) with exponential backoff — so a single flaky page no
    longer discards the whole multi-store pull.
    """
    page = 1
    while True:
        params = {
            "startDate": _iso_timestamp(start_dt),
            "endDate":   _iso_timestamp(end_dt),
            "page":      page,
            "pageSize":  PAGE_SIZE,
        }
        resp = _fetch_page(session, store, params, page)

        data = resp.json()
        batch = data if isinstance(data, list) else data.get("orders", [])
        if not batch:
            return
        for order in batch:
            yield order
        page += 1


def _selection_contribution(sel):
    """Net contribution + category bucket of one selection.

    Returns (category, contribution_decimal). If the selection doesn't apply
    (voided, or not a fundraiser item) returns (None, Decimal(0)) so callers
    can cheaply skip it.
    """
    if sel.get("voided"):
        return None, Decimal(0)
    name = sel.get("displayName")
    if not _matches_menu_item(name):
        return None, Decimal(0)
    price = Decimal(str(sel.get("price") or 0))
    refund = Decimal(str((sel.get("refundDetails") or {}).get("refundAmount") or 0))
    return _categorize_menu_item(name), price - refund


def _service_charge_contribution(sc) -> Decimal:
    """Net round-up contribution of one appliedServiceCharge, or 0 if it's not a fundraiser."""
    if sc.get("serviceChargeCategory") != FUNDRAISING_CATEGORY:
        return Decimal(0)
    gross = Decimal(str(sc.get("chargeAmount") or 0))
    refund = Decimal(str((sc.get("refundDetails") or {}).get("refundAmount") or 0))
    return gross - refund


def _process_order(order):
    """Return (menu_by_category, round_up_total) for one order.

    menu_by_category is a dict keyed by MENU_CATEGORIES with Decimal values —
    each named bucket plus an "other" catch-all. round_up_total is the net
    fundraising service-charge amount on the order.
    """
    menu_by_cat = _empty_category_totals()
    round_up_total = Decimal(0)
    if order.get("deleted"):
        return menu_by_cat, round_up_total
    for check in order.get("checks", []):
        if check.get("voided") or check.get("deleted"):
            continue
        for sel in check.get("selections", []):
            category, contribution = _selection_contribution(sel)
            if category is not None:
                menu_by_cat[category] += contribution
        for sc in check.get("appliedServiceCharges", []):
            round_up_total += _service_charge_contribution(sc)
    return menu_by_cat, round_up_total


def _fetch_brand(cred, start_dt, end_dt):
    """Walk every store in a brand and sum fundraiser contributions, including per-item categories."""
    brand = cred["brand"]
    logger.info("Authenticating %s...", brand)
    token = get_access_token(cred["clientId"], cred["clientSecret"], cred["userAccessType"])
    session = _make_session(token)

    summary = {
        "menu_item_sales": Decimal(0),
        "menu_item_sales_by_category": _empty_category_totals(),
        "round_ups": Decimal(0),
        "by_store": {},
    }

    for store in BRAND_STORES[brand]:
        store_by_cat = _empty_category_totals()
        store_round = Decimal(0)
        n_orders = 0
        for order in _fetch_orders_for_store(session, store, start_dt, end_dt):
            order_by_cat, r = _process_order(order)
            for cat, amount in order_by_cat.items():
                store_by_cat[cat] += amount
            store_round += r
            n_orders += 1
        store_menu_total = sum(store_by_cat.values(), Decimal(0))
        summary["menu_item_sales"] += store_menu_total
        summary["round_ups"] += store_round
        for cat, amount in store_by_cat.items():
            summary["menu_item_sales_by_category"][cat] += amount
        summary["by_store"][store["store_code"]] = {
            "name": store["name"],
            "n_orders": n_orders,
            "menu_item_sales": float(store_menu_total),
            "menu_item_sales_by_category": {cat: float(v) for cat, v in store_by_cat.items()},
            "round_ups": float(store_round),
        }
        logger.info(
            "  %s/%s: %s orders, menu=$%.2f, round-ups=$%.2f",
            brand, store["store_code"], n_orders, store_menu_total, store_round,
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
                    "menu_item_sales_by_category": {
                        "Chaniya Taro Milk Tea": float,
                        "Chaniya Taro XLB": float,
                        "give $10 get $10": float,
                        "other": float,
                    },
                    "round_ups": float,
                    "by_brand": {
                        brand: {
                            "menu_item_sales": float,
                            "menu_item_sales_by_category": {...same shape...},
                            "round_ups": float,
                            "by_store": {
                                store_code: {
                                    ..., "menu_item_sales_by_category": {...}
                                }
                            },
                        }
                    }
                }
            }
    """
    assert_credentials_loaded()

    grand_menu = Decimal(0)
    grand_menu_by_cat = _empty_category_totals()
    grand_round = Decimal(0)
    by_brand = {}

    for cred in CREDENTIALS:
        s = _fetch_brand(cred, window_start, window_end)
        by_brand[cred["brand"]] = {
            "menu_item_sales": float(s["menu_item_sales"]),
            "menu_item_sales_by_category": {cat: float(v) for cat, v in s["menu_item_sales_by_category"].items()},
            "round_ups": float(s["round_ups"]),
            "by_store": s["by_store"],
        }
        grand_menu += s["menu_item_sales"]
        grand_round += s["round_ups"]
        for cat, amount in s["menu_item_sales_by_category"].items():
            grand_menu_by_cat[cat] += amount

    total = grand_menu + grand_round
    return {
        "source": "toast",
        "campaign_total": float(total),
        "currency": "USD",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_breakdown": {
            "menu_item_sales": float(grand_menu),
            "menu_item_sales_by_category": {cat: float(v) for cat, v in grand_menu_by_cat.items()},
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
    for cat, amount in result['raw_breakdown']['menu_item_sales_by_category'].items():
        print(f"    {cat}: ${amount:.2f}")
    print(f"  round-ups:  ${result['raw_breakdown']['round_ups']:.2f}")
