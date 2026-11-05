#!/usr/bin/env python3
"""Generic daily price tracker.

Reads config.json, checks the price of every product, records prices in
price_history.json, and sends ONE daily email summarizing everything,
flagging new all-time lows (in your tracked history) and target prices.

A product can be tracked three ways (mix freely):
  1. By name only:      "products": ["Sony WH-1000XM6"]
        -> looks the name up on Google Shopping (needs SERPAPI_KEY).
  2. By store URL:      {"name": "...", "sources": {"anystore": {"url": "..."}}}
        -> any store name works; the page is read with a generic price parser.
  3. Both together.

Usage:
    python price_tracker.py             # check prices and send email
    python price_tracker.py --dry-run   # check prices, print email, don't send
"""
import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
HISTORY_FILE = HERE / "price_history.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}
TIMEOUT = 25


# ---------------------------------------------------------------- parsing
def to_price(value):
    """Convert '$1,049.95' / 1049.95 / '1049.95' to float, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"\d[\d,]*\.?\d*", str(value))
    if not m:
        return None
    try:
        p = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return p if p > 0 else None


def _walk_offers(node, found):
    if isinstance(node, dict):
        if "price" in node and ("priceCurrency" in node or "@type" in node):
            p = to_price(node.get("price"))
            if p:
                found.append(p)
        if "lowPrice" in node:
            p = to_price(node.get("lowPrice"))
            if p:
                found.append(p)
        for v in node.values():
            _walk_offers(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_offers(v, found)


def jsonld_price(html):
    """Lowest price found in schema.org JSON-LD blocks."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            _walk_offers(json.loads(tag.string or ""), found)
        except (json.JSONDecodeError, TypeError):
            continue
    return min(found) if found else None


_OLD_PRICE_WORDS = ("old", "was", "regular", "strike", "original", "compare", "msrp")


