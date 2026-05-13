"""Diagnostic: dump what GoFundMe actually returns to a plain `requests` client.

The scraper failed to parse because my regex assumed the visible "$X,XXX raised"
text. The actual static HTML is likely a Next.js shell with the data embedded
in a JSON blob. This script finds where in the response the raised amount lives
so we can update the regexes.

Run from project root:
    python3 inspect_gofundme.py
"""

import re

import requests

URL = "https://www.gofundme.com/f/standing-with-chaniya-through-her-recovery"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

resp = requests.get(URL, headers={"User-Agent": UA}, timeout=30)
print(f"HTTP {resp.status_code}, body length: {len(resp.text)} chars\n")

html = resp.text

# 1. Anti-bot block?
print("--- anti-bot / Cloudflare check ---")
lowered = html.lower()
if any(k in lowered for k in ["checking your browser", "challenge", "captcha"]):
    print("  Possible bot challenge in response.")
else:
    print("  No obvious bot block.")

# 2. Any visible "raised" context?
print("\n--- contexts around 'raised' (first 10) ---")
found = list(re.finditer(r".{40}raised.{40}", html, re.IGNORECASE))[:10]
if not found:
    print("  (no occurrences of 'raised')")
for m in found:
    print(f"  {m.group()!r}")

# 3. Distinct dollar amounts on the page.
print("\n--- distinct dollar amounts (top 20) ---")
amounts = sorted(set(re.findall(r"\$[\d,]+(?:\.\d+)?", html)))[:20]
for a in amounts:
    print(f"  {a}")

# 4. JSON-embedded amount-y fields.
print("\n--- JSON-style amount fields (first 3 of each) ---")
candidates = [
    "current_amount", "currentAmount",
    "amount_raised", "amountRaised",
    "raised_amount", "raisedAmount",
    "totalRaised", "total_raised",
    "campaign_amount_raised",
    "amount",
    "goalAmount", "goal_amount",
]
for field in candidates:
    hits = re.findall(rf'"{field}"\s*:\s*[^,}}\]]{{1,120}}', html)[:3]
    if hits:
        print(f"  '{field}':")
        for h in hits:
            print(f"    {h}")

# 5. __NEXT_DATA__ check.
print("\n--- __NEXT_DATA__ blob ---")
match = re.search(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    html, re.DOTALL,
)
if match:
    blob = match.group(1)
    print(f"  Found __NEXT_DATA__, length: {len(blob)} chars")
    # Show first 2KB to inspect shape
    print(f"  First 1500 chars:\n{blob[:1500]}")
else:
    print("  No __NEXT_DATA__ tag found.")
