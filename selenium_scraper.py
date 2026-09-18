"""
givenchy-scraper — Selenium edition
===================================

Scrapes givenchybeauty.com: a category listing into one row per product
tile, or a product page into one row. The SITE facts — eleven locales as
path prefixes, JSON-LD on product pages only, the tile's own `data-*`
attributes as the listing's primary source, and why a listing run is a
single fetch — are in playwright_scraper.py's docstring and in
product_parser.py; they are not repeated here, because three copies of a
measurement is how two of them go stale.

    --mode category    a listing page. --category with --site-locale, or --url.
    --mode product     one product page. --url.

All three engines must agree on exit codes, run status, and whether a run
crashes or spends money. The shared decisions live in
output_writer.finish_run() and page_flow.py.

Engine limits worth knowing before choosing this one
----------------------------------------------------
* **Selenium cannot use an authenticated remote CDP endpoint.** chromedriver's
  `debuggerAddress` takes a bare `host:port` with nowhere to put a password,
  so the Scraping Browser API's `ws://user:pass@host:port` has no home here.
  This is not a generic "connect to CDP" option.
* **`--proxy-server` cannot authenticate at all.** Credentials are stripped
  and a warning is printed, rather than letting a `user:pass` URL look like
  it is doing something.
* It runs with `page_load_strategy = "eager"`: the default blocks until a
  `load` event many sites never fire.

Usage
-----
    python selenium_scraper.py --category makeup/lips --site-locale us

    python selenium_scraper.py --mode product \\
        --url https://www.givenchybeauty.com/us/p/fantasque-P000170.html

Requires: pip install -r requirements.txt -r requirements-selenium.txt
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import List, Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options

from captcha_solver import (detect_aws_waf, detect_recaptcha_v3,
                            detect_recaptcha_in_page, reconcile_detections,
                            solve_recaptcha, AWS_WAF_COOKIE, INJECT_TOKEN_JS)
from product_parser import (parse_category, parse_product,
                            detect_bot_challenge, page_url,
                            site_host, is_supported_host, unsupported_reason,
                            HOSTS, BASE, LOCALES, is_showcase_locale,
                            locale_of, is_robots_allowed,
                            is_self_clearing_challenge)
from output_writer import dedupe_by_key, finish_run, RemoteAPIError, EXIT_REMOTE_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30
# Modes with more than one page. NEITHER mode here has one: a product page is
# a single page by definition, and a listing's own paging is robots-disallowed
# (see playwright_scraper.py's docstring and `product_parser.page_url`). Kept
# as an empty tuple, identical in all three engines, rather than deleted.
PAGINATED_MODES = ()

_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)

_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium "
            "cannot send them: chromedriver's debuggerAddress is a bare "
            "host:port. Use playwright_scraper.py or puppeteer_scraper.py "
            "for a credentialed endpoint such as the Scraping Browser API.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


@dataclass
class PageOutcome:
    page_num: int
    url: str
    final_url: Optional[str] = None
    rows: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


class _Session:
    """One Chrome driver, relaunchable onto a different exit."""

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        # Selenium's equivalent of the `domcontentloaded` that
        # playwright_scraper.py and puppeteer_scraper.py both navigate with.
        # The default strategy is "normal", which blocks until the `load`
        # event, and on this site that event does not arrive: measured
        # A sibling repo measured its own site timing out at 60s on every attempt
        # ("timeout: Timed out receiving message from renderer") while
        # https://example.com returned in 0.2s through the same driver, and
        # while both other engines fetched the same page in under two
        # seconds. A listing page's rows are in the first response here (see
        # page_flow.py), so waiting for the last tracker pixel buys nothing
        # and cost this engine every live run it ever attempted.
        options.page_load_strategy = "eager"
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM. They have been stripped, so requests will go out "
                    "unauthenticated. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script, fingerprint_user_agent
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s).", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements("css selector", selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _parse_for_mode(html: str, url: str, args, page_num: int) -> List:
    if args.mode == "category":
        return parse_category(html, url, page_num=page_num)
    if args.mode == "product":
        row = parse_product(html, url)
        return [row] if row is not None else []
    raise ValueError(f"unknown mode {args.mode!r}")


def _next_page_href(session) -> Optional[str]:
    try:
        return session.driver.execute_script(
            "const a = document.querySelector(\"link[rel='next'], a[rel='next']\");"
            "return a ? (a.href || a.getAttribute('href')) : null;")
    except WebDriverException:
        return None


def handle_captcha_if_present(session, args, ready_selector: str,
                              proxy=None) -> bool:
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    already_rendered = d["count"](ready_selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    # AWS WAF first, exactly as playwright_scraper.py does it: it is the one
    # challenge this site has been measured serving, and `window.gokuProps`
    # appears on nothing else. This engine used to skip the check entirely,
    # so it could detect a WAF page through the generic markers and then have
    # nothing to do about it.
    challenge = detect_aws_waf(html, d["current_url"]())
    if challenge is None:
        html_challenge = detect_recaptcha_v3(html, d["current_url"]())
        runtime_challenge = detect_recaptcha_in_page(
            lambda js: driver.execute_script(f"return ({js})();"),
            page_url=d["current_url"]())
        challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > 0:
        logger.info("%s detected via %s, but content is already on the "
                    "page — not solving it.", challenge.kind, challenge.source)
        return False
    # Section 19: "unsolvable" is a property of a PAGE -- it means the page
    # carries no widget. An AWS WAF CHALLENGE-action page is exactly that:
    # challenge.js only, no puzzle rendered, nothing for a solver to work
    # on. Sending it anyway buys a token for a widget that was never there,
    # and `createTask` validates little enough to take the money. A browser
    # that runs the script passes this by itself, which is why the wait
    # above is still the right thing to do for it.
    if challenge.is_aws_waf and not challenge.has_captcha_widget:
        logger.info("AWS WAF %s action and no captcha widget on the page — "
                    "not sending this to the solver API; there is no puzzle "
                    "here to buy an answer to. NOTE: this engine does not "
                    "wait for the challenge script to finish either, so a "
                    "run can report blocked on a page a browser might have "
                    "cleared by itself — measured on a GitHub runner "
                    "2026-09-17, blocked 0.4s after the fetch.",
                    challenge.aws_waf_action)
        return False

    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key set, so this challenge is not "
                       "being solved. 2captcha does solve this type — set "
                       "TWOCAPTCHA_KEY (or pass --twocaptcha-key) to use it.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score,
                               proxy=proxy)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    if challenge.is_aws_waf:
        # AWS WAF reads its answer back from a COOKIE, not a form field, so
        # there is no `g-recaptcha-response` textarea for INJECT_TOKEN_JS to
        # fill. add_cookie() applies to the driver's current domain, which is
        # this page's, and survives the refresh below.
        host = urlparse(d["current_url"]()).hostname or ""
        try:
            driver.add_cookie({"name": AWS_WAF_COOKIE, "value": token,
                               "domain": host, "path": "/"})
        except WebDriverException as e:
            logger.error("Could not set %s (%s).", AWS_WAF_COOKIE, e)
            return False
        logger.info("Set %s for %s — refreshing to let the WAF re-check.",
                    AWS_WAF_COOKIE, host)
    else:
        try:
            driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
        except WebDriverException as e:
            logger.error("Could not inject the token (%s).", e)
            return False
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    ready_selector = page_flow.ready_selector(args.mode)
    ready_count = page_flow.ready_count(args.mode)
    content_timeout = page_flow.content_timeout_ms(args.mode)
    html, state, load_failed = None, "ok", False

    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d: %s", page_num, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating (%d/%d).",
                           mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session, args, ready_selector,
                                    proxy=pool.current if pool else None):
            time.sleep(1)

        html = d["content"]() or ""
        if is_self_clearing_challenge(html):
            # Shared with the other two engines -- see page_flow for why the
            # wait condition is "the site's content hooks appeared", not
            # "the challenge markers went away".
            html, _cleared = page_flow.wait_out_self_clearing_challenge(
                d["content"], d["sleep"], url=d["current_url"](), log=logger.info)
        state = page_flow.classify(html, url=d["current_url"]())

        if not page_flow.should_retry(state):
            break

        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error("Refused or unreachable (state=blocked) — saved to %s.", debug_html)
        outcome.blocked_by = "blocked"
        outcome.final_url = d["current_url"]()
        return outcome

    if state == "content":
        seen = page_flow.wait_for_count(d["count"], d["sleep"], ready_selector,
                                        ready_count, content_timeout)
        if seen >= ready_count:
            time.sleep(0.3)
        else:
            logger.info("No rows appeared within %.0fs (%d/%d matched) — if "
                        "this is a genuinely empty page, that is the expected "
                        "answer.", content_timeout / 1000, seen, ready_count)
        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if page_num == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)

    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if state != "content" else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s.",
                     vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    rows = _parse_for_mode(html, final_url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(rows), page_num)

    if rows and args.mode == "category" and not is_showcase_locale(url):
        priced = sum(1 for r in rows if r.price is not None)
        coverage = priced / len(rows)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(rows), 100.0 * coverage)
        # Every tile on a priced locale carries a price (291 of 291 measured
        # 2026-09-18). The showcase locales are excluded at the `if` above,
        # not here: warning about their correct 0% would train the reader to
        # ignore this warning.
        if coverage < page_flow.PRICE_COVERAGE_FLOOR:
            logger.warning("Price coverage on page %d (%.0f%%) is below the "
                           "%.0f%% floor — check for a parsing regression "
                           "rather than assuming these products are "
                           "genuinely unpriced. %s is a priced locale.",
                           page_num, 100.0 * coverage,
                           100.0 * page_flow.PRICE_COVERAGE_FLOOR,
                           locale_of(url))

    if not rows:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser saw to %s.", debug_html)

    outcome.rows = rows
    outcome.final_url = final_url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    stop_reason = "single_page_mode" if args.mode not in PAGINATED_MODES else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint "
                       "the remote browser has its own exit.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel "
                       "page fetching (--mode transfers) is implemented in "
                       "playwright_scraper.py. Running sequentially.")

    session = _Session(args, pool).open()
    try:
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode not in PAGINATED_MODES:
            pass
        else:
            seen_keys.update(r.sku for r in first.rows if r.sku is not None)
            site_next = _next_page_href(session)
            addressable = page_flow.pagination_is_addressable(
                args.mode, first.final_url, site_next)
            planned = ([page_url(first.final_url, n) for n in range(2, args.pages + 1)]
                      if addressable else None)

            for page_num in range(2, args.pages + 1):
                if planned:
                    url = planned[page_num - 2]
                else:
                    href = _next_page_href(session)
                    if not href:
                        logger.info("No further pagination link on page %d "
                                    "— stopping here.", page_num - 1)
                        stop_reason = "pagination_exhausted"
                        break
                    url = href

                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for r in outcome.rows
                                  if r.sku is None or r.sku not in seen_keys)
                seen_keys.update(r.sku for r in outcome.rows if r.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end.", page_num)
                    stop_reason = "no_new_products"
                    break
                if page_flow.is_thin_page(len(outcome.rows), len(first.rows)):
                    logger.warning(
                        "Page %d returned only %d row(s), well under page "
                        "1's %d — possibly a page beyond the listing's "
                        "real depth, or a markup regression. Continuing; "
                        "this is informational only and does not change "
                        "the exit code.", page_num, len(outcome.rows),
                        len(first.rows))
                if page_num < args.pages:
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation after page {page_num}")
                        session.relaunch()
                    time.sleep(args.delay)
    finally:
        session.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.rows, merged_seen, key="sku")
        if len(fresh) < len(oc.rows):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.rows) - len(fresh))
        all_rows.extend(fresh)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url) or "givenchybeauty.com",
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(description="Givenchy Beauty scraper (Selenium edition)")
    p.add_argument("--mode", choices=["category", "product"], default="category",
                   help="category (default): one listing page, one row per "
                        "product tile. product: one product page, one row.")
    p.add_argument("--url", default=None,
                   help="The exact page to read. Required for --mode product. "
                        "Falls back to $GIVENCHY_URL, then to what "
                        "--category builds.")
    p.add_argument("--category", default="makeup/lips",
                   help="Category path WITHOUT the locale prefix, e.g. "
                        "makeup/lips. Take real paths from the locale's own "
                        "sitemap_1-category.xml: they are NOT shared between "
                        "locales.")
    p.add_argument("--site-locale", default="us", choices=list(LOCALES),
                   help="The locale PATH PREFIX on www.givenchybeauty.com "
                        "(default us). Not --locale, which is the browser's "
                        "own. int/en and ru publish no prices.")
    p.add_argument("--pages", type=int, default=1,
                   help="Accepted for family compatibility; a listing is one "
                        "page on this site (robots-disallowed paging).")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Ignored in this engine — see playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="givenchy_products")
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-sessions", type=int, default=None,
                   help="With --proxy pointing at a 2Captcha proxy gateway, mint this many session-pinned exits from that one credential instead of keeping a file of them. Each session is a different exit address (measured 2026-09-17: ten sessions, ten distinct addresses). Has no effect with --proxy-file, and is refused for a non-2Captcha host.")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--fp-tags", default="Windows")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="A CREDENTIAL-FREE debuggerAddress only — Selenium "
                        "cannot authenticate on the WebSocket upgrade the "
                        "way Playwright/pyppeteer do. See this file's "
                        "docstring.")
    p.add_argument("--dump-html", default=None, metavar="PATH")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)

    if not args.url:
        if args.mode == "product":
            p.error("--mode product needs --url (or $GIVENCHY_URL). Take one "
                    "from a locale's sitemap_0-product.xml, or run --mode "
                    "category first and use a row's url.")
        args.url = f"{BASE}/{args.site_locale}/{args.category.strip('/')}/"

    if not is_robots_allowed(args.url):
        p.error(f"{args.url} matches a Disallow rule in this site's "
                f"robots.txt and will not be fetched. Category sitemaps do "
                f"list such URLs (93 of ru's 445): filter them with "
                f"product_parser.is_robots_allowed.")

    if not is_supported_host(args.url):
        why = unsupported_reason(args.url)
        p.error(f"{site_host(args.url) or args.url!r} {why}. Supported: "
                f"{', '.join(sorted(HOSTS))}.")
    if args.mode not in PAGINATED_MODES and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s.", args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except RemoteAPIError as e:
        # The Fingerprint API call failed on its own terms -- not
        # Givenchy blocking a page (exit 3) and not an unexpected bug
        # (exit 1, which still gets a real traceback). See
        # output_writer.RemoteAPIError. (Selenium has no credentialed
        # --cdp-endpoint path to fail this way -- see _cdp_host_port, which
        # exits 2 for that as a usage error before any connection attempt.)
        logger.error("%s", e)
        sys.exit(EXIT_REMOTE_API_ERROR)
