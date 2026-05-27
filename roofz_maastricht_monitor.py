#!/usr/bin/env python3
"""Monitor Roofz Maastricht listings and notify Telegram on new properties.

Design goals:
- Free or near-free deployment
- No ChatGPT / MCP / Actions required
- Works on a schedule (e.g. GitHub Actions cron)
- Uses Telegram bot messages for alerts

Environment variables:
- ROOFZ_URL: listing page URL to monitor
- TELEGRAM_BOT_TOKEN: Telegram bot token from @BotFather
- TELEGRAM_CHAT_ID: numeric chat id or @channelusername
- STATE_FILE: path to a local JSON file used to remember seen listings
- USE_PLAYWRIGHT: set to 1 to force Playwright rendering if installed
- ALERT_ON_FIRST_RUN: set to 1 to alert on the first run instead of seeding state silently
- REQUEST_TIMEOUT: HTTP timeout in seconds (default 30)

Dependencies:
- requests
- beautifulsoup4
- (optional) playwright
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_ROOFZ_URL = "https://roofz.eu/huur/woningen?filter=location:Maastricht"
DEFAULT_STATE_FILE = "state.json"


@dataclass
class Listing:
    url: str
    title: str = ""
    price: str = ""
    area: str = ""
    rooms: str = ""
    availability: str = ""

    def key(self) -> str:
        return canonicalize_url(self.url)


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {message}", flush=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def canonicalize_url(raw: str) -> str:
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    query = parsed.query
    if query:
        return f"{parsed.scheme}://{parsed.netloc}{path}?{query}"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen_urls": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_urls": []}


def save_state(path: Path, seen_urls: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen_urls": sorted(set(seen_urls)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_html(url: str) -> str:
    """Fetch page HTML.

    Tries Playwright first when enabled or available, then falls back to requests.
    """
    use_playwright = env("USE_PLAYWRIGHT", "0") == "1"

    if use_playwright:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 2000})
                page.goto(url, wait_until="networkidle", timeout=int(env("REQUEST_TIMEOUT", "30")) * 1000)
                html = page.content()
                browser.close()
                return html
        except Exception as exc:
            log(f"Playwright fetch failed, falling back to requests: {exc}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    timeout = int(env("REQUEST_TIMEOUT", "30"))
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_listing_urls(listing_page_html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(listing_page_html, "html.parser")
    urls: list[str] = []
    seen = set()

    # Prefer project pages, which Roofz uses for property detail pages.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue
        absolute = canonicalize_url(urljoin(base_url, href))
        parsed = urlparse(absolute)
        if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
            continue
        if "/projecten/" not in parsed.path:
            continue
        # Exclude nav links and the current listing page itself.
        if canonicalize_url(base_url) == absolute:
            continue
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)

    return urls


def text_of(soup: BeautifulSoup) -> str:
    return " ".join(soup.get_text(" ", strip=True).split())


def first_match(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def extract_detail_fields(detail_html: str, url: str) -> Listing:
    soup = BeautifulSoup(detail_html, "html.parser")
    title = ""

    # Title from og:title / h1 / title tag.
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    body_text = text_of(soup)

    price = first_match([
        r"€\s?\d[\d\.,]*",
        r"EUR\s?\d[\d\.,]*",
    ], body_text)

    area = first_match([
        r"\b\d{1,4}(?:[\.,]\d+)?\s?m2\b",
        r"\b\d{1,4}(?:[\.,]\d+)?\s?sqm\b",
        r"\b\d{1,4}(?:[\.,]\d+)?\s?m\xb2\b",
    ], body_text)

    rooms = first_match([
        r"\b\d+\s?rooms?\b",
        r"\b\d+\s?room\b",
        r"\b\d+\s?bedrooms?\b",
        r"\b\d+\s?bedroom\b",
    ], body_text)

    availability = ""
    for marker in ["available", "availability", "available from", "now available", "for rent"]:
        if marker.lower() in body_text.lower():
            availability = marker
            break

    return Listing(
        url=canonicalize_url(url),
        title=title,
        price=price,
        area=area,
        rooms=rooms,
        availability=availability,
    )


def send_telegram(message: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }
    response = requests.post(endpoint, json=payload, timeout=30)
    response.raise_for_status()


def format_alert(listing: Listing) -> str:
    parts = ["New Roofz property detected in Maastricht"]
    if listing.title:
        parts.append(f"Title: {listing.title}")
    if listing.price:
        parts.append(f"Price: {listing.price}")
    if listing.area:
        parts.append(f"Area: {listing.area}")
    if listing.rooms:
        parts.append(f"Rooms: {listing.rooms}")
    if listing.availability:
        parts.append(f"Status: {listing.availability}")
    parts.append(f"Link: {listing.url}")
    parts.append(f"Seen at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}")
    return "\n".join(parts)


def run() -> int:
    roofz_url = env("ROOFZ_URL", DEFAULT_ROOFZ_URL)
    state_path = Path(env("STATE_FILE", DEFAULT_STATE_FILE))
    alert_on_first_run = env("ALERT_ON_FIRST_RUN", "0") == "1"

    log(f"Fetching listing page: {roofz_url}")
    try:
        listing_html = fetch_html(roofz_url)
    except Exception as exc:
        log(f"Failed to fetch listing page: {exc}")
        return 2

    if "No properties found" in listing_html:
        log("No properties found marker detected on page.")

    urls = extract_listing_urls(listing_html, roofz_url)
    log(f"Discovered {len(urls)} candidate listing URLs")

    state = load_state(state_path)
    seen_urls = set(state.get("seen_urls", []))

    if not seen_urls and not alert_on_first_run:
        log("First run: seeding state and exiting without alert.")
        save_state(state_path, urls)
        return 0

    new_urls = [u for u in urls if u not in seen_urls]
    if not new_urls:
        log("No new listings detected.")
        save_state(state_path, seen_urls.union(urls))
        return 0

    log(f"Found {len(new_urls)} new listing(s)")
    alerts: list[str] = []

    for url in new_urls:
        try:
            detail_html = fetch_html(url)
            listing = extract_detail_fields(detail_html, url)
            alerts.append(format_alert(listing))
            seen_urls.add(url)
        except Exception as exc:
            log(f"Failed to inspect listing {url}: {exc}")

    if alerts:
        message = "\n\n".join(alerts)
        send_telegram(message)
        log("Telegram notification sent.")

    save_state(state_path, seen_urls.union(urls))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
