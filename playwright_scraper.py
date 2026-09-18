"""
givenchy-scraper — Playwright edition (primary engine)
======================================================

Scrapes givenchybeauty.com: a category listing into one row per product
tile, or a single product page into one row. One hostname serves all eleven
locales as PATH prefixes (`/us/`, `/gb/`, `/int/en/`), so there is no
per-country host to support and `--site-locale` picks the prefix.

    --mode category    a listing page. Give --category (e.g. makeup/lips)
                       with --site-locale, or --url. One fetch: see
                       "Pagination" below.
    --mode product     one product page. Give --url.

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money; the shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Givenchy Beauty
---------------------------------------
* **Nothing here needs JavaScript to render.** Every page captured for this
  repo arrived as complete HTML from a plain `curl` — 317 product tiles
  across 20 captures, their prices and ids included, with no scrolling and
  no hydration wait (measured 2026-09-17/18). What a browser buys here is a
  normal-looking TCP/TLS fingerprint and cookie jar, not rendering.
* **JSON-LD exists on product pages and nowhere else.** A priced locale's
  product page carries one complete `Product` block; a category page carries
  none, and a showcase locale's product page carries none either. See
  product_parser.py's docstring for the table and for why the tile's own
  `data-*` attributes are the listing's primary source.
* **Pagination is robots-disallowed, so a listing run is ONE fetch.** The
  site pages with `?start=N&sz=25` and robots.txt forbids both parameters,
  so `product_parser.page_url` returns its input unchanged and `--pages N`
  stops after page 1 with `pagination_exhausted`. To cover a whole locale,
  read its `sitemap_0-product.xml` — robots-allowed, and every product in
  the locale — and run `--mode product` over the result. The README's
  "Covering a whole locale" has the loop.
* **Two locales publish no prices at all.** `/int/en/` and `/ru/` are
  showcase storefronts: their tiles carry no price container, so rows come
  back with `price=None` and `currency=None`. That is the correct reading of
  a correct page, not a parsing failure, and the price-coverage warning
  below is skipped for them.
* **No captcha and no refusal was observed while building this repo** — 15
  plain fetches of category pages, product pages and sitemaps from a Moscow
  residential address on 2026-09-17/18, all HTTP 200, and 0 occurrences of
  every vendor marker in product_parser.BOT_CHALLENGE_MARKERS across the 12
  served HTML captures. That is a statement about those fetches from that
  address, not about the site in general; the detect-and-solve machinery is
  wired in and deliberately broad, because a different exit or a higher rate
  is a different experiment.

Usage
-----
    python playwright_scraper.py --category makeup/lips --site-locale us

    python playwright_scraper.py --category makeup/lips --site-locale gb \\
        --format csv --out gb_lips

    python playwright_scraper.py --mode product \\
        --url https://www.givenchybeauty.com/us/p/fantasque-P000170.html

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            detect_aws_waf, AWS_WAF_COOKIE,
                            INJECT_TOKEN_JS)
from product_parser import (parse_category, parse_product,
                            detect_bot_challenge, page_url,
                            site_host, is_supported_host, unsupported_reason,
                            HOSTS, BASE, LOCALES, DEFAULT_LOCALE,
                            is_showcase_locale, locale_of, is_robots_allowed,
                            is_self_clearing_challenge)
from output_writer import dedupe_by_key, finish_run, RemoteAPIError, EXIT_REMOTE_API_ERROR
import page_flow
import product_parser
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

# Modes with more than one page. NEITHER mode here has one: a product page
# is a single page by definition, and a listing's own paging is
# robots-disallowed (see the module docstring and `product_parser.page_url`).
# Kept as an empty tuple rather than deleted, because every `--pages` guard
# in this file and its two sibling engines reads it, and an empty tuple
# makes them all say the same true thing in one place.
PAGINATED_MODES = ()
# No mode here is independently addressable: `page_url` is a no-op on this
# site, so `page_flow.pagination_is_addressable` answers False for every
# listing and there is no second page for a second worker to fetch. Workers
# would send N times the traffic from N addresses to re-fetch page 1.
CONCURRENCY_CAPABLE_MODES = ()


def _chrome_ua(chromium_version: str) -> str:
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced. Collected per page, merged afterwards in page
    order — see the sibling repos' identical dataclass for why merging
    afterwards (rather than folding into shared state during the loop)
    is what keeps output deterministic under concurrency."""
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


