"""Stripe fundraiser connector.

Pulls QR-code donations: every Checkout Session created from the QR Stripe Payment
Link, within the campaign window, that completed successfully.

We identify donations by Payment Link ID rather than by metadata, so we catch every
donation through the link automatically — no setup needed on the Payment Link side,
and historical donations from before we built the tracker are included automatically.

Money math:
  - Sum `amount_total` (in cents) for sessions where status=complete and payment_status=paid
  - Divide by 100 for dollars
  - Refunds are not subtracted in v1 (QR donation refunds are rare). To add later,
    chase from session.payment_intent → refunds.
"""

# Make the project root importable whether this file is run as a script or module.
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


STRIPE_API_BASE = "https://api.stripe.com/v1"
PAGE_LIMIT = 100  # Stripe max for list endpoints


# ---------- helpers ----------

def _config():
    """Read Stripe credentials from environment. Raises if missing."""
    api_key = os.environ.get("STRIPE_API_KEY", "").strip()
    payment_link = os.environ.get("STRIPE_PAYMENT_LINK_ID", "").strip()
    if not api_key or not payment_link:
        raise RuntimeError(
            "Missing Stripe config. Set STRIPE_API_KEY and STRIPE_PAYMENT_LINK_ID in .env "
            "(local) or GitHub Actions Secrets (CI).\n"
            "STRIPE_API_KEY: Stripe Dashboard → Developers → API keys → Create restricted key "
            "with permission 'Checkout sessions: Read'.\n"
            "STRIPE_PAYMENT_LINK_ID: Dashboard → Payment Links → click your QR link → ID "
            "is in the URL after /payment-links/ (starts with 'plink_')."
        )

    # Normalize: Stripe Payment Link IDs always have a "plink_" prefix. Easy to miss
    # when copying just the suffix from a URL — auto-add it if it's not already there.
    if not payment_link.startswith("plink_"):
        payment_link = f"plink_{payment_link}"

    return api_key, payment_link


def _make_session(api_key):
    s = requests.Session()
    s.auth = (api_key, "")  # Stripe uses HTTP Basic with key as username, empty password
    s.headers.update({"Accept": "application/json"})
    return s


def _to_unix(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _fetch_sessions(session, payment_link, start_dt, end_dt):
    """Yield every Checkout Session from this Payment Link in [start_dt, end_dt)."""
    url = f"{STRIPE_API_BASE}/checkout/sessions"
    params = {
        "payment_link": payment_link,
        "created[gte]": _to_unix(start_dt),
        "created[lt]":  _to_unix(end_dt),
        "limit": PAGE_LIMIT,
    }

    page = 0
    while True:
        page += 1
        resp = session.get(url, params=params)
        if not resp.ok:
            raise RuntimeError(
                f"Stripe sessions list failed (HTTP {resp.status_code}): {resp.text[:400]}"
            )
        body = resp.json()
        batch = body.get("data", [])
        logger.info("  page %s: %s sessions", page, len(batch))
        for s in batch:
            yield s
        if not body.get("has_more") or not batch:
            return
        # Cursor pagination — pass last seen ID as starting_after on next page.
        params["starting_after"] = batch[-1]["id"]


def _session_contribution(s) -> Decimal:
    """Net contribution from one Checkout Session, in dollars."""
    if s.get("status") != "complete":
        return Decimal(0)
    if s.get("payment_status") != "paid":
        return Decimal(0)
    amount_total_cents = Decimal(s.get("amount_total") or 0)
    return amount_total_cents / 100


# ---------- public API ----------

def fetch_total(window_start: datetime, window_end: datetime) -> dict:
    """Pull Stripe's contribution to the fundraiser for [window_start, window_end)."""
    api_key, payment_link = _config()
    session = _make_session(api_key)

    logger.info(
        "Fetching Stripe checkout sessions from Payment Link %s (window: %s → %s)",
        payment_link, window_start.isoformat(), window_end.isoformat(),
    )

    grand_total = Decimal(0)
    n_sessions = 0
    n_paid = 0
    n_skipped = 0

    for s in _fetch_sessions(session, payment_link, window_start, window_end):
        n_sessions += 1
        if s.get("status") == "complete" and s.get("payment_status") == "paid":
            n_paid += 1
            grand_total += _session_contribution(s)
        else:
            n_skipped += 1

    logger.info(
        "Stripe totals: %s sessions (%s paid, %s incomplete), net=$%.2f",
        n_sessions, n_paid, n_skipped, grand_total,
    )

    return {
        "source": "stripe",
        "campaign_total": float(grand_total),
        "currency": "USD",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_breakdown": {
            "net_amount": float(grand_total),
            "n_sessions_scanned": n_sessions,
            "n_sessions_paid": n_paid,
            "n_sessions_incomplete_or_unpaid": n_skipped,
            "payment_link_id": payment_link,
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
        end   = datetime(2026, 6, 1, 0, 0, 0, tzinfo=pt)
        print(f"[full campaign] window: {start.isoformat()} → {end.isoformat()}")

    result = fetch_total(start, end)
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    print(f"\nGrand total: ${result['campaign_total']:.2f}")
