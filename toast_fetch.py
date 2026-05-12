# toast_fetch.py
#
# Reference implementation: fetches sales of menu items named "Chaniya" from a
# single store between two dates. Kept as a reference while we build out the
# proper connector in sources/toast.py — that module will also handle the
# "give $10 get $10" item and POS round-up donations.

import time
import logging
import requests
from typing import List, Dict

from toast_config import ORDERS_BULK_URL, PAGE_SIZE

logger = logging.getLogger(__name__)

def fetch_gift_card_records_for_store(
    session: requests.Session,
    store_guid: str,
    start_date: str,
    end_date: str
) -> List[Dict]:
    """
    Fetches all 'Chaniya' line-items between start_date and end_date for a given store.
    Returns a list of dicts with keys:
      - order_guid
      - business_date
      - check_guid
      - quantity
      - amount
      - refund_amount
    """
    records: List[Dict] = []
    page = 1

    while True:
        params = {
            "startDate": start_date,
            "endDate":   end_date,
            "page":      page,
            "pageSize":  PAGE_SIZE,
        }

        # Inject the store header, preserving any existing headers
        headers_backup = session.headers.copy()
        session.headers["Toast-Restaurant-External-ID"] = store_guid

        resp = session.get(ORDERS_BULK_URL, params=params)
        if resp.status_code == 429:
            # Rate-limited → back off
            ra = resp.headers.get("Retry-After")
            wait = int(ra) if ra and ra.isdigit() else min(2 ** page, 60)
            logger.warning(f"429 on page {page}. Retrying after {wait}s.")
            time.sleep(wait)
            session.headers = headers_backup.copy()
            continue

        resp.raise_for_status()
        data = resp.json()

        # Support both {"orders": [...]} and bare-list responses
        if isinstance(data, dict):
            orders = data.get("orders", [])
        elif isinstance(data, list):
            orders = data
        else:
            orders = []

        if not orders:
            break

        for order in orders:
            order_guid    = order.get("guid")
            business_date = order.get("businessDate")
            for check in order.get("checks", []):
                check_guid = check.get("guid")
                for sel in check.get("selections", []):
                    item = sel.get("menuItem", {})
                    if item.get("name") != "Chaniya":
                        continue

                    rec = {
                        "order_guid":    order_guid,
                        "business_date": business_date,
                        "check_guid":    check_guid,
                        "quantity":      sel.get("quantity", 1),
                        "amount":        sel.get("amount", 0.0),
                        "refund_amount": sel.get("refundDetails", {})
                                               .get("refundAmount", 0.0),
                    }
                    records.append(rec)

        # Restore headers & advance to next page
        session.headers = headers_backup.copy()
        page += 1

    return records