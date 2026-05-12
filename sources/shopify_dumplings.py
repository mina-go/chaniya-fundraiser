"""Shopify fundraiser connector.

All Shopify sales during the campaign window count toward the fundraiser
(no per-product filtering). Returns the sum of every order's subtotal_price
minus any refunded product amounts.

Subtotal is the post-discount, pre-tax, pre-shipping product total — same
"what the customer paid for products" interpretation we used for Toast.

Shopify uses a separate Stripe account from the QR donation Stripe account,
so no double-counting risk between this source and the Stripe connector.
"""

# Make the project root importable whether this file is run as a script
# (`python sources/shopify_dumplings.py`) or as a module.
import os as _os
import sys as _sys
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import requests

# Load .env automatically if python-dotenv is available.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


# Pinned Shopify Admin API version. Update yearly or as needed.
SHOPIFY_API_VERSION = "2024-10"

# Max records per page (Shopify max is 250 for orders).
PAGE_SIZE = 250


# ---------- helpers ----------

def _config():
    """Read Shopify Dev Dashboard credentials from environment. Raises if missing or malformed.

    Required env vars:
      SHOPIFY_CLIENT_ID      - From Dev Dashboard → app → Settings → Credentials
      SHOPIFY_CLIENT_SECRET  - Same place
      SHOPIFY_STORE_DOMAIN   - The store's *.myshopify.com domain or bare handle

    Legacy custom apps (with their `shpat_` Admin API access tokens) were deprecated
    Jan 1 2026; the new flow exchanges Client ID + Secret for a 24-hour access token
    via the OAuth `client_credentials` grant.
    """
    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    domain = os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip()
    if not client_id or not client_secret or not domain:
        raise RuntimeError(
            "Missing Shopify credentials. Set SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, "
            "and SHOPIFY_STORE_DOMAIN in .env (local) or GitHub Actions Secrets (CI).\n"
            "Find Client ID and Secret in Shopify Dev Dashboard → your app → Settings → Credentials."
        )

    # Strip protocol and trailing slash if someone pasted a full URL.
    domain = domain.replace("https://", "").replace("http://", "").rstrip("/")

    if domain.endswith(".myshopify.com"):
        return client_id, client_secret, domain
    if "." not in domain:
        return client_id, client_secret, f"{domain}.myshopify.com"

    raise RuntimeError(
        f"SHOPIFY_STORE_DOMAIN must be the *.myshopify.com domain, not a custom "
        f"storefront domain. Got: {domain!r}.\n"
        f"In the Dev Dashboard sidebar under 'Dev stores' or in the admin URL "
        f"(https://admin.shopify.com/store/<handle>), the <handle> is what you want."
    )


def _get_access_token(client_id, client_secret, domain):
    """Exchange Client ID + Secret for a 24-hour Admin API access token.

    Uses the OAuth `client_credentials` grant. Each hourly run gets a fresh token.
    """
    url = f"https://{domain}/admin/oauth/access_token"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if not resp.ok:
        body = resp.text[:400]
        if "shop_not_permitted" in body.lower():
            raise RuntimeError(
                f"Shopify token exchange failed: shop_not_permitted (HTTP {resp.status_code}).\n"
                f"The app and the store must be in the same Shopify organization, and the "
                f"app must be installed on this store. Check in Dev Dashboard: 'Dev stores' "
                f"sidebar should list '{domain}', and the app should be installed on it."
            )
        raise RuntimeError(f"Shopify token exchange failed: HTTP {resp.status_code}: {body}")

    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Shopify token exchange returned no access_token: {payload}")
    logger.info(
        "Got Shopify access token (scope=%s, expires_in=%ss)",
        payload.get("scope", "?"), payload.get("expires_in", "?"),
    )
    return token


def _make_session(token):
    s = requests.Session()
    s.headers.update({
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
    })
    return s


