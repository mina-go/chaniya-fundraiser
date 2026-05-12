"""Toast API configuration.

Credentials are read from environment variables at runtime — never hardcoded.
See .env.example for the full list of required variables.
"""

import os

# API endpoints
ORDERS_BULK_URL = "https://ws-api.toasttab.com/orders/v2/ordersBulk"
PAYMENTS_URL = "https://ws-api.toasttab.com/orders/v2/payments"
PAYMENT_DETAIL_URL = "https://ws-api.toasttab.com/orders/v2/payments/{guid}"

# Request settings
PAGE_SIZE = 100  # max records per page

# Date/time formats Toast expects
DATE_FORMAT = "%Y-%m-%d"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.000+0000"

# API credentials, read from environment variables at runtime.
# Set these in .env locally and in GitHub Actions Secrets in CI.
CREDENTIALS = [
    {
        "brand": "Kizuki",
        "clientId": os.environ.get("TOAST_KIZUKI_CLIENT_ID", ""),
        "clientSecret": os.environ.get("TOAST_KIZUKI_CLIENT_SECRET", ""),
        "userAccessType": "TOAST_MACHINE_CLIENT",
    },
    {
        "brand": "SD",
        "clientId": os.environ.get("TOAST_SD_CLIENT_ID", ""),
        "clientSecret": os.environ.get("TOAST_SD_CLIENT_SECRET", ""),
        "userAccessType": "TOAST_MACHINE_CLIENT",
    },
]

# All store locations participating in the Chaniya fundraiser.
# Store GUIDs are not secret — these are just identifiers Toast needs to scope each API call.
BRAND_STORES = {
    "Kizuki": [
        {"store_code": "BKBEL", "guid": "7026f5b7-2a85-46d4-af09-91a9e283b7da", "name": "Kizuki Bellevue",          "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "CHSLU", "guid": "10ede3f7-98aa-448c-b4aa-4611bdf02f2f", "name": "Kizuki Capitol Hill",      "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "REDMN", "guid": "a986ab85-ff88-4496-a4d0-414c57d999fc", "name": "Kizuki Redmond",           "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "WSEAT", "guid": "4f1dd6d2-92c2-4d51-a979-0e54e66d4a9d", "name": "Kizuki West Seattle",      "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "KZALD", "guid": "5591fde4-b43a-429f-82cb-fe9b1719f587", "name": "Kizuki Alderwood Mall",    "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "BSQML", "guid": "1b6cf506-d62e-4350-931e-48b87f7e8d58", "name": "Kizuki Bellevue Square",   "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "NGKMD", "guid": "ea11da20-7679-4ae2-b8fc-cd2ca417d588", "name": "Kizuki Northgate",         "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "RNTCK", "guid": "b07f693e-3897-4bb9-80ea-49dc5e84abd6", "name": "Kizuki Renton",            "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "SCMLL", "guid": "c8f41a98-21cd-4170-ad46-1f2a47d9032f", "name": "Kizuki South Center",      "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "TCMLL", "guid": "ac18500b-8bba-4d73-9eb1-911ea08ebdc7", "name": "Kizuki Tacoma",            "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "BVORG", "guid": "1405e368-5501-4d91-a644-4dfa968cf276", "name": "Kizuki Beaverton",         "state": "OR", "timezone": "America/Los_Angeles"},
        {"store_code": "PFHDX", "guid": "5c2a3eb1-f64c-4594-a380-2e182614bad2", "name": "Kizuki Portland Food Hall","state": "OR", "timezone": "America/Los_Angeles"},
        {"store_code": "UPORG", "guid": "9f7935dc-72f5-4eef-be83-3a264a2078e0", "name": "Kizuki Uptown",            "state": "OR", "timezone": "America/Los_Angeles"},
        {"store_code": "KTTEX", "guid": "546fc7e1-ed04-45ed-9cb5-7fc6d595647d", "name": "Kizuki Katy",              "state": "TX", "timezone": "America/Chicago"},
        {"store_code": "LGTEX", "guid": "761fe0ee-555e-4277-9c5c-20f1eaa89021", "name": "Kizuki Legacy",            "state": "TX", "timezone": "America/Chicago"},
        {"store_code": "KZCST", "guid": "860317c3-bf6f-4175-989a-1e76c56dece6", "name": "Kizuki Stonestown",        "state": "CA", "timezone": "America/Los_Angeles"},
    ],
    "SD": [
        {"store_code": "SDBEL", "guid": "1d3b721b-f358-43dc-abb6-195b943afd8c", "name": "Supreme Dumplings Bellevue",   "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "SDKLD", "guid": "b2a5d824-603b-4c5c-9336-825272c3eac3", "name": "Supreme Dumplings Kirkland",   "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "SDSLU", "guid": "b0a9c540-ec58-465d-ad18-b15aecdeb71d", "name": "Supreme Dumplings SLU",        "state": "WA", "timezone": "America/Los_Angeles"},
        {"store_code": "SDCST", "guid": "bbb0a156-0e37-47c7-b0f0-16b5a7c3ae92", "name": "Supreme Dumplings Stonestown", "state": "CA", "timezone": "America/Los_Angeles"},
        {"store_code": "SDTKT", "guid": "3ccd07f2-1bca-415c-a23b-83010c027cdc", "name": "Supreme Dumplings Katy",       "state": "TX", "timezone": "America/Chicago"},
    ],
}


def assert_credentials_loaded():
    """Sanity check that the required env vars are set. Call at startup."""
    missing = [
        f"{c['brand']} (clientId or clientSecret)"
        for c in CREDENTIALS
        if not c["clientId"] or not c["clientSecret"]
    ]
    if missing:
        raise RuntimeError(
            "Missing Toast credentials in environment for: "
            + ", ".join(missing)
            + ". Check your .env file or GitHub Actions Secrets."
        )
