"""Find the QR Payment Link's plink_ ID by inspecting recent Checkout Sessions.

Uses checkout_sessions:read (which we already have) instead of payment_links:read.
Lists every distinct payment_link ID seen on recent sessions with session counts —
the most-active one is almost certainly your QR donation link.

Run from project root:
    python3 find_stripe_link.py
"""

import os
import sys
from collections import Counter

import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("STRIPE_API_KEY", "").strip()
if not api_key:
    print("Set STRIPE_API_KEY in .env first.", file=sys.stderr)
    sys.exit(1)


def fetch_recent_sessions(limit_per_page=100, max_pages=10):
    url = "https://api.stripe.com/v1/checkout/sessions"
    params = {"limit": limit_per_page}
    for _ in range(max_pages):
        resp = requests.get(url, auth=(api_key, ""), params=params, timeout=15)
        if not resp.ok:
            print(f"Stripe API error (HTTP {resp.status_code}): {resp.text[:300]}",
                  file=sys.stderr)
            sys.exit(1)
        body = resp.json()
        for s in body.get("data", []):
            yield s
        if not body.get("has_more") or not body.get("data"):
            return
        params["starting_after"] = body["data"][-1]["id"]


counts = Counter()
example_amounts = {}  # plink_id -> latest amount_total in cents
total_sessions = 0
total_with_link = 0

for session in fetch_recent_sessions():
    total_sessions += 1
    plink = session.get("payment_link")
    if plink:
        counts[plink] += 1
        total_with_link += 1
        # Track the latest amount we've seen for each plink (helps identify by typical donation size)
        if plink not in example_amounts:
            example_amounts[plink] = session.get("amount_total", 0)

print(f"\nScanned {total_sessions} recent Checkout Sessions ({total_with_link} via Payment Links)\n")

if not counts:
    print(
        "No sessions found that came from a Payment Link. Possibilities:\n"
        "  - The QR Payment Link has had no donations yet (in the recent sessions we scanned)\n"
        "  - Sessions are older than the most recent 1000\n"
        "  - The restricted API key is scoped to a different account than the Payment Link"
    )
    sys.exit(0)

print(f"{'plink_ID':<32} {'sessions':<10} {'sample amount'}")
print("-" * 60)
for plink, count in counts.most_common():
    sample = example_amounts.get(plink, 0)
    print(f"{plink:<32} {count:<10} ${sample/100:.2f}")

best, best_count = counts.most_common(1)[0]
print()
if len(counts) == 1:
    print(f"Only one Payment Link active. Put this in .env:\n  STRIPE_PAYMENT_LINK_ID={best}")
else:
    print(
        f"Most active Payment Link ({best_count} sessions): {best}\n"
        f"If that matches your QR donation pattern, use it. If not, pick the one whose "
        f"sample amount looks like a typical donation."
    )
