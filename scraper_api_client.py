"""
scraper_api_client.py
---------------------
The fourth way to fetch a page in this repo: 2Captcha's Scraper API, which
renders and returns HTML over plain HTTPS with no local browser at all. Same
parser, same row schema, same exit codes as the three browser engines — only
the transport differs.

When this is the right tool
---------------------------
* No browser to install, and nothing to keep alive between pages.
* A page this repo met live from a Scraping Browser profile on 2026-09-18
  came back as an Akamai behavioural interstitial (`sec-cpt`), and the
  browser engines wait it out. This client cannot wait out a JS challenge —
  it gets one response — so it is the better tool where the plain HTML path
  works and the worse one where a challenge appears.

What was measured
-----------------
Fifteen plain fetches of givenchybeauty.com category pages, product pages
and sitemaps from a Moscow residential address on 2026-09-17/18 all returned
HTTP 200, with 0 occurrences of every vendor marker in
`product_parser.BOT_CHALLENGE_MARKERS` across the 14 served HTML captures.
That is a statement about those fetches from that address; a datacentre
exit, a different country or a higher rate is a different experiment, which
is what this client and its `--proxy-country` exist for.

Pagination
----------
There is none to do: this site's listing paging is `?start=N&sz=25`, both
parameters are robots-disallowed, and `product_parser.page_url` is therefore
a no-op. `--pages N > 1` is reported and ignored, in every engine including
this one. To cover a whole locale, read its `sitemap_0-product.xml` (which
robots allows) and run `--mode product` over the result — see the README.

Usage
-----
    python3 scraper_api_client.py --key "$TWOCAPTCHA_KEY" \\
        --category makeup/lips --site-locale us

    python3 scraper_api_client.py --key "$TWOCAPTCHA_KEY" --mode product \\
        --url https://www.givenchybeauty.com/us/p/fantasque-P000170.html

Prefer $TWOCAPTCHA_KEY in .env over --key: a key on the command line is
visible to anyone who can run `ps`, and it lands in shell history.

Requires: pip install -r requirements.txt   (just `requests` and bs4 — this
          client needs no engine library at all)
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional
from urllib.parse import urljoin

import re

import requests
from bs4 import BeautifulSoup

from product_parser import (parse_category, parse_product,
                            detect_bot_challenge, detect_page_state, page_url,
                            site_host, is_supported_host, unsupported_reason,
                            HOSTS, BASE, LOCALES, is_showcase_locale,
                            locale_of, is_robots_allowed)
from output_writer import finish_run, dedupe_by_key, EXIT_REMOTE_API_ERROR
import page_flow
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# No mode here has a second page: a product page is one page, and the
# listing's own paging is robots-disallowed (see product_parser.page_url).
PAGINATED_MODES = ()


# Global on purpose, not a single find/partition -- see CLAUDE.md §8: "A
# masker that handles the first occurrence prints the password the other
# four times and looks like it is working." Same pattern the three browser
# engines use in their own _mask_credentials.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _build_wait_for(args) -> Optional[str]:
    """`waitFor` must be a JSON STRING (double-encoded), per the API docs.
    Passing a nested object is silently wrong.

    Default (no flag): wait for the DOM. Not needed for a page that arrives
    as complete HTML with no hydration (this site, per the module
    docstring) -- --wait-text/--wait-element exist for the day that stops
    being true, or for a --cdp-url run through a challenge page."""
    if args.wait_text:
        return json.dumps({"text": args.wait_text})
    if args.wait_element:
        return json.dumps({"element": args.wait_element, "checkVisible": True})
    if args.wait_state:
        return json.dumps({"state": args.wait_state})
    return None


def fetch_html(args, url: str, timeout: int):
    payload = {
        "task_type": "scrape",
        "url": url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "headers", "body"}
        "timeout": min(timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", wait_for)

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header -- worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", debug)

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces); 402 = out of balance; 408 = sync wait exceeded.
        # This is the Scraper API's OWN call failing, not the target site --
        # see output_writer.RemoteAPIError / EXIT_REMOTE_API_ERROR, which
        # this file maps to below rather than treating it as a crash or as
        # Givenchy blocking a page.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = body.get("status")
    upstream_headers = body.get("headers") or {}
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS and the HEADERS both travel WITH the HTML, neither thrown
    # away. The Scraper API hands back `{"status", "headers", "body"}` and
    # until v0.4.1 this function kept the first two thirds of that and
    # dropped the headers on the floor -- which meant the single most
    # decisive signal this site emits, `x-amzn-waf-action: captcha`, arrived
    # in the response and was discarded one line before the classifier that
    # needed it. See product_parser.detect_page_state, which consults that
    # header before anything else.
    waf_action = upstream_headers.get("x-amzn-waf-action") or \
        upstream_headers.get("X-Amzn-Waf-Action")
    if waf_action:
        logger.warning("AWS WAF answered this request with action=%r "
                       "(server=%r). The page body is the challenge, not the "
                       "listing.", waf_action,
                       upstream_headers.get("server")
                       or upstream_headers.get("Server"))
    return html, upstream_status, upstream_headers


def _parse_for_mode(html: str, url: str, args, page_num: int) -> List:
    if args.mode == "category":
        return parse_category(html, url, page_num=page_num)
    if args.mode == "product":
        row = parse_product(html, url)
        return [row] if row is not None else []
    raise ValueError(f"unknown mode {args.mode!r}")


def _fetch_one_page(args, page_num: int, url: str):
    """(rows, blocked_by, upstream_status, html) for one page. Never raises
    for an EXPECTED failure (a bad status, a challenge page); those come
    back as blocked_by set. A Scraper API call failure IS allowed to raise
    -- caught once in main() and mapped to EXIT_REMOTE_API_ERROR. `html` is
    returned (even on a blocked/vendor outcome, where it's the challenge
    page) so the caller can read page 1's own next-link for
    page_flow.pagination_is_addressable, the same way the browser engines
    read it off the live DOM."""
    attempts = max(1, args.retries + 1)
    html, upstream_status, upstream_headers = "", None, {}
    for attempt in range(1, attempts + 1):
        html, upstream_status, upstream_headers = fetch_html(args, url, args.timeout)

        if args.dump_html:
            dump_path = (args.dump_html if page_num == 1
                        else f"{args.dump_html}.page{page_num}")
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                        dump_path, len(html))

        state = detect_page_state(html, status=upstream_status, url=url,
                                  headers=upstream_headers)
        if state == "blocked":
            # Retried like a vendor challenge, NOT returned immediately: a
            # transient upstream error (a non-2xx status, or an empty body)
            # deserves the same --retries budget every other engine in this
            # repo gives it -- playwright_scraper.py's own _fetch_one_page
            # retries a "blocked" classification (see page_flow.should_retry),
            # and this client used to be the one place in the family where
            # --retries silently did nothing for that outcome: it returned
            # on the FIRST attempt regardless of how many were requested.
            if attempt < attempts:
                logger.info("state=blocked (upstream HTTP %s) on attempt "
                            "%d/%d -- retrying in %ds.", upstream_status,
                            attempt, attempts, args.retry_delay)
                time.sleep(args.retry_delay)
                continue
            debug_html = f"{args.out}_page{page_num}_debug.html"
            with open(debug_html, "w", encoding="utf-8") as f:
                f.write(html)
            logger.error("Refused or unreachable (state=blocked, upstream "
                        "HTTP %s, %d bytes) -- saved to %s. This is exit 3, "
                        "distinct from a genuinely empty result (exit 4).",
                        upstream_status, len(html), debug_html)
            return [], "blocked", upstream_status, html

        vendor = detect_bot_challenge(html, url=url) if state != "content" else None
        if vendor:
            if attempt < attempts:
                logger.info("%s challenge page on attempt %d/%d -- retrying "
                            "in %ds.", vendor, attempt, attempts, args.retry_delay)
                time.sleep(args.retry_delay)
                continue
            debug_html = f"{args.out}_page{page_num}_debug.html"
            with open(debug_html, "w", encoding="utf-8") as f:
                f.write(html)
            logger.error("Blocked by %s before parsing (%d bytes) -- saved "
                        "to %s.", vendor, len(html), debug_html)
            return [], vendor, upstream_status, html
        break

    rows = _parse_for_mode(html, url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(rows), page_num)

    if rows and args.mode == "category" and not is_showcase_locale(url):
        priced = sum(1 for r in rows if r.price is not None)
        coverage = priced / len(rows)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(rows), 100.0 * coverage)
        # See page_flow.PRICE_COVERAGE_FLOOR for why transfers is excluded.
        if coverage < page_flow.PRICE_COVERAGE_FLOOR:
            logger.warning("Price coverage on page %d (%.0f%%) is below the "
                           "%.0f%% floor for --mode %s — check for a "
                           "parsing regression rather than assuming these "
                           "players are all free agents.",
                           page_num, 100.0 * coverage,
                           100.0 * page_flow.PRICE_COVERAGE_FLOOR, args.mode)

    if not rows:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning("0 rows parsed -- saved what the API returned to %s.", debug_html)

    return rows, None, upstream_status, html


def scrape(args) -> int:
    all_rows: List = []
    seen_keys = set()
    blocked = False
    stop_reason = "single_page_mode" if args.mode not in PAGINATED_MODES else "completed"
    pages_completed = 0
    pages_failed: List[int] = []
    final_url = args.url

    rows, blocked_by, _status, page1_html = _fetch_one_page(args, 1, args.url)
    if blocked_by is not None:
        stop_reason = f"blocked_{blocked_by}"
        blocked = True
        pages_failed.append(1)
    else:
        pages_completed = 1
        fresh = dedupe_by_key(rows, seen_keys, key="sku")
        all_rows.extend(fresh)

        addressable = True
        if args.pages > 1 and args.mode in PAGINATED_MODES:
            # Same "verify first" check the browser engines make off the
            # live DOM (page_flow.pagination_is_addressable) -- made here
            # off the fetched HTML instead. See page_flow.py's "gallery
            # This site has no addressable pagination at all (see the
            # module docstring), so this always answers False.
            # transfers), and only rarely is not -- this checks which is
            # true FOR THIS RUN rather than assuming either way.
            soup = BeautifulSoup(page1_html, "html.parser")
            next_el = soup.select_one("link[rel='next'], a[rel='next']")
            site_next = next_el.get("href") if next_el else None
            if site_next:
                site_next = requests.compat.urljoin(args.url, site_next)
            addressable = page_flow.pagination_is_addressable(args.mode, args.url, site_next)

        if args.pages > 1 and args.mode in PAGINATED_MODES and not addressable:
            logger.warning(
                "--pages %d requested for --mode %s, but page 1's own "
                "next-link does not match the plain ?page=N this client "
                "would construct (page_flow.pagination_is_addressable) -- "
                "this client has no browser session to chain the site's "
                "own link through instead, so only page 1 was fetched. "
                "This is exit 6 (partial), not 0: the site has more data "
                "than this engine reached this run. Use "
                "playwright_scraper.py, which chains link-to-link for "
                "exactly this case.", args.pages, args.mode)
            # Deliberately NOT in output_writer.COMPLETE_STOP_REASONS -- see
            # the warning above. A pipeline reading `status` should see
            # "partial" here, the same as a page that failed mid-run, not
            # "complete" for a request this engine only partly satisfied.
            stop_reason = "stateless_pagination_unavailable"
        elif args.pages > 1 and args.mode not in PAGINATED_MODES:
            # Defence in depth: `parse_args` already clamps --pages to 1 for
            # a mode with no second page, but a caller that builds its own
            # args object (a scheduler, a test) must not be able to walk
            # into a loop that re-fetches page 1 under a different page
            # NUMBER -- which is what happened before this branch existed,
            # and which a sidecar would then report as two pages read.
            logger.info("--pages %d ignored in --mode %s: this site's "
                        "listing paging is robots-disallowed, so page 1 is "
                        "the whole listing. Read the locale's "
                        "sitemap_0-product.xml and run --mode product over "
                        "it to cover the catalogue.", args.pages, args.mode)
        elif args.pages > 1:
            for page_num in range(2, args.pages + 1):
                url = page_url(args.url, page_num)
                page_rows, page_blocked_by, _status, _html = _fetch_one_page(args, page_num, url)
                if page_blocked_by is not None:
                    stop_reason = f"blocked_{page_blocked_by}"
                    blocked = True
                    pages_failed.append(page_num)
                    break
                pages_completed += 1
                final_url = url
                fresh_count = sum(1 for r in page_rows
                                  if r.sku is None or r.sku not in seen_keys)
                fresh = dedupe_by_key(page_rows, seen_keys, key="sku")
                if len(fresh) < len(page_rows):
                    # The same dropped-duplicate log line every browser
                    # engine in this family prints on its own merge step
                    # (playwright_scraper.py, puppeteer_scraper.py,
                    # selenium_scraper.py) -- this engine was missing it,
                    # which made a run that silently re-fetched the same
                    # page look identical to one that made real progress.
                    logger.info("Page %d: dropped %d duplicate row(s).",
                               page_num, len(page_rows) - len(fresh))
                all_rows.extend(fresh)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen -- "
                                "treating that as the end.", page_num)
                    stop_reason = "no_new_products"
                    break
                if page_flow.is_thin_page(len(page_rows), len(rows)):
                    logger.warning(
                        "Page %d returned only %d row(s), well under page "
                        "1's %d -- possibly a page beyond the listing's "
                        "real depth, or a markup regression. Continuing; "
                        "this is informational only and does not change "
                        "the exit code.", page_num, len(page_rows), len(rows))
                if page_num < args.pages:
                    time.sleep(args.delay)

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=pages_completed,
                      pages_failed=pages_failed, mode=args.mode,
                      source=site_host(final_url) or "givenchybeauty.com",
                      start_url=args.url, final_url=final_url)


def main() -> int:
    args = parse_args()
    try:
        return scrape(args)
    except requests.RequestException as e:
        logger.error("Network error talking to the Scraper API: %s", e)
        return EXIT_REMOTE_API_ERROR
    except RuntimeError as e:
        # HTTP 4xx/5xx from the Scraper API itself, including the 422 a
        # busy or unreachable --cdp-url produces -- the API's own call
        # failing, not Givenchy blocking a page. See
        # output_writer.RemoteAPIError (this file raises plain RuntimeError
        # from fetch_html rather than that class, since this client has no
        # dependency on a browser engine to share it with -- but the exit
        # code is the same family contract value either way).
        logger.error("%s", e)
        return EXIT_REMOTE_API_ERROR


def parse_args():
    p = argparse.ArgumentParser(
        description="Givenchy Beauty scraper -- 2captcha Scraper API edition "
                    "(no local browser). See this file's docstring: this "
                    "site was measured to need neither a proxy nor "
                    "--cdp-url for the plain HTTP path to work.")
    p.add_argument("--mode", choices=["category", "product"], default="category",
                   help="Same meaning as the browser engines -- see "
                        "playwright_scraper.py's --mode help.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key passed on the
    # command line is visible to anyone who can run `ps`, and it lands in
    # shell history and in any log that echoes the command line.
    p.add_argument("--key", default=None,
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Falls back to $TWOCAPTCHA_KEY via .env/env_config, "
                        "which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="Override the URL this mode would otherwise build. "
                        "Falls back to $GIVENCHY_URL, then to the "
                        "mode's own canonical URL.")
    p.add_argument("--category", default="makeup/lips",
                   help="Category path without the locale prefix, e.g. "
                        "makeup/lips.")
    p.add_argument("--site-locale", default="us", choices=list(LOCALES),
                   help="Locale path prefix on www.givenchybeauty.com.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to crawl. Checked per run against "
                        "page 1's own next-link (page_flow.pagination_is_"
                        "addressable). A listing is one page on this site: "
                        "its own paging is robots-disallowed.")
    p.add_argument("--delay", type=float, default=1.0, help="Delay between pages, seconds")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="givenchy_products_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port "
                        "-- NOT needed for this site as measured; see the module docstring.")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page. Only useful with "
                           "--cdp-url -- a plain fetch already returns complete HTML here.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible, e.g. 'table.items'")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a bot-challenge page comes back. Each attempt "
                        "is a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10, help="Seconds between retries")
    p.add_argument("--dump-html", default=None,
                   help="Save the exact HTML the parser is given (always, even on success)")
    args = p.parse_args()
    # This client uses --key rather than --twocaptcha-key, so the env
    # mapping is spelled out instead of defaulted -- same pattern this
    # family's mediamarkt-scraper uses for its own scraper_api_client.py.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "GIVENCHY_CDP_ENDPOINT": "cdp_url",
        "GIVENCHY_URL": "url",
    })

    if not args.key:
        p.error("no --key given, and TWOCAPTCHA_KEY is not set in the environment or in .env.")

    if not args.url:
        if args.mode == "product":
            p.error("--mode product needs --url (or $GIVENCHY_URL).")
        args.url = f"{BASE}/{args.site_locale}/{args.category.strip('/')}/"

    if not is_robots_allowed(args.url):
        p.error(f"{args.url} matches a Disallow rule in this site's "
                f"robots.txt and will not be fetched.")

    if not is_supported_host(args.url):
        why = unsupported_reason(args.url)
        p.error(f"{site_host(args.url) or args.url!r} {why}. Supported: "
                f"{', '.join(sorted(HOSTS))}.")

    if args.mode not in PAGINATED_MODES and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s.", args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