def _fetch_orders(session, domain, start_dt, end_dt):
    """Yield every Shopify order created in the [start_dt, end_dt) window.

    Uses Shopify's cursor-based pagination via the Link response header.
    """
    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",                       # include cancelled too; we filter ourselves
        "created_at_min": start_dt.isoformat(),
        "created_at_max": end_dt.isoformat(),
        "limit": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        resp = session.get(url, params=params)
        if not resp.ok:
            raise RuntimeError(
                f"Shopify orders: HTTP {resp.status_code} on page {page}: {resp.text[:300]}"
            )

        data = resp.json()
        batch = data.get("orders", [])
        logger.info("  page %s: %s orders", page, len(batch))
        for order in batch:
            yield order

        # On subsequent pages, use the URL from the Link header verbatim — it already
        # includes the original query params plus the page_info cursor.
        url = resp.links.get("next", {}).get("url")
        params = None


def _refunded_subtotal(order):
    """Sum the post-discount, pre-tax refund amount across all refunds on an order."""
    refunded = Decimal(0)
    for refund in order.get("refunds") or []:
        for rli in refund.get("refund_line_items") or []:
            # Prefer the MoneyBag shape (modern API); fall back to the flat field.
            amount = (rli.get("subtotal_set") or {}).get("shop_money", {}).get("amount")
            if amount is None:
                amount = rli.get("subtotal")
            refunded += Decimal(str(amount or "0"))
    return refunded


def _order_contribution(order):
    """Net subtotal contribution for one order (0 if the order shouldn't count)."""
    if order.get("test"):
        return Decimal(0)
    if order.get("cancelled_at"):
        return Decimal(0)
    subtotal = Decimal(str(order.get("subtotal_price") or "0"))
    return subtotal - _refunded_subtotal(order)


# ---------- public API ----------

def fetch_total(window_start: datetime, window_end: datetime) -> dict:
    """Pull Shopify's contribution to the fundraiser for [window_start, window_end).

    Args:
        window_start: timezone-aware datetime; start of the campaign window (inclusive).
        window_end:   timezone-aware datetime; end of the campaign window (exclusive).

    Returns:
        Source connector envelope:
            {
                "source": "shopify",
                "campaign_total": float (USD),
                "currency": "USD",
                "fetched_at_utc": ISO 8601 string,
                "raw_breakdown": {
                    "subtotal_minus_refunds": float,
                    "n_orders_scanned": int,
                    "n_orders_skipped_test_or_cancelled": int,
                    "n_orders_with_refunds": int,
                }
            }
    """
    client_id, client_secret, domain = _config()
    token = _get_access_token(client_id, client_secret, domain)
    session = _make_session(token)

    logger.info(
        "Fetching Shopify orders from %s (window: %s → %s)",
        domain, window_start.isoformat(), window_end.isoformat(),
    )

    grand_total = Decimal(0)
    n_orders = 0
    n_skipped = 0
    n_with_refunds = 0

    for order in _fetch_orders(session, domain, window_start, window_end):
        n_orders += 1
        if order.get("refunds"):
            n_with_refunds += 1
        if order.get("test") or order.get("cancelled_at"):
            n_skipped += 1
            continue
        grand_total += _order_contribution(order)

    logger.info(
        "Shopify totals: %s orders, %s skipped (test/cancelled), "
        "%s had refunds, net=$%.2f",
        n_orders, n_skipped, n_with_refunds, grand_total,
    )

    return {
        "source": "shopify",
        "campaign_total": float(grand_total),
        "currency": "USD",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_breakdown": {
            "subtotal_minus_refunds": float(grand_total),
            "n_orders_scanned": n_orders,
            "n_orders_skipped_test_or_cancelled": n_skipped,
            "n_orders_with_refunds": n_with_refunds,
        },
    }


# ---------- standalone smoke test ----------

if __name__ == "__main__":
    import json
    import sys
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pt = ZoneInfo("America/Los_Angeles")

    if "--quick" in sys.argv:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        print(f"[quick mode] window: {start.isoformat()} → {end.isoformat()}")
    else:
        start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=pt)
        end = datetime(2026, 6, 1, 0, 0, 0, tzinfo=pt)
        print(f"[full campaign] window: {start.isoformat()} → {end.isoformat()}")

    result = fetch_total(start, end)
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    print(f"\nGrand total: ${result['campaign_total']:.2f}")
