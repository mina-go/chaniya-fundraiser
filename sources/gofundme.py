"""GoFundMe fundraiser connector.

GoFundMe has no API, so we fetch the campaign page and extract the "raised" total
from the HTML. The number appears literally as "$11,632 raised of $13K" — we match
that pattern with a regex, falling back to a JSON-embedded amount if the visible
markup ever shifts.

Note: GoFundMe gives us only the *all-time* campaign total since the campaign's
start date (April 9 2026 for this campaign). We can't filter by date the way we
can with Toast / Shopify / Stripe. The window arguments are accepted for API
consistency but the returned total is always all-time. Since this GoFundMe is
dedicated entirely to Chaniya's fundraiser, all of it counts toward the total.

If the scraper fails — GoFundMe changing markup, Cloudflare interfering, etc. —
fetch_total raises a clear exception. The aggregator is responsible for falling
back to the last-known-good snapshot.
"""

# Make project root importable
import os as _os
import sys as _sys
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


DEFAULT_URL = "https://www.gofundme.com/f/standing-with-chaniya-through-her-recovery"

# Realistic browser User-Agent — GoFundMe will sometimes block stripped-down clients.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Primary: JSON-embedded `currentAmount.amount` field in GoFundMe's Next.js page.
# Structure: "currentAmount":{"__typename":"Money","amount":11632,"currencyCode":"USD"}
# We non-greedily match up to the next "}" so we don't accidentally pick up goalAmount.
JSON_CURRENT_AMOUNT_RE = re.compile(
    r'"currentAmount"\s*:\s*\{[^}]*?"amount"\s*:\s*([\d.]+)',
    re.DOTALL,
)

# Fallback: the visible text. GoFundMe wraps the number in a <span> and inserts
# an HTML comment between it and "raised", so we tolerate arbitrary tag/whitespace gunk.
HTML_RAISED_RE = re.compile(
    r"\$([\d,]+(?:\.\d+)?)(?:\s*</[^>]+>)?\s*(?:<!--[^>]*-->)?\s*raised",
    re.IGNORECASE,
)


# ---------- helpers ----------

def _campaign_url():
    return os.environ.get("GOFUNDME_CAMPAIGN_URL", "").strip() or DEFAULT_URL


def _fetch_html(url):
    resp = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"GoFundMe fetch failed: HTTP {resp.status_code}. "
            f"Possible causes: page moved, GoFundMe blocking the request, or network issue."
        )
    return resp.text


def _parse_raised(html):
    """Try several strategies to extract the raised amount. Returns (Decimal, strategy_name)."""
    # 1. Primary: structural JSON match on currentAmount.amount.
    m = JSON_CURRENT_AMOUNT_RE.search(html)
    if m:
        return Decimal(m.group(1)), "json_currentAmount"

    # 2. Fallback: visible-text "$X,XXX raised" with tolerance for inline HTML.
    m = HTML_RAISED_RE.search(html)
    if m:
        return Decimal(m.group(1).replace(",", "")), "html_visible_text"

    raise RuntimeError(
        "Could not parse 'raised' amount from GoFundMe page. The HTML structure may have "
        "changed — run `python3 inspect_gofundme.py` and update the regex patterns "
        "in sources/gofundme.py based on what it finds."
    )


# ---------- public API ----------

def fetch_total(window_start: datetime, window_end: datetime) -> dict:
    """Pull the current 'raised' total from the GoFundMe campaign page.

    The window args are accepted for interface consistency with other connectors
    but are ignored — GoFundMe only exposes the all-time campaign total.
    """
    url = _campaign_url()
    logger.info("Fetching GoFundMe campaign page: %s", url)
    html = _fetch_html(url)
    amount, source_strategy = _parse_raised(html)
    logger.info("Parsed $%.2f from GoFundMe via %s", amount, source_strategy)

    return {
        "source": "gofundme",
        "campaign_total": float(amount),
        "currency": "USD",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_breakdown": {
            "raised_all_time": float(amount),
            "campaign_url": url,
            "parse_strategy": source_strategy,
        },
    }


# ---------- standalone smoke test ----------

if __name__ == "__main__":
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pt = ZoneInfo("America/Los_Angeles")

    # Window args don't matter for GoFundMe — same number every time.
    start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=pt)
    end = datetime(2026, 6, 1, 0, 0, 0, tzinfo=pt)

    result = fetch_total(start, end)
    print(json.dumps(result, indent=2))
    print(f"\nGrand total: ${result['campaign_total']:.2f}")
