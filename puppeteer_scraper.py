"""
givenchy-scraper — pyppeteer edition
====================================

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
* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It ships here for parity, not because it is the better tool.
* **On Apple silicon it needs `--chromium-path`**: the bundled Chromium is
  x86_64 and dies under Rosetta.

Usage
-----
    python puppeteer_scraper.py --category makeup/lips --site-locale us

    python puppeteer_scraper.py --mode product \\
        --url https://www.givenchybeauty.com/us/p/fantasque-P000170.html

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import List, Optional

# At module level, deliberately — see CLAUDE.md §10 and §16. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip; CI's engine-smoke job fails on any reported skip. That
# mechanism only catches a broken import if importing this module actually
# requires the driver — hiding this inside a launch function let a sibling
# repo's pyppeteer engine die with NameError on a live run while `--help`,
# `compileall` and the whole offline suite stayed green.
from pyppeteer import launch, connect

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
logger = logging.getLogger("puppeteer_scraper")

# Modes with more than one page. NEITHER mode here has one: a product page is
# a single page by definition, and a listing's own paging is robots-disallowed
# (see playwright_scraper.py's docstring and `product_parser.page_url`). Kept
# as an empty tuple, identical in all three engines, rather than deleted.
PAGINATED_MODES = ()
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30

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
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged — see the
    sibling repos' identical class for the full rationale. The short
    version: every call gets an explicit, enforced timeout, which
    pyppeteer's own API does not offer, and "every remote call is bounded"
    is a family invariant this engine has to meet somehow.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        message = str(context.get("exception") or context.get("message") or "")
        if "Target closed" in message or "Connection closed" in message:
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"pyppeteer call did not return within {timeout}s")

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


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
    """One pyppeteer browser + page, relaunchable onto a different exit."""

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            try:
                self.browser = self.bridge.run(
                    connect(browserWSEndpoint=self.args.cdp_endpoint,
                            ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
                self.page = self.bridge.run(self.browser.newPage())
            except Exception as e:  # noqa: BLE001 - pyppeteer's own connect
                # failure isn't one well-known exception type (a websocket
                # refusal, a TimeoutError from _AsyncBridge, ...), and this
                # is the Scraping Browser API's OWN connection refusing us,
                # not the target site -- exit 5, not an uncaught crash (1).
                # See output_writer.RemoteAPIError and the identical guard
                # in playwright_scraper.py's _connect_remote.
                raise RemoteAPIError(
                    f"could not connect to --cdp-endpoint "
                    f"{_mask_credentials(self.args.cdp_endpoint)}: "
                    f"{_mask_credentials(str(e))}\n"
                    f"A Scraping Browser profile allows ONE live connection "
                    f"at a time, so this usually means another run still "
                    f"holds this `pid`. Wait for it to finish, or use a "
                    f"different pid."
                ) from None
            # Turn the Scraping Browser's own auto-solve on, exactly as
            # playwright_scraper._connect_remote does. Until v0.4.1 this
            # engine connected over CDP and then did NOT do this, so a run
            # with --cdp-endpoint got no auto-solve here while its twin got
            # one — silent engine drift on the paid path, and another
            # instance of the "does less than it says while reporting
            # success" shape. Selenium cannot join them: its
            # `debuggerAddress` takes a bare host:port with nowhere to put a
            # password, so it cannot reach an authenticated endpoint at all
            # (see the README's engine limits).
            if getattr(self.args, "no_autosolve", False):
                logger.warning("--no-autosolve: Captcha.setAutoSolve NOT "
                               "enabled. Challenges are left unsolved; this "
                               "is a measurement mode, not a way to scrape.")
                return self
            try:
                session = self.bridge.run(self.page.target.createCDPSession())
                self.bridge.run(session.send(
                    "Captcha.setAutoSolve",
                    {"autoSolve": True, "options": [{"type": "*"}]}))
                session.on("Captcha.detected", lambda *_: logger.info(
                    "[Scraping Browser] CAPTCHA detected on page."))
                session.on("Captcha.solveFinished", lambda *_: logger.info(
                    "[Scraping Browser] CAPTCHA solved automatically."))
                session.on("Captcha.solveFailed", lambda *_: logger.warning(
                    "[Scraping Browser] CAPTCHA auto-solve failed."))
                logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
            except Exception as e:  # noqa: BLE001 — a plain CDP endpoint that
                # is not the Scraping Browser simply does not carry the
                # Captcha domain; that is not a failure of the run.
                logger.info("Captcha.setAutoSolve not available on this "
                            "--cdp-endpoint (%s).", e)
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        credentials = None
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))

        launch_options = dict(headless=self.args.headless, args=launch_args,
                              ignoreHTTPSErrors=True, handleSIGINT=False,
                              handleSIGTERM=False, handleSIGHUP=False)
        chromium_path = getattr(self.args, "chromium_path", None)
        if chromium_path:
            # pyppeteer reads no environment variable for this, so an
            # unusable bundled build can only be worked around from here.
            launch_options["executablePath"] = chromium_path
            logger.info("Launching the Chromium at %s instead of pyppeteer's "
                        "own download.", chromium_path)
        self.browser = self.bridge.run(launch(**launch_options),
                                       timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script, fingerprint_user_agent
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = fingerprint_user_agent(fp)
        script = playwright_init_script(fp)
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            # pyppeteer's Page.evaluateOnNewDocument is a thin wrapper over
            # the same CDP call Selenium reaches via execute_cdp_cmd
            # ("Page.addScriptToEvaluateOnNewDocument") — same script,
            # same effect, native method instead of a raw CDP send.
            self.bridge.run(self.page.evaluateOnNewDocument(script))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not apply the fingerprint (%s).", e)

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        # Swallowed deliberately: page_flow.wait_for_count polls this every
        # 250ms while the page may be navigating, and a transient error from
        # one poll means "nothing there yet", not a failed run.
        try:
            return len(bridge.run(page.querySelectorAll(selector)))
        except Exception as e:  # noqa: BLE001 -- pyppeteer raises broadly here
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

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
    bridge, page = session.bridge, session.page
    return bridge.run(page.evaluate(
        "() => { const a = document.querySelector(\"link[rel='next'], "
        "a[rel='next']\"); return a ? (a.href || a.getAttribute('href')) : null; }"))


def handle_captcha_if_present(session, args, ready_selector: str,
                              proxy=None) -> bool:
    bridge, page = session.bridge, session.page
    html = _driver(session)["content"]()
    if html is None:
        return False

    already_rendered = len(bridge.run(page.querySelectorAll(ready_selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    # AWS WAF first, exactly as playwright_scraper.py does it: it is the one
    # challenge this site has been measured serving, and `window.gokuProps`
    # appears on nothing else. This engine used to skip the check entirely,
    # so it could detect a WAF page through the generic markers and then have
    # nothing to do about it.
    challenge = detect_aws_waf(html, page.url)
    if challenge is None:
        html_challenge = detect_recaptcha_v3(html, page.url)
        runtime_challenge = detect_recaptcha_in_page(
            lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
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
        # fill. Set on the page, scoped to its own host, and it survives the
        # reload below.
        host = urlparse(page.url).hostname or ""
        bridge.run(page.setCookie({"name": AWS_WAF_COOKIE, "value": token,
                                   "domain": host, "path": "/"}))
        logger.info("Set %s for %s — reloading to let the WAF re-check.",
                    AWS_WAF_COOKIE, host)
    else:
        bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)
    ready_selector = page_flow.ready_selector(args.mode)
    ready_count = page_flow.ready_count(args.mode)

    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d: %s", page_num, url)
        load_failed = False
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
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
                d["content"], d["sleep"], url=page.url, log=logger.info)
        state = page_flow.classify(html, url=page.url)

        if not page_flow.should_retry(state):
            break

        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            bridge, page = session.bridge, session.page
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
        content_timeout = page_flow.content_timeout_ms(args.mode)
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

    vendor = detect_bot_challenge(html, url=page.url) if state != "content" else None
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s.",
                     vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    rows = _parse_for_mode(html, page.url, args, page_num)
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
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser saw to %s.", debug_html)

    outcome.rows = rows
    outcome.final_url = page.url
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

    bridge = _AsyncBridge()
    session = _Session(bridge, args, pool).open()
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
        bridge.close()

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
    p = argparse.ArgumentParser(description="Givenchy Beauty scraper (pyppeteer edition)")
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
    p.add_argument("--no-autosolve", action="store_true",
                   help="Do not enable Captcha.setAutoSolve on a "
                        "--cdp-endpoint session (measurement mode: it "
                        "lets a challenge be met and left unsolved, "
                        "which is what a control needs).")
    p.add_argument("--proxy-sessions", type=int, default=None,
                   help="With --proxy pointing at a 2Captcha proxy gateway, mint this many session-pinned exits from that one credential instead of keeping a file of them. Each session is a different exit address (measured 2026-09-17: ten sessions, ten distinct addresses). Has no effect with --proxy-file, and is refused for a non-2Captcha host.")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--fp-tags", default="Windows")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an existing browser, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser "
                        "API endpoint. pyppeteer authenticates on the "
                        "WebSocket upgrade, so credentials here work "
                        "(unlike selenium_scraper.py).")
    p.add_argument("--dump-html", default=None, metavar="PATH")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    p.add_argument("--chromium-path", default=None,
                   help="Chromium/Chrome binary to launch instead of the one "
                        "pyppeteer downloads for itself. Needed on Apple "
                        "silicon: pyppeteer's bundled build is x86_64 only "
                        "(measured 2026-09-16 on revision 1181205) and dies "
                        "under Rosetta with a mach_port_rendezvous error the "
                        "moment it has finished printing its DevTools URL. "
                        "Point this at an arm64 Chromium -- Playwright's, or "
                        "'/Applications/Google Chrome.app/Contents/MacOS/"
                        "Google Chrome'. Not a credential, so the command "
                        "line is the right place for it.")
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
        # The Fingerprint API or the --cdp-endpoint connect failed on their
        # own terms -- not Givenchy blocking a page (exit 3) and not an
        # unexpected bug (exit 1, which still gets a real traceback). See
        # output_writer.RemoteAPIError.
        logger.error("%s", e)
        sys.exit(EXIT_REMOTE_API_ERROR)