def _driver(page):
    def count(selector):
        # Swallowed deliberately: page_flow.wait_for_count polls this every
        # 250ms while the page may be navigating, and a transient error from
        # one poll means "nothing there yet", not a failed run.
        try:
            return len(page.query_selector_all(selector))
        except (PWError, PWTimeout) as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    return {
        "count": count,
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


def _classify(page, html: str, status=None, headers=None) -> str:
    return page_flow.classify(html, status=status, url=page.url,
                              headers=headers)


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    See the sibling repos' identical class for why a rotation means tearing
    the whole browser down: cookies issued against one exit and replayed
    from another are a stronger signal than either address alone.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # A RemoteAPIError, not a re-raised PWError: this is the Scraping
        # Browser API's OWN connection refusing us, not the target site, and
        # it needs to be exit 5 rather than falling through to an uncaught
        # crash (1) — see output_writer.RemoteAPIError.
        raise RemoteAPIError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # Counters, not just log lines. Measured 2026-09-17 on a live AWS WAF
    # captcha over this endpoint: the engine's own detector fired, spent ~50s
    # in the PAID solver API, set a cookie and reloaded -- and only then did
    # `Captcha.detected` arrive. `Captcha.solveFinished` never did, because
    # the reload had already moved the page under the auto-solver's feet.
    # The Scraping Browser's auto-solve is the PRIMARY path here and the
    # solver API is the fallback; without somewhere to record these events
    # there is no way for the fallback to wait its turn.
    autosolve = {"enabled": False, "detected": 0, "finished": 0, "failed": 0}
    page.autosolve = autosolve
    if getattr(args, "no_autosolve", False):
        # Deliberately NOT enabling it. This exists so a challenge can be met
        # and left unsolved, which is the only way to measure how often one
        # clears on its own -- the control the family's section 19 requires
        # before a solve may be credited with anything. Without it, every
        # challenge this endpoint meets is solved before it can be observed.
        logger.warning("--no-autosolve: Captcha.setAutoSolve NOT enabled. "
                       "Challenges will be left unsolved. This is a "
                       "measurement mode, not a way to run a scrape.")
        return browser, context, page
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})

        def _on(event, level):
            def handler(*_):
                autosolve[event] += 1
                level("[Scraping Browser] CAPTCHA %s (%d).", event, autosolve[event])
            return handler

        cdp_session.on("Captcha.detected", _on("detected", logger.info))
        cdp_session.on("Captcha.solveFinished", _on("finished", logger.info))
        cdp_session.on("Captcha.solveFailed", _on("failed", logger.warning))
        autosolve["enabled"] = True
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).", e)
    return browser, context, page


# How long to let the Scraping Browser's own auto-solver work before the paid
# solver API is offered the challenge.
#
# MEASURED on a live AWS WAF captcha over this endpoint, 2026-09-17:
#   detected  -> +1s after the engine's own detector fired
#   finished  -> +96s after detected
# A first attempt used 45s and timed out one minute early, so the paid
# fallback ran anyway and its answer landed in the same second as the
# auto-solver's -- two events one second apart, nothing attributable, and
# $0.00290 spent for it. 180s is twice the measured figure, because the cost
# of waiting too long is latency and the cost of waiting too little is money
# plus a reload that moves the page out from under the primary path.
AUTOSOLVE_WAIT_MS = 180_000
AUTOSOLVE_POLL_MS = 500


def wait_for_autosolve(page, ready_selector: str) -> bool:
    """Give the Scraping Browser's auto-solver its turn. True if it cleared.

    Returns False when there is no auto-solver, when it reports failure, or
    when the budget runs out -- in all three cases the caller falls back to
    the solver API, which is what the fallback is for.
    """
    state = getattr(page, "autosolve", None)
    if not state or not state.get("enabled"):
        return False
    waited = 0
    logger.info("Waiting up to %.0fs for the Scraping Browser's own auto-solve "
                "before offering this to the solver API.", AUTOSOLVE_WAIT_MS / 1000)
    while waited < AUTOSOLVE_WAIT_MS:
        if state["finished"]:
            logger.info("Auto-solve reported solveFinished after %.1fs — the "
                        "primary path cleared it, nothing was charged to the "
                        "solver API.", waited / 1000)
            return True
        if state["failed"]:
            logger.warning("Auto-solve reported solveFailed after %.1fs — "
                           "falling back to the solver API.", waited / 1000)
            return False
        try:
            page.wait_for_timeout(AUTOSOLVE_POLL_MS)
        except Exception:  # noqa: BLE001 -- a navigating page is not a failure
            time.sleep(AUTOSOLVE_POLL_MS / 1000)
        waited += AUTOSOLVE_POLL_MS
    logger.info("Auto-solve did not report solveFinished within %.0fs "
                "(detected=%d) — falling back to the solver API.",
                AUTOSOLVE_WAIT_MS / 1000, state["detected"])
    return False


