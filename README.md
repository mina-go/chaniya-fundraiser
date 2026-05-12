# Chaniya Fundraising Tracker

Live, hourly-updating fundraising tracker for Chaniya's May 2026 campaign. Aggregates totals from four independent sources into a single number displayed on the Wix site.

**Campaign window:** May 1 00:00 PT → May 31 23:59 PT (America/Los_Angeles)
**Update cadence:** Every hour, via GitHub Actions cron
**Status:** Under construction — May 2026 campaign

## What this does

Every hour, a Python script:

1. Pulls the campaign-to-date total from Toast POS, GoFundMe, Stripe, and Shopify.
2. Sums them into one grand total.
3. Writes `public/totals.json` (consumed by the Wix embed) and an audit snapshot in `snapshots/`.
4. Commits the result back to this repo. GitHub Pages auto-deploys the updated embed page and JSON.

The Wix site loads a small iframe pointing at the GitHub Pages embed page, which renders the live total.

## Architecture

```
   Toast POS    GoFundMe     Stripe      Shopify
       \           |           |           /
        \          |           |          /
         \         v           v         /
          +----------------------------+
          |   aggregate.py (hourly)    | <-- GitHub Actions cron
          +----------------------------+
                       |
                       v
          public/totals.json + snapshots/
                       |
                       v
          GitHub Pages embed page (index.html)
                       |
                       v
                Wix iframe element
```

Design principles:

- Each source returns its full campaign-to-date total on every call, so the aggregator is idempotent. A failed hourly run is harmless — the next one produces the right number.
- No database. Snapshots are just JSON files committed to the repo, giving us complete history and an instant last-known-good fallback if a source fails.
- All state stored in UTC. We convert to America/Los_Angeles only at the API edges (when computing campaign window boundaries) and in the embed display.

## Data sources

### Toast POS
- **Method:** Toast API (existing `toast_*.py` credentials)
- **What we pull:**
  - Sales of fundraising menu items (identified by name containing "Chaniya" or "give $10 get $10")
  - Round-up donations from POS payments
- **Notes:** Toast returns timestamps in UTC. Round-up extraction logic depends on how round-ups are recorded in Toast — to be confirmed during the discovery pass (see `sources/toast.py`).

### GoFundMe
- **Method:** HTML scrape (no API available)
- **Campaign URL:** https://www.gofundme.com/f/standing-with-chaniya-through-her-recovery
- **What we pull:** The "raised" amount shown on the page
- **Risk:** GoFundMe can change their markup mid-campaign. If the parser fails, the aggregator alerts and falls back to last-known-good.

### Stripe (QR code donations)
- **Method:** Stripe API
- **What we pull:** All Payment Intents within the campaign window where `metadata.purpose == "qr_donation"`
- **Setup required:** The QR-code Stripe Payment Link must have `purpose=qr_donation` set in its Metadata field (Stripe propagates Payment Link metadata to all child Checkout Sessions and Payment Intents)

### Shopify (frozen dumpling sales)
- **Method:** Shopify Admin API
- **What we pull:** Sum of all orders created within the campaign window. All May Shopify sales count toward the fundraiser.
- **Note:** Shopify is on a completely separate Stripe account from the QR donations, so no double-counting risk between these two sources.

## Repo layout

```
chaniya-fundraiser/
├── .github/workflows/hourly.yml      # GitHub Actions cron
├── public/
│   ├── index.html                    # Wix iframe target (served by GitHub Pages)
│   └── totals.json                   # live total (Wix consumes this)
├── snapshots/                        # one JSON file per hourly run, append-only history
├── sources/
│   ├── toast.py
│   ├── gofundme.py
│   ├── stripe_donations.py
│   └── shopify_dumplings.py
├── aggregate.py                      # orchestrator: calls sources, sums, writes
├── config.py                         # campaign window, product IDs, env vars
├── toast_config.py                   # existing — Toast credentials
├── toast_auth.py                     # existing — Toast token generation
├── toast_fetch.py                    # existing — sample Toast menu-item fetch
├── requirements.txt
└── README.md
```

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Export secrets (or use a .env file with python-dotenv)
export STRIPE_API_KEY=sk_live_xxx
export SHOPIFY_ADMIN_TOKEN=xxx
export SHOPIFY_STORE_DOMAIN=xxx.myshopify.com
# Toast credentials live in toast_config.py

# Run the aggregator
python aggregate.py
```

This pulls fresh totals from every source, prints the breakdown to stdout, and writes `public/totals.json` + a new file in `snapshots/`. Safe to run any time — every run is idempotent.

## Configuration

Edit `config.py` to change campaign-level settings:

- `CAMPAIGN_START` / `CAMPAIGN_END` — campaign window in PT (defaults: May 1 00:00 → May 31 23:59 PT)
- `TIMEZONE` — `America/Los_Angeles`
- `TOAST_ITEM_GUIDS` — fundraising menu item GUIDs (filled in during the discovery pass)
- `STRIPE_METADATA_KEY` / `STRIPE_METADATA_VALUE` — defaults to `purpose=qr_donation`
- `GOFUNDME_URL` — campaign URL

Secrets are stored in GitHub Actions repository secrets and read as environment variables at runtime. Never commit secrets to the repo.

## How the hourly job works

`.github/workflows/hourly.yml` runs on a cron schedule:

```yaml
on:
  schedule:
    - cron: '0 * * * *'    # top of every hour, UTC
  workflow_dispatch:        # manual trigger button in the Actions tab
```

Each run:

1. Checks out the repo
2. Installs Python dependencies
3. Runs `aggregate.py`
4. Commits any changes to `public/totals.json` and the new `snapshots/` file back to the repo
5. GitHub Pages picks up the updated `public/` folder and serves it

You can trigger a run manually from the Actions tab if you need the displayed total to refresh sooner than the next scheduled hour.

## Wix integration

In the Wix editor:

1. Add an HTML iframe element where the tracker should appear.
2. Set the iframe source to the GitHub Pages URL: `https://<youraccount>.github.io/chaniya-fundraiser/`
3. Size the iframe to match the embed design.

The embed page uses a transparent background and inherits the site's font stack where possible, so it fits naturally on the page.

## Troubleshooting

**A source shows $0 or a stale total.** Open the Actions tab, find the most recent failed run, and check the logs — each source logs its raw response when it fails. If the source is genuinely down, the aggregator falls back to that source's last-known-good total from the most recent successful snapshot, and `totals.json` will include `degraded: true` plus a per-source `last_fresh_at` timestamp.

**The total looks wrong.** Check the most recent file in `snapshots/`. Each snapshot includes a per-source breakdown with raw figures, so you can spot which source contributed the unexpected number.

**The GoFundMe scraper broke.** Most likely cause: GoFundMe changed their HTML. Update the parser in `sources/gofundme.py` and re-run.

**A scheduled run was delayed.** GitHub Actions cron can be delayed under heavy GitHub load. If the total is more than ~90 minutes stale, trigger a manual run from the Actions tab.

## Open decisions

These are pending and tracked in the project task list:

- End-of-campaign behavior: freeze the displayed total at May 31 23:59 PT, or keep the aggregator running through ~June 7 to catch transactions that settle late?
- Shopify amount: count product subtotals only, or include shipping and tax?

## Future campaigns

This system is designed to be reused. To run a new campaign:

1. Update `CAMPAIGN_START` / `CAMPAIGN_END` in `config.py`.
2. Update `TOAST_ITEM_GUIDS` and the Stripe metadata value as needed.
3. Update the embed copy and styling in `public/index.html`.
4. Re-enable the GitHub Actions workflow.
