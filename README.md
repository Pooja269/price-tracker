# Daily price tracker (any product)

Checks the products in `config.json` once a day, keeps a price history, and sends ONE email
covering all of them. The subject line is tagged `[LOW: ...]` when a product hits a new
all-time low (in your tracked history) or goes at/below its `target_price`.

## 1. Choose what to track (config.json)
See `config.example.json` for all of these. Mix and match inside `products`:

**A. By name only** (the "just change the name" way):

    "products": ["Sony WH-1000XM6 headphones"]

The tracker looks the name up on Google Shopping and reports the cheapest offer per store.
This needs a SerpAPI key (`SERPAPI_KEY`, see below). By default every word of the name must
appear in a listing's title, and offers priced under half the median are dropped, which
filters out accessories and parts. Fine-tune per product with a `search` block:
`query`, `must_include`, `exclude`, `min_price`, `max_price`, `stores`, `max_results`,
`outlier_filter`.

**B. By store page URL** (free, no key; most reliable for a specific model/listing):

    {"name": "Instant Pot Duo 6qt",
     "sources": {"amazon": {"url": "https://www.amazon.ca/dp/..."},
                 "costco": {"url": "https://www.costco.ca/....html"}}}

Any store name works. Amazon, Best Buy and Walmart have their own parsers; every other
store uses a generic one (JSON-LD, meta tags, common price markup). Add
`"enabled": false` to pause a source.

**C. Both** in the same product.

Other settings: `report_title` (email subject prefix), `country` (`ca`, `us`, ... for
search), `currency` / `currency_symbol` (labels only), and a per-product `target_price`.
Best Buy US only: put the `sku` in the source and set `BESTBUY_API_KEY` to use their API.

## 2. Settings (environment variables / GitHub secrets)
| Name | Example |
|---|---|
| SMTP_HOST | smtp.gmail.com |
| SMTP_PORT | 465 |
| SMTP_USER | your Gmail address |
| SMTP_PASS | a Gmail **App Password** (Google Account > Security > 2-Step Verification > App passwords) |
| EMAIL_TO | where to send the alert |
| SERPAPI_KEY | only for name-only search (serpapi.com; it has a small free tier, check current limits) |

## 3. Test locally (Mac/Linux)
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python price_tracker.py --dry-run     # prints the email, sends nothing

## 4. Run it daily (pick one)
**GitHub Actions (free, no computer needed):** push this folder to a private GitHub repo,
add the secrets above under Settings > Secrets and variables > Actions. The workflow in
`.github/workflows/daily.yml` runs every day at 8:00 AM Toronto time (13:00 UTC; it shifts
to 9:00 AM during daylight saving) and commits `price_history.json` so history persists.
Use the Actions tab > "Run workflow" to test it right away.

**Cron (your own always-on machine):**
    0 8 * * * cd /path/to/folder && SMTP_HOST=... SMTP_PORT=465 SMTP_USER=... SMTP_PASS=... EMAIL_TO=... .venv/bin/python price_tracker.py

## Known limits
- **Facebook Marketplace is not checked.** It requires login and forbids automated
  scraping; each product in the email has a ready-made Marketplace search link instead.
- Name-only search shows Google Shopping's listed prices, which can lag or include
  third-party sellers. Check the link before buying; use store URLs (option B) for
  the prices you most care about.
- Amazon and Walmart sometimes show bot-check pages to scripts, and some stores load prices
  with JavaScript. The email then says "unavailable" for that store rather than failing.
- If a store keeps reporting "price not found", that store's page needs a parser tweak.
