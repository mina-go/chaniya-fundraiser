"""Aggregator — orchestrates every source connector and writes the live total.

Runs every hour via .github/workflows/hourly.yml. Each run:
  1. Calls fetch_total(window_start, window_end) on each source module.
  2. Falls back to that source's last-known-good value from snapshots/ if it fails.
  3. Sums all sources into a grand_total.
  4. Writes public/totals.json (what the Wix embed reads).
  5. Writes a timestamped audit snapshot to snapshots/.

Re-running is idempotent and self-correcting: each call re-queries the full
campaign window from each source, so a failed run is harmless and the next
successful run produces the right answer.

Usage:
    python3 aggregate.py            # full campaign window
"""

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Ensure project root is on sys.path so `from sources import ...` works.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sources import gofundme, shopify_dumplings, stripe_donations, toast  # noqa: E402

logger = logging.getLogger(__name__)


# ---------- campaign config ----------

CAMPAIGN_ID = "chaniya-may-2026"
TIMEZONE = ZoneInfo("America/Los_Angeles")
CAMPAIGN_START = datetime(2026, 5, 1, 0, 0, 0, tzinfo=TIMEZONE)
CAMPAIGN_END   = datetime(2026, 6, 1, 0, 0, 0, tzinfo=TIMEZONE)

# Sources to query, in order.
SOURCES = {
    "toast":    toast,
    "shopify":  shopify_dumplings,
    "stripe":   stripe_donations,
    "gofundme": gofundme,
}

# Output locations.
ROOT          = Path(__file__).parent
PUBLIC_DIR    = ROOT / "public"
SNAPSHOTS_DIR = ROOT / "snapshots"
TOTALS_PATH   = PUBLIC_DIR / "totals.json"


# ---------- helpers ----------

def _load_last_known_good(source_name):
    """Walk snapshots newest → oldest, return the most recent non-failed entry for a source."""
    if not SNAPSHOTS_DIR.exists():
        return None
    for snap_path in sorted(SNAPSHOTS_DIR.iterdir(), reverse=True):
        if not snap_path.is_file() or not snap_path.name.endswith(".json"):
            continue
        try:
            data = json.loads(snap_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        entry = (data.get("by_source") or {}).get(source_name)
        if entry and not entry.get("failed"):
            return entry
    return None


def _fetch_one(name, module, start, end):
    """Call one connector. On failure, fall back to last-known-good with degraded flag."""
    try:
        logger.info("=== %s ===", name)
        result = module.fetch_total(start, end)
        result["failed"] = False
        return result
    except Exception as exc:
        logger.error("%s failed: %s", name, exc)
        logger.debug(traceback.format_exc())
        fallback = _load_last_known_good(name)
        if fallback:
            logger.warning(
                "  using last-known-good for %s ($%.2f from %s)",
                name, fallback["campaign_total"], fallback.get("fetched_at_utc", "?"),
            )
            return {
                **fallback,
                "failed": True,
                "failure_reason": str(exc),
                "stale_since_utc": fallback.get("fetched_at_utc"),
            }
        logger.warning("  no prior snapshot found for %s — reporting $0", name)
        return {
            "source": name,
            "campaign_total": 0.0,
            "currency": "USD",
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "raw_breakdown": None,
            "failed": True,
            "failure_reason": str(exc),
        }


# ---------- public API ----------

def aggregate(start: datetime = None, end: datetime = None) -> dict:
    """Pull every source, sum, write totals.json + snapshot, return the result dict."""
    start = start or CAMPAIGN_START
    end = end or CAMPAIGN_END
    now_utc = datetime.now(timezone.utc)

    by_source = {name: _fetch_one(name, mod, start, end) for name, mod in SOURCES.items()}

    grand_total = round(sum(s["campaign_total"] for s in by_source.values()), 2)
    degraded = any(s.get("failed") for s in by_source.values())

    payload = {
        "campaign": CAMPAIGN_ID,
        "campaign_window_local": {
            "start": start.isoformat(),
            "end":   end.isoformat(),
            "timezone": str(TIMEZONE),
        },
        "grand_total": grand_total,
        "currency": "USD",
        "fetched_at_utc": now_utc.isoformat(),
        "degraded": degraded,
        "by_source": by_source,
    }

    # Write the live total Wix reads.
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    TOTALS_PATH.write_text(json.dumps(payload, indent=2))
    logger.info("Wrote %s ($%.2f%s)",
                TOTALS_PATH, grand_total, " [degraded]" if degraded else "")

    # Append an audit snapshot.
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snap_name = now_utc.strftime("%Y-%m-%dT%H-%M-%SZ.json")
    (SNAPSHOTS_DIR / snap_name).write_text(json.dumps(payload, indent=2))
    logger.info("Wrote snapshots/%s", snap_name)

    return payload


# ---------- CLI ----------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = aggregate()
    print("\n=== GRAND TOTAL ===")
    print(f"${result['grand_total']:,.2f}{'  [DEGRADED]' if result['degraded'] else ''}")
    print()
    for name, s in result["by_source"].items():
        flag = "  [failed, using last-known-good]" if s.get("failed") else ""
        print(f"  {name:<10} ${s['campaign_total']:>10,.2f}{flag}")