def visible_price(html):
    """Fallbacks for pages without JSON-LD (works on Magento and similar shops)."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. Magento's final-price attribute (the sale price when discounted).
    for el in soup.select('[data-price-type="finalPrice"][data-price-amount]'):
        p = to_price(el.get("data-price-amount"))
        if p:
            return p

    # 2. Product meta tags.
    for sel in (
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
    ):
        el = soup.select_one(sel)
        if el and to_price(el.get("content")):
            return to_price(el.get("content"))

    # 3. First element whose class/id says "price" and doesn't look like a
    #    crossed-out "was" price.
    for el in soup.find_all(True, class_=re.compile("price", re.I)):
        cls = " ".join(el.get("class", [])).lower() + " " + (el.get("id") or "").lower()
        if any(w in cls for w in _OLD_PRICE_WORDS):
            continue
        if el.find_parent(class_=re.compile("|".join(_OLD_PRICE_WORDS), re.I)):
            continue
        text = el.get_text(" ", strip=True)
        m = re.fullmatch(r"(?:CA|US)?\s*\$\s?(\d[\d,]*\.\d{2})", text)
        if m:
            return to_price(m.group(1))
    return None


def page_price(html):
    return jsonld_price(html) or visible_price(html)


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------- retailers
def fetch_generic(cfg):
    """Works for most stores; used for any source name without its own parser."""
    html = get(cfg["url"])
    p = page_price(html)
    if p:
        return p
    low = html.lower()
    if "captcha" in low or "robot check" in low or "are you a robot" in low:
        raise RuntimeError("site showed a bot check")
    return None


def fetch_amazon(cfg):
    html = get(cfg["url"])
    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        "#corePrice_feature_div span.a-offscreen",
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ):
        el = soup.select_one(sel)
        if el and to_price(el.get_text()):
            return to_price(el.get_text())
    if "captcha" in html.lower():
        raise RuntimeError("Amazon showed a CAPTCHA (bot check)")
    return jsonld_price(html)


def fetch_bestbuy(cfg):
    # Official Best Buy (US) API if an API key and SKU are provided.
    key = os.environ.get("BESTBUY_API_KEY")
    if key and cfg.get("sku"):
        r = requests.get(
            f"https://api.bestbuy.com/v1/products/{cfg['sku']}.json",
            params={"apiKey": key, "show": "salePrice,regularPrice,name"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return to_price(r.json().get("salePrice"))
    if cfg.get("url"):
        return page_price(get(cfg["url"]))
    raise RuntimeError("Best Buy needs either sku + BESTBUY_API_KEY or a url")


def fetch_walmart(cfg):
    html = get(cfg["url"])
    p = page_price(html)
    if p:
        return p
    m = re.search(r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*([\d.]+)', html)
    if m:
        return to_price(m.group(1))
    if "robot" in html.lower() or "captcha" in html.lower():
        raise RuntimeError("Walmart showed a bot check")
    return None


FETCHERS = {
    "amazon": fetch_amazon,
    "bestbuy": fetch_bestbuy,
    "walmart": fetch_walmart,
    "generic": fetch_generic,
}


def search_offers(name, search):
    """Look a product up by name on Google Shopping via SerpAPI.

    Returns a list of result rows (one per store, cheapest offer each).
    Optional filters in the product's "search" block:
      query, country, must_include[], exclude[], min_price, max_price,
      stores[] (only keep stores whose name contains one of these), max_results
    """
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise RuntimeError("SERPAPI_KEY not set (needed for name-only search)")
    r = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_shopping",
            "q": search.get("query") or name,
            "gl": search.get("country", "ca"),
            "hl": "en",
            "api_key": key,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    # By default every word of the product name must appear in the listing title,
    # which screens out accessories and unrelated products. Set "must_include": []
    # to turn that off, or list your own words.
    if "must_include" in search:
        must = [w.lower() for w in search["must_include"]]
    else:
        stop = {"the", "and", "with", "for", "a", "an", "of", "in"}
        must = [w for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", name.lower())
                if w not in stop]
    bad = [w.lower() for w in search.get("exclude", [])]
    stores = [s.lower() for s in search.get("stores", [])]
    lo, hi = search.get("min_price"), search.get("max_price")

    best = {}
    for item in r.json().get("shopping_results", []):
        title = item.get("title", "")
        t = title.lower()
        price = item.get("extracted_price") or to_price(item.get("price"))
        store = item.get("source") or "unknown"
        if not price:
            continue
        if must and not all(w in t for w in must):
            continue
        if any(w in t for w in bad):
            continue
        if lo is not None and price < lo:
            continue
        if hi is not None and price > hi:
            continue
        if stores and not any(s in store.lower() for s in stores):
            continue
        row = {
            "store": store,
            "price": price,
            "note": title[:70],
            "url": item.get("product_link") or item.get("link") or "",
        }
        if store not in best or price < best[store]["price"]:
            best[store] = row
    rows = sorted(best.values(), key=lambda x: x["price"])
    # Drop suspiciously cheap offers (accessories, parts, scams): anything under
    # half the median price when there are at least 3 offers.
    if len(rows) >= 3 and search.get("outlier_filter", True):
        prices = sorted(r["price"] for r in rows)
        median = prices[len(prices) // 2]
        rows = [r for r in rows if r["price"] >= 0.5 * median]
    return rows[: int(search.get("max_results", 5))]


def check_product(product):
    results = []
    for name, cfg in product.get("sources", {}).items():
        if not cfg.get("enabled", True):
            continue
        if not (cfg.get("url") or cfg.get("sku")):
            continue  # blank placeholder
        fetcher = FETCHERS.get(cfg.get("type", name), fetch_generic)
        try:
            price = fetcher(cfg)
            note = "" if price else "price not found on page"
        except Exception as e:  # keep going if one store fails
            price, note = None, f"{type(e).__name__}: {e}"[:140]
        results.append(
            {"store": name, "price": price, "note": note, "url": cfg.get("url", "")}
        )

    search = product.get("search")
    if search is not None and search.get("enabled", True):
        try:
            rows = search_offers(product["name"], search)
            results += rows or [{"store": "Google Shopping", "price": None,
                                 "note": "no matching offers", "url": ""}]
        except Exception as e:
            results.append({"store": "Google Shopping", "price": None,
                            "note": f"{type(e).__name__}: {e}"[:140], "url": ""})
    return results


def normalize_products(config):
    """Allow plain strings and fill in defaults from the top-level config."""
    out = []
    for p in config["products"]:
        if isinstance(p, str):
            p = {"name": p, "search": {}}
        p = dict(p)
        p.setdefault("currency", config.get("currency", ""))
        p.setdefault("currency_symbol", config.get("currency_symbol", "$"))
        p.setdefault("target_price", None)
        if "search" in p and "country" not in p["search"] and config.get("country"):
            p["search"] = {**p["search"], "country": config["country"]}
        out.append(p)
    return out


def check_all(config):
    return [{"product": p, "results": check_product(p)} for p in normalize_products(config)]


# ----------------------------------------------------------------- history
def hkey(product_name, store):
    return f"{product_name}|{store}"


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def update_history(history, checked):
    today = dt.date.today().isoformat()
    prices = {}
    for item in checked:
        for r in item["results"]:
            if r["price"]:
                prices[hkey(item["product"]["name"], r["store"])] = r["price"]
    history = [h for h in history if h["date"] != today]
    if prices:
        history.append({"date": today, "prices": prices})
    HISTORY_FILE.write_text(json.dumps(history, indent=2))
    return history


def prior_lows(history_before_today):
    """{product_name: (price, store, date)} lowest ever recorded per product."""
    best = {}
    for h in history_before_today:
        for key, p in h["prices"].items():
            product, _, store = key.rpartition("|")
            if product not in best or p < best[product][0]:
                best[product] = (p, store, h["date"])
    return best


# ------------------------------------------------------------------- email
def marketplace_url(name):
    return "https://www.facebook.com/marketplace/search/?query=" + urllib.parse.quote(name)


def build_email(config, checked, lows):
    esc = htmllib.escape
    title = config.get("report_title", "Price report")
    summary_bits, flagged = [], []
    text = []
    sections = ""

    for item in checked:
        product, results = item["product"], item["results"]
        name = product["name"]
        cur = product.get("currency_symbol", "$")
        code = product.get("currency", "")
        target = product.get("target_price")
        ok = sorted((r for r in results if r["price"]), key=lambda r: r["price"])

        text.append(f"{name}" + (f" ({code})" if code else ""))
        rows = ""
        for r in ok:
            extra = f"  [{r['note']}]" if r.get("note") else ""
            text.append(f"  {r['store'].title():<14} {cur}{r['price']:>9,.2f}   {r['url']}{extra}")
            link = f"<a href='{esc(r['url'])}'>link</a>" if r["url"] else ""
            small = f"<br><small>{esc(r['note'])}</small>" if r.get("note") else ""
            rows += (
                f"<tr><td>{esc(r['store'].title())}{small}</td>"
                f"<td align='right'><b>{cur}{r['price']:,.2f}</b></td>"
                f"<td>{link}</td></tr>"
            )
        for r in results:
            if not r["price"]:
                text.append(f"  {r['store'].title():<14} unavailable ({r['note']})")
                rows += (
                    f"<tr><td>{esc(r['store'].title())}</td>"
                    f"<td colspan='2'><i>unavailable ({esc(r['note'])})</i></td></tr>"
                )

        notes = []
        prior = lows.get(name)
        if ok:
            best = ok[0]
            summary_bits.append(f"{name} {cur}{best['price']:,.2f}")
            if prior is None:
                notes.append("first price recorded")
            elif best["price"] < prior[0]:
                notes.append("NEW LOWEST PRICE SEEN")
                flagged.append(name)
            if target and best["price"] <= target:
                notes.append(f"at/below your target ({cur}{target:,.2f})")
                if name not in flagged:
                    flagged.append(name)
        else:
            summary_bits.append(f"{name} n/a")
        if prior:
            notes.append(f"previous low {cur}{prior[0]:,.2f} at "
                         f"{prior[1].title()} on {prior[2]}")
        if notes:
            text.append("  " + "; ".join(notes))
        mkt = marketplace_url(name)
        text += [f"  Facebook Marketplace (manual): {mkt}", ""]

        sections += (
            f"<h4 style='margin-bottom:4px'>{esc(name)}"
            + (f" <small>({esc(code)})</small>" if code else "") + "</h4>"
            f"<table cellpadding='6' border='1' style='border-collapse:collapse'>{rows}</table>"
            + (f"<p style='margin:4px 0'><small>{esc('; '.join(notes))}</small></p>"
               if notes else "")
            + f"<p style='margin:2px 0'><small><a href='{esc(mkt)}'>"
              "Search Facebook Marketplace</a> (not checked automatically)</small></p>"
        )

    subject = f"{title}: " + " | ".join(summary_bits)
    if flagged:
        subject = f"[LOW: {', '.join(flagged)}] " + subject
    return subject, subject + "\n\n" + "\n".join(text), f"<h3>{esc(subject)}</h3>{sections}"


def send_email(subject, text, html):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    to = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print email, don't send")
    args = ap.parse_args()

    config = json.loads(CONFIG_FILE.read_text())
    checked = check_all(config)

    history = load_history()
    today = dt.date.today().isoformat()
    lows = prior_lows([h for h in history if h["date"] != today])
    update_history(history, checked)

    subject, text, html = build_email(config, checked, lows)
    if args.dry_run:
        print(text)
        return 0
    send_email(subject, text, html)
    print("Email sent:", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