def _resolve_pagination_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args, ready_selector: str,
                              proxy=None) -> bool:
    """Detect and solve a challenge. True if something was solved.

    See captcha_solver.py's closing note: no challenge of this kind was
    observed anywhere in this repo's research. Wired in anyway, broad by
    design, per the family rule that detection should not be narrowed to
    what has been seen so far.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    already_rendered = len(page.query_selector_all(ready_selector))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    # AWS WAF first: it is the one challenge this site has actually been
    # measured serving (2026-09-16), and it is unambiguous when present --
    # `window.gokuProps` appears on nothing else. The reCAPTCHA detectors
    # below stay wired for the same "do not narrow detection to what has
    # been seen" reason they were wired originally.
    challenge = detect_aws_waf(html, page.url)
    if challenge is None:
        html_challenge = detect_recaptcha_v3(html, page.url)
        runtime_challenge = detect_recaptcha_in_page(
            lambda js: page.evaluate(js), page_url=page.url)
        challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > 0:
        logger.info("%s detected via %s, but content is already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source)
        return False

    # PRIMARY path first. Over the Scraping Browser API the browser's own
    # extension is what this repo documents as the default way to clear a
    # challenge; the solver API behind it is the fallback. Firing the paid
    # call on detection means the fallback always wins the race and the
    # primary is never exercised.
    if wait_for_autosolve(page, ready_selector):
        # Do NOT reload here. The auto-solver navigates the page itself once
        # it has the token, and a reload issued alongside that lands on
        # `net::ERR_ABORTED; maybe frame was detached?` -- measured
        # 2026-09-17, one second after a solveFinished that had genuinely
        # worked, leaving the run to parse a detached frame and report zero
        # rows. Wait for the content the solve was for instead, using the
        # same polled count every readiness wait in this repo uses.
        d = _driver(page)
        seen = page_flow.wait_for_count(
            d["count"], d["sleep"], ready_selector,
            page_flow.ready_count(getattr(args, "mode", "category")),
            page_flow.content_timeout_ms(getattr(args, "mode", "category")))
        if seen:
            logger.info("Page painted %d match(es) after the auto-solve.", seen)
        else:
            logger.info("Nothing painted after the auto-solve; reloading once.")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except (PWTimeout, PWError) as e:
                logger.warning("Reload after auto-solve failed (%s) — "
                               "continuing with whatever the page holds.", e)
        return True

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
        logger.warning("No 2captcha API key — continuing with whatever the "
                       "page already holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score,
                               proxy=proxy)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s) — continuing.", e)
        return False

    if challenge.is_aws_waf:
        # AWS WAF reads its answer back from a COOKIE, not from a form
        # field, so this path cannot reuse INJECT_TOKEN_JS: there is no
        # `g-recaptcha-response` textarea on the page to fill. The cookie is
        # set on the context (so it survives the reload that follows) and
        # scoped to the page's own host.
        host = urlparse(page.url).hostname or ""
        page.context.add_cookies([{
            "name": AWS_WAF_COOKIE,
            "value": token,
            "domain": host,
            "path": "/",
        }])
        logger.info("Set %s for %s — reloading to let the WAF re-check.",
                    AWS_WAF_COOKIE, host)
    else:
        page.evaluate(INJECT_TOKEN_JS, token)
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int) -> List:
    if args.mode == "category":
        return parse_category(html, url, page_num=page_num)
    if args.mode == "product":
        row = parse_product(html, url)
        return [row] if row is not None else []
    raise ValueError(f"unknown mode {args.mode!r}")


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Never raises for an EXPECTED failure — a
    timeout, a block, a captcha page are all recorded on the outcome."""
    outcome = PageOutcome(page_num=page_num, url=url)
    ready_selector = page_flow.ready_selector(args.mode)
    ready_count = page_flow.ready_count(args.mode)
    content_timeout = page_flow.content_timeout_ms(args.mode)

    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, state, load_failed = None, "ok", False
    # What actually went wrong, and how many tries it really took. Both are
    # kept because the "gave up" line below used to report NEITHER: it printed
    # `args.retries` whatever had happened, and never printed the exception at
    # all. A real run against the Scraping Browser died in 185ms and said
    # "after 3 attempt(s)" -- three things false at once (it was one attempt,
    # it was not a timeout, and the reason was discarded), which left nothing
    # to debug from. "Fail loudly" means saying WHAT failed.
    last_error, attempts_made = None, 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d: %s", page_num, url)
        load_failed, exit_failed = False, None
        resp_status, resp_headers = None, None
        for attempt in range(1, args.retries + 1):
            try:
                # The Response is KEPT, not discarded. Until v0.4.1 this
                # return value was thrown away and `_classify` was called
                # with no status and no headers at all -- which meant this
                # site's AWS WAF captcha (HTTP 405, `x-amzn-waf-action:
                # captcha`) reached the classifier as a bare 2331-byte body
                # with no vendor marker it recognised, and came out as
                # `empty`: exit 4, "zero rows", reported as a real answer
                # about the catalogue rather than as a challenge. `goto`
                # returns None for a same-document navigation, so neither
                # field is assumed present.
                response = session.page.goto(url, wait_until="domcontentloaded",
                                             timeout=60000)
                if response is not None:
                    resp_status = response.status
                    try:
                        resp_headers = response.headers
                    except PWError:
                        # Headers can be unavailable if the response was
                        # already discarded by a redirect chain; the markers
                        # still carry the detection on their own.
                        resp_headers = None
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                last_error, attempts_made = _mask_credentials(str(e)), attempt
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    # Named on the spot rather than only through the rotation
                    # branch below, which does not run at all without a pool —
                    # so a single-exit run used to swallow this entirely.
                    logger.error("The exit could not reach %s (%s). Over "
                                 "--cdp-endpoint this is the REMOTE browser's "
                                 "own exit failing, not this machine's "
                                 "network.", url, reason)
                    break
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating (%d/%d).",
                           mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args, ready_selector,
                                    proxy=pool.current if pool else None):
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        if is_self_clearing_challenge(html):
            # Shared with the other two engines -- see page_flow for why the
            # wait condition is "the content hooks appeared", not "the
            # challenge markers went away".
            d = _driver(session.page)
            html, cleared = page_flow.wait_out_self_clearing_challenge(
                lambda: _content_when_settled(session.page), d["sleep"],
                url=session.page.url, log=logger.info)
            if cleared:
                # The original response's status described the interstitial.
                resp_status, resp_headers = None, None
        state = _classify(session.page, html, status=resp_status,
                          headers=resp_headers)

        if not page_flow.should_retry(state):
            break

        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s) of %d. Last "
                     "error: %s", url, attempts_made or args.retries,
                     args.retries, last_error or "not recorded")
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error("Refused or unreachable (state=blocked) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).%s", debug_html,
                     f" Tried {block_retries + 1} exit(s)." if block_retries else "")
        outcome.blocked_by = "blocked"
        outcome.final_url = session.page.url
        return outcome

    if state == "content":
        d = _driver(session.page)
        seen = page_flow.wait_for_count(d["count"], d["sleep"], ready_selector,
                                        ready_count, content_timeout)
        if seen >= ready_count:
            session.page.wait_for_timeout(300)
        else:
            logger.info("No rows appeared within %.0fs (%d/%d matched) — if "
                        "this is a genuinely empty page (an exhausted "
                        "listing, an empty squad), that is the expected "
                        "answer.", content_timeout / 1000, seen, ready_count)
        html = _content_when_settled(session.page) or html

    if args.dump_html:
        dump_path = (args.dump_html if page_num == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    vendor = (detect_bot_challenge(html, url=session.page.url)
              if state != "content" else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s.",
                     vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    rows = _parse_for_mode(html, session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(rows), page_num)

    if rows and args.mode == "category" and not is_showcase_locale(url):
        priced = sum(1 for r in rows if r.price is not None)
        coverage = priced / len(rows)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(rows), 100.0 * coverage)
        # Every tile on a priced locale carries a price (291 of 291 measured
        # 2026-09-18), so a drop below the floor is a parsing regression to
        # flag rather than the site's own doing. The showcase locales are
        # excluded at the `if` above, not here: warning about their correct
        # 0% would train the reader to ignore this warning.
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
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser saw to %s "
                       "and %s.", debug_html, debug_png)

    outcome.rows = rows
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Not reached by any mode in this repo — see CONCURRENCY_CAPABLE_MODES.
    Kept, exercised by the offline suite with the browser stubbed out, and
    kept identical to its siblings': CLAUDE.md §10 requires the concurrency
    machinery to be tested where a live run cannot reach it, and a repo that
    deleted it would be the one that quietly diverged when a site's
    pagination became addressable again.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.rows:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the "
                                        "listing.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def _plan_or_chain(session, args, first_outcome):
    """Pages 2..N as a plan (list of URLs) if independently addressable, or
    None to chain link-to-link. See page_flow.pagination_is_addressable."""
    if args.pages < 2:
        return None
    site_next = None
    try:
        el = session.page.query_selector("link[rel='next'], a[rel='next']")
        href = el.get_attribute("href") if el else None
        site_next = _resolve_pagination_url(first_outcome.final_url, href) if href else None
    except PWError:
        site_next = None
    if page_flow.pagination_is_addressable(args.mode, first_outcome.final_url, site_next):
        return [page_url(first_outcome.final_url, n) for n in range(2, args.pages + 1)]
    logger.info("--mode %s: pagination is not independently addressable — "
                "chaining the site's own link one page at a time.", args.mode)
    return None


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

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode not in CONCURRENCY_CAPABLE_MODES:
            logger.info("--concurrency is ignored in --mode %s: this site's "
                        "listing pagination is robots-disallowed, so there "
                        "is one page to fetch and nothing for a second "
                        "worker to do.", args.mode)
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: "
                           "the Scraping Browser API allows one live "
                           "connection per profile.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every "
                           "worker leaves from the SAME address. Pass "
                           "--proxy-file to actually spread the load.",
                           concurrency)
        elif pool.rotates_per_page():
            logger.info("--proxy-rotate per-page has no effect above "
                        "--concurrency 1: each worker owns one exit for "
                        "its whole lifetime by design (CLAUDE.md §7), "
                        "rotated only per WORKER, not per page.")

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
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
                planned = _plan_or_chain(session, args, first)

                if args.pages > 1 and concurrency > 1 and planned is None:
                    logger.warning("--concurrency %d requested, but pages "
                                   "cannot be addressed independently — "
                                   "falling back to one page at a time.",
                                   concurrency)
                    concurrency = 1

                if args.pages > 1 and concurrency > 1:
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)]
                    logger.info("Fetching pages 2-%d across %d worker(s)%s.",
                                args.pages, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        stop_reason = "pages_unattempted"
                    session = None
                else:
                    for page_num in range(2, args.pages + 1):
                        if planned:
                            url = planned[page_num - 2]
                        else:
                            # Chain: follow the site's own next-link from
                            # the page we are actually standing on.
                            el = session.page.query_selector(
                                "link[rel='next'], a[rel='next']")
                            href = el.get_attribute("href") if el else None
                            if not href:
                                logger.info("No further pagination link on "
                                            "page %d — stopping here.",
                                            page_num - 1)
                                stop_reason = "pagination_exhausted"
                                break
                            url = _resolve_pagination_url(session.page.url, href)

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout" if outcome.load_failed
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = outcome.blocked_by is not None
                            break

                        fresh_count = sum(1 for r in outcome.rows
                                          if r.sku is None or r.sku not in seen_keys)
                        seen_keys.update(r.sku for r in outcome.rows
                                        if r.sku is not None)
                        if not fresh_count:
                            logger.info("Page %d added no rows not already "
                                        "seen — treating that as the end.",
                                        page_num)
                            stop_reason = "no_new_products"
                            break
                        if page_flow.is_thin_page(len(outcome.rows), len(first.rows)):
                            logger.warning(
                                "Page %d returned only %d row(s), well "
                                "under page 1's %d — possibly a page "
                                "beyond the listing's real depth, or a "
                                "markup regression. Continuing; this is "
                                "informational only and does not change "
                                "the exit code.", page_num,
                                len(outcome.rows), len(first.rows))
                        if page_num < args.pages:
                            if pool and pool.rotates_per_page():
                                pool.advance(f"per-page rotation after page {page_num}")
                                session.relaunch()
                            time.sleep(args.delay)
        finally:
            if session is not None:
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
    p = argparse.ArgumentParser(description="Givenchy Beauty scraper (Playwright edition)")
    p.add_argument("--mode", choices=["category", "product"], default="category",
                   help="category (default): one listing page, one row per "
                        "product tile. product: one product page, one row.")
    p.add_argument("--url", default=None,
                   help="The exact page to read. Required for --mode "
                        "product; for --mode category it overrides "
                        "--category/--site-locale. Falls back to "
                        "$GIVENCHY_URL, then to the URL --category builds.")
    p.add_argument("--category", default="makeup/lips",
                   help="Category path WITHOUT the locale prefix, e.g. "
                        "makeup/lips or fragrance/womens-fragrance. Combined "
                        "with --site-locale to build the URL. The default is "
                        "the listing this repo's canary watches. Take a "
                        "locale's real paths from its own "
                        "sitemap_1-category.xml: they are NOT shared between "
                        "locales (/fr/fr/makeup/lips/ answers 410).")
    p.add_argument("--site-locale", default="us", choices=list(LOCALES),
                   help="The locale PATH PREFIX on www.givenchybeauty.com "
                        "(default us). Not the same thing as --locale, which "
                        "is the browser's own locale: this one decides which "
                        "storefront is served, and therefore the currency. "
                        "int/en and ru publish no prices at all.")
    p.add_argument("--pages", type=int, default=1,
                   help="Accepted for family compatibility. This site's "
                        "listing pagination is robots-disallowed, so a "
                        "listing is one page and a value above 1 is reported "
                        "and ignored. See the module docstring.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and ignored: no "
                        "mode here has a second page to fetch in parallel "
                        "(see CONCURRENCY_CAPABLE_MODES).")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3).")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="givenchy_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US, matching the "
                        "English-language site this repo targets).")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--no-autosolve", action="store_true",
                   help="Do not enable Captcha.setAutoSolve on a "
                        "--cdp-endpoint session. A measurement mode: it lets "
                        "a challenge be MET and left unsolved, which is what "
                        "a control needs. Without a control, 'we solved it "
                        "and the page came back' cannot be told apart from "
                        "'the block expired'.")
    p.add_argument("--proxy-sessions", type=int, default=None,
                   help="With --proxy pointing at a 2Captcha proxy gateway, mint this many session-pinned exits from that one credential instead of keeping a file of them. Each session is a different exit address (measured 2026-09-17: ten sessions, ten distinct addresses). Has no effect with --proxy-file, and is refused for a non-2Captcha host.")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back blocked, retry it from this "
                        "many OTHER exits before giving up (default 2). "
                        "Needs a pool of more than one.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it. Needs --twocaptcha-key.")
    p.add_argument("--fp-tags", default="Windows",
                   help="Fingerprint OS-family tag (default Windows). See "
                        "fingerprint_client.py: this is ONE value, not a list.")
    p.add_argument("--fp-country", default=None, help="Fingerprint country, ISO 3166-1 alpha-2.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP, "
                        "e.g. ws://user:pass@host:port — the Scraping "
                        "Browser API endpoint.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on "
                        "success as well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)

    if not args.url:
        if args.mode == "product":
            # No URL is built for a product: this site's slugless product
            # form was seen on 2 of 763 sitemap URLs and nobody has measured
            # whether it is served for an arbitrary id, so building one from
            # a bare sku would be a guess presented as an address.
            p.error("--mode product needs --url (or $GIVENCHY_URL). Take one "
                    "from a locale's sitemap_0-product.xml, or run --mode "
                    "category first and use a row's url.")
        args.url = f"{BASE}/{args.site_locale}/{args.category.strip('/')}/"

    if not is_robots_allowed(args.url):
        # robots.txt disallows the platform's own listing parameters
        # (?start=, sz, cgid, srule, ...), and a locale's category sitemap
        # lists some of them outright — 93 of ru's 445 entries. Refusing
        # here, with the reason, beats fetching it and beats a silent skip.
        p.error(f"{args.url} matches a Disallow rule in this site's "
                f"robots.txt and will not be fetched. Category sitemaps do "
                f"list such URLs (93 of ru's 445): filter them with "
                f"product_parser.is_robots_allowed.")

    if not is_supported_host(args.url):
        why = unsupported_reason(args.url)
        p.error(f"{site_host(args.url) or args.url!r} {why}. Supported: "
                f"{', '.join(sorted(HOSTS))}.")

    if args.mode not in PAGINATED_MODES and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one "
                       "page to read. A listing's own paging is "
                       "robots-disallowed on this site; read the locale's "
                       "sitemap_0-product.xml and run --mode product over "
                       "it to cover the catalogue.", args.pages, args.mode)
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
