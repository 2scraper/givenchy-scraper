"""
product_parser.py
-----------------
Everything this repo knows about givenchybeauty.com. The engines and
`page_flow.py` carry no site knowledge; it all lives here.

The site, as measured
---------------------
Salesforce B2C Commerce (Demandware), confirmed from the markup
(`Sites-givenchy-beauty-int-Site`) and from robots.txt
(`Disallow: */on/demandware.store/*`).

**One hostname, eleven locales, and the locale is a PATH PREFIX** —
`www.givenchybeauty.com/us/...`, `/gb/...`, `/int/en/...`. Five of the eleven
are two segments long (`ca/en`, `de/de`, `en/ee`, `fr/fr`, `int/en`), which
is why locale detection here walks a list longest-first instead of taking
`path.split("/")[1]`. The list is the site's own, read from
`sitemap-global.xml` (re-read 2026-09-18, unchanged: 11 entries).

**Two page kinds, with different primary data sources.** Measured
2026-09-17/18 across 20 captures:

| Page | `ld+json` | Primary source |
|---|---|---|
| category listing | **0** on every locale checked | the tile's own attributes |
| product, priced locale (`us`, `gb`, `jp`) | **1**, a complete `Product` | JSON-LD |
| product, showcase locale (`int/en`, `ru`) | **0** | the DOM, and there is no price to read |

So CLAUDE.md §4's "JSON-LD first" is right for product pages and wrong for
listings, where the site publishes none. Per §4's amazon-scraper precedent
the listing's primary source is then the site's OWN data attributes — here a
`data-gtm` JSON blob plus `data-pid`/`data-masterid`/`data-currency` on every
tile — with the URL pattern kept as the fallback.

**Prices agree between the two views, so there is no overlay.** Measured
2026-09-18: `/us/p/le-rouge-satin-silk-lipstick-F20100269.html` publishes
JSON-LD `price: "50.00"`, `priceCurrency: "USD"`, and the same product's tile
in `/us/makeup/lips/` carries `content="50.00"` with
`meta[itemprop=priceCurrency] content="USD"`. Farfetch's three-price
disagreement is not this site, so §4's tile-price overlay is deliberately not
ported — it would be dead code that looks load-bearing.

Traps this file exists to absorb
--------------------------------
1. **`data-brand` on a tile holds the PRICE, not the brand** (`data-brand="50"`
   on a $50.00 lipstick). The brand is inside the `data-gtm` JSON. Reading
   the obviously-named attribute gives a column of numbers named "brand".
2. **A showcase locale still advertises a currency.** `int/en` and `ru` tiles
   carry `data-currency="USD"` and `data-gtm` `"price":0` while rendering no
   price container at all. Both are the platform's fallbacks. This file reads
   neither: no price container means `price=None` and `currency=None`.
3. **Three price shapes, not one.** A normal tile uses
   `div.product-price-container`; a GIFT SET uses
   `div.price-container.set-price-volume`, which sometimes carries full
   microdata plus a struck list price and sometimes only a bare `£82.00`
   text node. Anchoring on `product-price-container` alone silently loses
   every set (8 of 25 tiles on `/gb/makeup/lips/`).
4. **A tile's price text can contain a SECOND price**: `gb` renders
   `£41.00 (£1,025.00/Kg)`, a unit price whose comma grouping is a better
   match for a naive money regex than the real price is. Parenthesised
   groups are stripped before matching.
5. **The tile links to the MASTER id, the tile IS a variant.**
   `data-pid="P000476"` sits on a tile whose href ends `-F20100269.html`.
   The PDP's JSON-LD `sku` agrees with the pid, not the URL, so `sku` is the
   pid and `master_id` holds the other.
6. **Product URLs have two shapes and ids are not all `[A-Z]\\d{8}`.**
   `/p/slug-ID.html` (757 of 763 measured) and `/p-ID.html` (2), with the jp
   slug free to contain `/` and non-ASCII, and ids running `F20100269`,
   `P000170`, `851113` and `PSETUK_00043`. `_SKU_IN_URL_RE` matched 763 of
   763 product URLs across five locale sitemaps on 2026-09-18, and none of
   four category URLs used as negative controls.
7. **The category sitemaps list robots-disallowed URLs.** 93 of ru's 445
   entries are `search?cgid=...`, which robots.txt forbids (`Disallow:
   *cgid*`). `is_robots_allowed()` filters them; a caller that feeds a
   sitemap straight to a fetcher will otherwise request pages this site asks
   robots not to.
"""

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import (parse_qsl, unquote, urljoin, urlparse, urlsplit,
                          urlunsplit)

from bs4 import BeautifulSoup

from output_writer import Product

# ---------------------------------------------------------------------------
# Host and locales
# ---------------------------------------------------------------------------
# Unlike every sibling repo, there is no per-country hostname to verify here:
# one host serves all eleven locales. `HOSTS` therefore has two entries, not
# eleven, and the mediamarkt.lu lesson (CLAUDE.md §5 — a same-brand host that
# turned out to run on a different platform) applies to the LOCALE list
# instead. Each locale below was fetched live and its markup compared against
# `/us/`: `productTile-wrapper`, `data-pid` and the `giv-ProductTile-*` class
# names are identical on all of them.
BASE = "https://www.givenchybeauty.com"
SITE_HOST = "www.givenchybeauty.com"
HOSTS = {"givenchybeauty.com", "www.givenchybeauty.com"}

# Ordered LONGEST FIRST so `int/en` is recognised before a bare `int` could
# be, and read from the site's own sitemap index rather than guessed.
LOCALES: Tuple[str, ...] = ("ca/en", "de/de", "en/ee", "fr/fr", "int/en",
                            "es", "gb", "it", "jp", "ru", "us")

# Locales that render no price anywhere — not a bug and not a block, but the
# site showing a catalogue without commerce. Measured 2026-09-18: 17 of 17
# `int/en` tiles and 9 of 9 `ru` tiles carry no price container, and their
# product pages publish no JSON-LD. A run against one of these is expected to
# return rows with `price=None`, and the engines do not treat that as a
# failure.
SHOWCASE_LOCALES = ("int/en", "ru")

DEFAULT_LOCALE = "int/en"


def site_host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def unsupported_reason(url: str) -> Optional[str]:
    host = site_host(url)
    if not host:
        return "is not a valid URL"
    if host in HOSTS:
        return None
    if "givenchy" in host:
        # Givenchy's couture site (givenchy.com) is a different platform from
        # the beauty site and shares none of the markup this file reads.
        # Naming the reason beats "is not a Givenchy site", which would be
        # false and would send the reader looking for a typo (CLAUDE.md §5).
        return ("is a Givenchy site, but not givenchybeauty.com — this repo "
                "reads the Salesforce B2C Commerce beauty storefront, whose "
                "markup the couture site does not share")
    return "is not a givenchybeauty.com URL"


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def locale_of(url: str) -> str:
    """The locale path prefix of `url`, or `DEFAULT_LOCALE` if it carries none.

    Longest-first so `/int/en/...` is not read as the locale `int`, and
    boundary-checked so a category called `international` cannot match `int`.
    """
    path = urlparse(url).path if "//" in url else url
    path = path if path.startswith("/") else "/" + path
    for loc in sorted(LOCALES, key=len, reverse=True):
        if path == "/" + loc or path.startswith("/" + loc + "/"):
            return loc
    return DEFAULT_LOCALE


def is_showcase_locale(url_or_locale: str) -> bool:
    loc = url_or_locale if url_or_locale in LOCALES else locale_of(url_or_locale)
    return loc in SHOWCASE_LOCALES


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
# Read from the live file on 2026-09-17 (captures/cap-robots.txt) and kept as
# literal substrings rather than a parser: the file has one `User-agent: *`
# group and no Allow rules, so substring containment IS the rule here.
#
# This matters more than it looks: `*?start=*` and `*sz*` are the site's own
# listing pagination, and `*cgid*` covers 93 of ru's 445 sitemap category
# entries. See `page_url` for what that costs and what this repo does instead.
ROBOTS_DISALLOWED = ("wishlist", "/Wishlist-Add", "/account", "/error",
                     "/on/demandware.store/", "prefn", "prefv", "srule",
                     "pmin", "cgid", "pid", "dwcont", "format", "?start=",
                     "?home", "sz", "search?q=")


def is_robots_allowed(url: str) -> bool:
    """Whether robots.txt permits fetching `url`.

    Checked against path+query only: robots rules are path patterns, and the
    scheme/host would otherwise let `sz` match nothing useful.
    """
    parts = urlsplit(url)
    target = parts.path + (("?" + parts.query) if parts.query else "")
    return not any(rule in target for rule in ROBOTS_DISALLOWED)


# ---------------------------------------------------------------------------
# Canonical URLs
# ---------------------------------------------------------------------------

def sitemap_url(locale: str, kind: str = "product") -> str:
    """A locale's own sitemap. `kind` is `product`, `category`, `content`,
    `folder` or the bare index."""
    index = {"product": "sitemap_0-product.xml",
             "category": "sitemap_1-category.xml",
             "content": "sitemap_2-content.xml",
             "folder": "sitemap_3-folder.xml",
             "other": "sitemap_4.xml"}
    return f"{BASE}/{locale}/{index.get(kind, kind)}"


GLOBAL_SITEMAP_URL = f"{BASE}/int/en/sitemap-global.xml"


def product_url(locale: str, sku: str, slug: str = "p") -> str:
    """A product URL built from an id.

    NOT verified to be slug-independent the way a sibling repo's site is:
    nobody
    has measured whether this site serves a product page with a wrong or
    missing slug, so this builds the SLUGLESS form the site itself publishes
    (`/int/en/p-PSETUS_00013.html`, 2 of 763 measured URLs) rather than
    inventing a slug and implying it does not matter.
    """
    if slug and slug != "p":
        return f"{BASE}/{locale}/p/{slug}-{sku}.html"
    return f"{BASE}/{locale}/p-{sku}.html"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# The listing's own paging is Demandware's `?start=N&sz=25`, read from the
# grid's `data-sort-options` on 2026-09-18 — and robots.txt disallows BOTH
# parameters (`*?start=*`, `*sz*`). There is no `link[rel=next]` anywhere on
# a category page (0 occurrences, measured on 26 listing captures) and no numbered
# pager: the site loads more tiles with a "show more" control that requests
# exactly those disallowed URLs.
#
# So this repo does not page a listing. `page_url` returns the URL unchanged
# for every page number, which makes CLAUDE.md §7's layer 3 — "this page
# added no new sku" — the terminating condition, and an engine asked for
# `--pages 5` stops after one fetch with `pagination_exhausted` instead of
# requesting a page robots asks it not to.
#
# This is not a limit on what can be collected: `sitemap_0-product.xml` is
# robots-ALLOWED and lists every product in a locale (763 URLs across five
# locales, measured 2026-09-18), so full coverage runs through `--mode
# product` over `parse_sitemap()`. See the README's "Covering a whole
# locale".
PAGE_PARAM = "start"
PAGE_SIZE = 25


def page_url(url: str, page_num: int) -> str:
    """`url` unchanged — this site's listing pagination is robots-disallowed.

    Kept with the family signature because `page_flow` and all three engines
    call it, and because returning the input is the honest answer rather than
    a missing function: a caller comparing `page_url(u, 2) == u` learns that
    the listing is single-page, which is exactly what `pagination_is_
    addressable` needs to know.
    """
    return url


def page_number_from_url(url: str) -> Optional[int]:
    """Read a page number back out of a URL.

    A URL this repo BUILDS never carries one (see `page_url`), but a URL a
    user pastes might: `?start=25&sz=25` is what the site's own show-more
    control requests, and reading it keeps a hand-built URL's page number out
    of the sidecar's "page 1" column.
    """
    query = dict(parse_qsl(urlsplit(url).query))
    if PAGE_PARAM not in query:
        return 1
    try:
        start = int(query[PAGE_PARAM])
    except ValueError:
        return None
    size = PAGE_SIZE
    if "sz" in query:
        try:
            size = int(query["sz"]) or PAGE_SIZE
        except ValueError:
            size = PAGE_SIZE
    return start // size + 1


# ---------------------------------------------------------------------------
# Ids and categories from URLs
# ---------------------------------------------------------------------------
# Matched 763 of 763 product URLs from the us/gb/int-en/ru/jp product
# sitemaps on 2026-09-18, and none of `/us/makeup/lips/`, `/us/`,
# `/int/en/sitemap_0-product.xml` or `/us/c/makeup-F1.html`.
#
# Written to survive four shapes seen in those sitemaps:
#   /us/p/fantasque-P000170.html            the ordinary case
#   /int/en/p-PSETUS_00013.html             no slug at all
#   /gb/p/l-interdit-refillable-set-PSETUK_00046.html   underscore in the id
#   /jp/p/<kanji>/<kanji>-(...)-F70000338.html          slug with a slash
# The greedy `.*-` is what makes the LAST hyphen-delimited token the id, so a
# hyphenated slug cannot donate a fragment of itself.
_SKU_IN_URL_RE = re.compile(r"/p(?:/|-)(?:.*-)?([A-Za-z0-9_]+)\.html$")


def sku_from_url(url: Optional[str]) -> Optional[str]:
    """The product id in `url`, or None if it is not a product URL.

    Unquotes first so a percent-encoded jp slug does not hide the `-ID.html`
    suffix behind `%2D`-style escapes.
    """
    if not url:
        return None
    path = unquote(urlsplit(url).path)
    m = _SKU_IN_URL_RE.search(path)
    return m.group(1) if m else None


def is_product_url(url: Optional[str]) -> bool:
    return sku_from_url(url) is not None


# A path that is a locale root, a sitemap, or one of the platform's own
# routes is not a category even though it has the shape of one.
_NOT_A_CATEGORY = ("/on/demandware.store/", "/on/demandware.static/",
                   "sitemap", "/search", "/account", "/wishlist", "/error")


def _path_without_locale(url: str) -> str:
    """`url`'s path with its locale prefix removed, if it carries one.

    A URL that carries NO recognised locale keeps its whole path: `locale_of`
    answers DEFAULT_LOCALE for it, and stripping a prefix the path does not
    have would silently turn every such URL into "not a category". That was
    a real bug, caught by the `/international/x/` case in the suite — a
    first segment that merely STARTS with a locale code.
    """
    path = unquote(urlsplit(url).path)
    loc = locale_of(url)
    if path.startswith("/" + loc + "/") or path == "/" + loc:
        path = path[len("/" + loc):]
    return path


def is_category_url(url: Optional[str]) -> bool:
    if not url:
        return False
    path = unquote(urlsplit(url).path)
    if is_product_url(url) or any(x in path for x in _NOT_A_CATEGORY):
        return False
    return bool(_path_without_locale(url).strip("/"))


def category_from_url(url: Optional[str]) -> Optional[str]:
    """The category path of a listing URL, locale stripped: `makeup/lips`.

    Returns None for a product URL — a product row's `category` comes from
    the page's breadcrumb, not from its own address.
    """
    if not is_category_url(url):
        return None
    return _path_without_locale(url).strip("/") or None


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Every grouping convention CLAUDE.md §4 lists, plus the no-break spaces a
# rendered page actually uses (a plain space is the rare form).
_SPACES = "    "

# Longest first, so `US$` is not swallowed by `$`. Only the symbols this
# site's eleven locales can print are listed; an unrecognised symbol yields
# no currency rather than a plausible-looking wrong one (§4, tier 5).
_CURRENCY_BY_SYMBOL: Tuple[Tuple[str, str], ...] = (
    ("US$", "USD"), ("CA$", "CAD"), ("C$", "CAD"),
    ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("₽", "RUB"), ("$", "USD"),
)

# `$` alone is ambiguous across this site's locales, and the locale is the
# only disambiguator available in the markup. Used ONLY for the 7-in-317
# tiles that render a bare price with no microdata; anything read this way is
# recorded as `tile-text`, never as a fact.
_AMBIGUOUS_SYMBOL_LOCALE = {("$", "ca/en"): "CAD", ("$", "us"): "USD"}

_ISO_CODES = ("USD", "EUR", "GBP", "JPY", "RUB", "CAD")

# A bare number with optional grouping and an optional 1-2 digit decimal
# tail. Built once rather than per call.
_MONEY_RE = re.compile(
    r"(\d{1,3}(?:[.,%s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)" % _SPACES)

# `349,–` is a round price in German/Dutch retail. The lookahead keeps a
# range (`349,–500`) from merging into one number.
_DASH_CENTS_RE = re.compile(r"([,.])[–—-](?!\d)")

# Both word orders: German `-16%`, Turkish `-%10,34`. Removed BEFORE matching,
# because a rejected match has still consumed the currency symbol beside it.
_PERCENT_RE = re.compile(r"[-−+]?\s*(?:%\s*\d[\d.,]*|\d[\d.,]*\s*%)")

# `gb` renders a unit price beside the real one: `£41.00 (£1,025.00/Kg)`.
# Whatever is in brackets is never the shelf price.
_PARENTHESISED_RE = re.compile(r"\([^()]*\)")


def parse_money(text: Optional[str], locale: str = DEFAULT_LOCALE
                ) -> Tuple[Optional[float], Optional[str]]:
    """`(amount, currency)` from a rendered price string.

    Returns `(None, None)` rather than a partial reading when no number is
    found: a currency with no amount is not a price.
    """
    if not text:
        return None, None
    cleaned = _PARENTHESISED_RE.sub(" ", text)
    cleaned = _PERCENT_RE.sub(" ", cleaned)
    cleaned = _DASH_CENTS_RE.sub(r"\g<1>00", cleaned)

    currency = None
    for code in _ISO_CODES:                      # a written code names itself
        if re.search(r"\b%s\b" % code, cleaned):
            currency = code
            break
    if currency is None:
        for symbol, code in _CURRENCY_BY_SYMBOL:
            if symbol in cleaned:
                currency = _AMBIGUOUS_SYMBOL_LOCALE.get((symbol, locale), code)
                break

    m = _MONEY_RE.search(cleaned)
    if not m:
        return None, None
    return _to_float(m.group(1)), currency


def _to_float(raw: str) -> Optional[float]:
    """A grouped number string to a float.

    The three rules CLAUDE.md §4 names: last separator wins when both appear;
    exactly three trailing digits after a lone separator is a THOUSANDS group
    (no currency here has a 3-digit subunit); spaces are always grouping.
    """
    s = raw.strip()
    for ch in _SPACES:
        s = s.replace(ch, "")
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        tail = s.rsplit(sep, 1)[1]
        s = s.replace(sep, "") if len(tail) == 3 else s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: Optional[str], locale: str = DEFAULT_LOCALE) -> Optional[float]:
    return parse_money(text, locale)[0]


# ---------------------------------------------------------------------------
# Bot challenges
# ---------------------------------------------------------------------------
# givenchybeauty.com is fronted by Akamai and Cloudflare (measured
# 2026-09-17 from response headers). The marker set is the family's, kept
# broad on purpose (CLAUDE.md §8: different geos surface different
# challenges) and CHECKED against pages known to be good: 0 occurrences of
# every marker below across 14 served captures — 8 listing pages in 5 locales,
# 3 product pages and 6 section indexes, measured 2026-09-18. A marker that
# fires on a good page is worse than no marker.
BOT_CHALLENGE_MARKERS = {
    "AWS WAF": ("captcha.awswaf.com", "token.awswaf.com",
                "window.gokuProps", "awsWafCookieDomainList"),
    "Cloudflare": ("Attention Required! | Cloudflare", "cf-error-details",
                   "cf-turnstile", "Just a moment...", "cf-chl-"),
    # Akamai's edge-level refusal, and — separately — its BEHAVIOURAL
    # interstitial, which this repo met live on 2026-09-18 through the
    # Scraping Browser API: a 4,383-byte document with the real page
    # nowhere in it. Both sets score 0 on all 14 served captures.
    #
    # Splitting them is not cosmetic. The `sec-cpt` page CLEARS ITSELF —
    # it ships a script that calls `location.reload(true)` once its
    # challenge XHR returns — so the right response is to WAIT for it,
    # which `is_self_clearing_challenge` below lets an engine detect. An
    # `Access Denied` page will never become the product page no matter
    # how long anyone waits.
    "Akamai": ("_abck", "ak_bmsc", "Access Denied - Akamai",
               "errors.edgesuite.net"),
    "Akamai sec-cpt": ("sec-if-cpt-container", "sec-bc-tile-container",
                       "scf-akamai-logo"),
    "DataDome": ("datadome", "geo.captcha-delivery.com"),
    "PerimeterX/HUMAN": ("px-captcha", "_px3", "perimeterx"),
    "reCAPTCHA": ("recaptcha/api.js", "g-recaptcha"),
    "hCaptcha": ("hcaptcha.com/captcha",),
    "generic": ("Access Denied", "Request unsuccessful",
                "sorry, you have been blocked"),
}

# Which vendors mean "a challenge that can be paid to clear" (state
# `captcha`, policy solve=True) rather than "an edge-level refusal" (state
# `blocked`, solve=False). Akamai is deliberately NOT here: its bot manager
# refuses at the edge rather than presenting a widget, and CLAUDE.md §19's
# rule cuts the other way too — offering a solver a page with no widget
# buys a task built from nothing. If an Akamai page here is ever measured
# carrying a real widget, move it and say what was measured.
SOLVABLE_VENDORS = ("reCAPTCHA", "hCaptcha", "Cloudflare", "AWS WAF")

# The Akamai `sec-cpt` interstitial carries no widget and no sitekey: its
# markup is a progress bar plus a script that reloads the page once the
# challenge XHR returns. So it is not offered to the solver — CLAUDE.md §19
# cuts both ways, and `createTask` validates almost nothing and would charge
# for a task built from a page with nothing in it to solve. This repo does
# not implement a paid path for this challenge; it waits the page out, which
# is what the page itself is asking for.
SELF_CLEARING_CHALLENGE_MARKERS = BOT_CHALLENGE_MARKERS["Akamai sec-cpt"]


def is_self_clearing_challenge(html: str) -> bool:
    """Whether `html` is a challenge page that reloads itself once passed.

    Measured live on 2026-09-18: a product page fetched over the Scraping
    Browser API came back as 4,383 bytes of Akamai `sec-cpt` markup. The
    page state for it is `blocked` (so a run can never report success with a
    row of nulls, which is what happened before this was added), but an
    engine that has a live page should spend a bounded wait on it BEFORE
    accepting that — see page_flow.CHALLENGE_SETTLE_MS.
    """
    if not html:
        return False
    scrubbed = _EXTENSION_TAG_RE.sub("", html)
    return any(m in scrubbed for m in SELF_CLEARING_CHALLENGE_MARKERS)

WAF_ACTION_HEADER = "x-amzn-waf-action"

# The Scraping Browser API's auto-solve extension injects its own hunter
# scripts into every page it loads, and two of the strings it injects
# (`cf-turnstile`, the awswaf interceptor) are in the marker set above — so
# this guard is live code here, not the dead copy CLAUDE.md §8 warns about
# adding speculatively.
_EXTENSION_TAG_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']+[\"'][^>]*>.*?</script>",
    re.IGNORECASE | re.DOTALL)

# The site's own asset signature. Present 276-512 times on every one of the
# 14 served captures (measured 2026-09-18) and structurally absent from an
# interstitial, which is CLAUDE.md §8's "a served page is built out of the
# site's own assets" applied here.
_SITE_ASSET_MARKER = "Sites-givenchy-beauty"

# Positive content hooks, each measured to be unambiguous:
#   js-ProductItem  — exactly one per product tile; 0 on every product page.
#   js-pdp          — 41-113 on every product page; 0 on every listing.
# Both are 0 on a served-but-empty category (`/es/.../sets/`), which is how
# that page reads as `empty` rather than as content or as a block.
_LISTING_HOOK = "js-ProductItem"
_PRODUCT_HOOK = "js-pdp"


def detect_bot_challenge(html: str, url: Optional[str] = None) -> Optional[str]:
    """The challenge vendor found in `html`, or None."""
    if not html:
        return None
    scrubbed = _EXTENSION_TAG_RE.sub("", html)
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(marker in scrubbed for marker in markers):
            return vendor
    return None


def _header(headers: Optional[Dict[str, str]], name: str) -> Optional[str]:
    """Case-insensitive header lookup — every layer here spells them
    differently."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def detect_page_state(html: str, status: Optional[int] = None,
                      url: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None) -> str:
    """content | blocked | captcha | empty — see page_flow.STATE_POLICY.

    Ordered by how much each check PROVES, per CLAUDE.md §17, not by cost:

    1. `x-amzn-waf-action` is the front naming its own action, so nothing
       overrides it.
    2. An empty body proves a failed fetch and nothing else.
    3. A sitemap is content: it is XML, carries none of the site's HTML
       assets, and an engine enumerating one must not read that as a block.
    4. The site's OWN hooks are an unambiguous positive, and run BEFORE the
       vendor scan because the scan can match a marker that is not the
       site's at all (the auto-solve extension, §19).
    5. The vendor scan then only refines the reason for a page that already
       failed to look like content.
    6. A non-2xx status comes last among the positive tests: it proves
       something went wrong without saying what. It still VETOES step 4 — a
       non-2xx response may not be read as content however good its body
       looks.

    A served category page with no tiles returns `empty`, which is a correct
    answer and not a retryable one: `/es/.../sets/` and `/it/.../sets/` are
    both real, both 200, and both list nothing (measured 2026-09-17).
    """
    action = (_header(headers, WAF_ACTION_HEADER) or "").strip().lower()
    if action == "captcha":
        return "captcha"
    if action in ("block", "challenge"):
        return "blocked"

    if not html:
        return "blocked"

    status_ok = status is None or 200 <= status < 300
    if status_ok and ("<urlset" in html or "<sitemapindex" in html):
        return "content"

    if status_ok and _SITE_ASSET_MARKER in html and (
            _LISTING_HOOK in html or _PRODUCT_HOOK in html):
        return "content"

    vendor = detect_bot_challenge(html, url=url)
    if vendor:
        return "captcha" if vendor in SOLVABLE_VENDORS else "blocked"

    if not status_ok:
        return "blocked"

    return "empty"


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
# Class names, not build hashes: `giv-ProductTile-*` and `pdp__*` are this
# theme's own vocabulary and were identical across all five locales checked.
# The URL pattern (`_SKU_IN_URL_RE`) remains the fallback anchor per
# CLAUDE.md §4, and `data-pid` is the primary one per its amazon-scraper
# precedent.
SELECTORS = {
    # A tile is the OUTERMOST node covering exactly one product: it carries
    # `data-pid` and there is exactly one per product, measured 317 tiles /
    # 317 `[data-pid]` elements across 20 captures. So no ancestor-widening
    # walk is needed here and none is written — the "junk-link data theft"
    # failure of CLAUDE.md §4 cannot arise when the site labels the tile
    # itself.
    "tile": "div.productTile-wrapper[data-pid]",
    "tile_article": "article.giv-ProductTile-item",
    "item_link": "a.giv-ProductTile-link[href]",
    "tile_name": "span.giv-ProductTile-name a",
    "tile_subname": "p.giv-ProductTile-productSubName",
    "tile_image": "img.giv-ProductTile-picture",
    "tile_badge": "span.giv-ProductTile-tag",
    "tile_swatch": "li.giv-ProductTile-swatch",
    "tile_swatch_more": "li.giv-ProductTile-more",
    # BOTH price containers. Anchoring on the first alone loses every gift
    # set — 8 of 25 tiles on `/gb/makeup/lips/`.
    "price_box": "div.product-price-container, div.price-container",
    "price_sales": "span.sales span.value[content]",
    "price_strike": "span.strike-through span.value[content]",
    "price_currency": "meta[itemprop=priceCurrency][content]",
    # Product page
    "pdp_name": "span.pdp__name, .pdp__name",
    "pdp_title": "h1",
    "pdp_price": "span.js-price-sales",
    "pdp_price_list": "span.js-price-list",
    "pdp_size": "span.pdp__capsule__value",
    "pdp_breadcrumb": "ol.breadcrumbs",
    "pdp_breadcrumb_link": "a.breadcrumb-link",
}

_OUT_OF_STOCK_CLASS = "out-of-stock-product-tile"
_IN_STOCK_CLASS = "in-stock-product-tile"


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _gtm(node) -> dict:
    """The tile's `data-gtm` JSON, or `{}`.

    Wrapped because this is site-supplied JSON in an HTML attribute: a
    malformed blob on one tile must cost that tile's extras, not the run.
    """
    raw = node.get("data-gtm") if node is not None else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Tile prices
# ---------------------------------------------------------------------------

def _content_float(node) -> Optional[float]:
    """A microdata `content="50.00"` attribute as a float.

    The `content` attribute is read rather than the element's text because
    the text is the RENDERED price, which on `gb` is `£41.00 (£1,025.00/Kg)`
    — a real price followed by a unit price whose grouping is a better match
    for a money regex than the price itself.
    """
    if node is None:
        return None
    raw = node.get("content")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return _to_float(str(raw))


def tile_price(box, locale: str = DEFAULT_LOCALE):
    """`(price, currency, original_price, price_source)` for one tile's price box.

    Three shapes, all real and all measured on 2026-09-18:

      1. microdata      284 of 317 tiles — `span.value[content]` + a
                        `meta[itemprop=priceCurrency]`. Both are facts.
      2. text only        7 of 317 — a `gb` gift set rendering a bare
                        `£82.00` with no microdata at all. The currency then
                        comes from the symbol, which §4 ranks as a guess.
      3. no price box    26 of 317 — every one of them a showcase locale.
                        Yields all-None rather than a zero.

    The struck price is kept only when it is ABOVE the sale price. That is
    §4's guard against reading an EU 30-day-low disclosure as a was-price: a
    low printed BELOW the price would give a negative discount on a product
    that is not discounted. None was found on this site (1 struck price in
    317 tiles, £134.00 against £105.20), and the guard costs one comparison.
    """
    if box is None:
        return None, None, None, None

    sales = box.select_one(SELECTORS["price_sales"])
    price = _content_float(sales)
    if price is not None:
        cur_node = box.select_one(SELECTORS["price_currency"])
        currency = _clean(cur_node.get("content")) if cur_node is not None else None
        original = _content_float(box.select_one(SELECTORS["price_strike"]))
        if original is not None and not original > price:
            original = None
        return price, currency, original, "tile-microdata"

    price, currency = parse_money(box.get_text(" ", strip=True), locale)
    if price is None:
        return None, None, None, None
    return price, currency, None, "tile-text"


def discount_pct(price: Optional[float], original: Optional[float]) -> Optional[float]:
    """Discount computed from the two figures, never read from the badge.

    The site prints its own `-21%`, but §4's Farfetch lesson is that a
    printed percentage can be the first of two compounding discounts. The
    computed value is the one that matches what the customer pays. Returns
    None — not 0 and not a negative — when the two figures are not what they
    were taken for.
    """
    if price is None or original is None or original <= 0 or original <= price:
        return None
    return round((original - price) / original * 100, 2)


# ---------------------------------------------------------------------------
# Category listings
# ---------------------------------------------------------------------------

def parse_category(html: str, base_url: str, page_num: int = 1) -> List[Product]:
    """Every product tile on a category listing page.

    Rows come out in the order the site laid them out; `dedupe_by_sku` in
    `output_writer` removes the repeats. Repeats are real: `/gb/makeup/lips/`
    served 25 tiles over 24 distinct ids on 2026-09-18.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    locale = locale_of(base_url)
    category = category_from_url(base_url)
    rows: List[Product] = []

    for index, tile in enumerate(soup.select(SELECTORS["tile"])):
        article = tile.select_one(SELECTORS["tile_article"])
        gtm = _gtm(tile) or _gtm(article)
        link = tile.select_one(SELECTORS["item_link"])
        href = link.get("href") if link is not None else None
        url = urljoin(base_url, href) if href else base_url

        # `data-pid` is the variant and the PDP's JSON-LD `sku` agrees with
        # it; the URL carries the MASTER id, so it is the fallback and not
        # the other way round.
        sku = _clean(tile.get("data-pid")) or _clean(gtm.get("id")) or sku_from_url(url)
        master = (_clean(article.get("data-masterid")) if article is not None else None) \
            or sku_from_url(href) or sku_from_url(url)

        price, currency, original, source = tile_price(
            tile.select_one(SELECTORS["price_box"]), locale)

        name_node = tile.select_one(SELECTORS["tile_name"])
        title = _clean(name_node.get_text(" ", strip=True)) if name_node is not None else None
        if not title and article is not None:
            title = _clean(article.get("data-name"))

        image = tile.select_one(SELECTORS["tile_image"])
        image_url = None
        if image is not None:
            # `src` is a placeholder until the lazy loader runs; `data-src`
            # is the real image and is present in server-rendered HTML.
            raw = image.get("data-src") or image.get("data-save-src") or image.get("src")
            image_url = urljoin(base_url, raw) if raw else None

        sub = tile.select_one(SELECTORS["tile_subname"])
        subtitle = _clean(sub.get_text(" ", strip=True)) if sub is not None else None
        if not subtitle:
            subtitle = _clean(gtm.get("productType"))

        badge_node = tile.select_one(SELECTORS["tile_badge"])
        badge = _clean(badge_node.get_text(" ", strip=True)) if badge_node is not None else None

        rows.append(Product(
            url=url,
            sku=sku,
            title=title,
            image_url=image_url,
            price=price,
            currency=currency,
            category=category,
            price_source=source,
            locale=locale,
            master_id=master,
            # NOT `data-brand`, which holds the price on this site — see the
            # module docstring's trap 1.
            brand=_clean(gtm.get("brand")),
            subtitle=subtitle,
            shade=_clean(gtm.get("variant")) or (
                _clean(article.get("data-color")) if article is not None else None),
            shade_count=_swatch_count(tile),
            shades=_swatch_names(tile),
            size=(_clean(article.get("data-size")) if article is not None else None),
            availability=_tile_availability(article, gtm),
            original_price=original,
            discount_pct=discount_pct(price, original),
            badge=badge,
            page=page_num,
            row_index=index,
        ))
    return rows


def _swatch_count(tile) -> Optional[int]:
    """How many shades the tile says exist: the swatches it draws plus the
    `+ 16 more colour available` overflow it does not."""
    shown = len(tile.select(SELECTORS["tile_swatch"]))
    more_node = tile.select_one(SELECTORS["tile_swatch_more"])
    extra = 0
    if more_node is not None:
        m = re.search(r"\d+", more_node.get_text(" ", strip=True) or "")
        extra = int(m.group(0)) if m else 0
    total = shown + extra
    return total or None


def _swatch_names(tile) -> Optional[List[str]]:
    """The shade names the tile renders, from each swatch's `aria-label`.

    Only the ones DRAWN -- a tile shows three and says "+ 16 more colour
    available", and the other sixteen are not in the listing HTML to be
    read. `_swatch_count` carries the total; this carries the names there
    are names for.
    """
    names = [_clean(li.get("aria-label")) for li in tile.select(SELECTORS["tile_swatch"])]
    names = [n for n in names if n]
    return names or None


def _tile_availability(article, gtm: dict) -> Optional[str]:
    """schema.org-shaped availability from the tile.

    The article's own class is the primary signal (5 of 317 tiles carried
    `out-of-stock-product-tile` on 2026-09-18); `data-gtm`'s `productStock`
    is the fallback for a tile that carries neither class.
    """
    classes = (article.get("class") or []) if article is not None else []
    if _OUT_OF_STOCK_CLASS in classes:
        return "OutOfStock"
    if _IN_STOCK_CLASS in classes:
        return "InStock"
    stock = (gtm.get("productStock") or "").strip().lower()
    if stock.startswith("in stock"):
        return "InStock"
    if stock.startswith("out of stock"):
        return "OutOfStock"
    return None


# ---------------------------------------------------------------------------
# Product pages
# ---------------------------------------------------------------------------

def _jsonld_blocks(soup) -> List[dict]:
    """Every JSON-LD object on the page, flattened.

    Handles the shapes CLAUDE.md §4 lists as legal and naive-parser-fatal: a
    block may be a list, and products may hide inside `@graph` rather than at
    the top level — which loses every product SILENTLY, reporting an empty
    page for an unread format. A malformed block costs itself, not the run.
    """
    out: List[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                graph = node.get("@graph")
                if isinstance(graph, (list, dict)):
                    stack.append(graph)
    return out


def _jsonld_product(soup) -> Optional[dict]:
    for node in _jsonld_blocks(soup):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "product" for t in types if t):
            return node
    return None


def _first_offer(node: dict) -> dict:
    """`offers` as a dict, whatever legal shape it arrived in.

    `"offers": null` is an explicit null, so a `.get("offers", {})` default
    does NOT apply — that is the `AttributeError` §4 names first. A list may
    also hold non-dicts.
    """
    offers = node.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for item in offers:
            if isinstance(item, dict):
                return item
    return {}


def _jsonld_image(node: dict, base_url: str) -> Optional[str]:
    """The first image URL from any of the four legal `image` shapes:
    a string, a list of strings, an `ImageObject`, or a list of them."""
    image = node.get("image")
    candidates = image if isinstance(image, list) else [image]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return urljoin(base_url, item.strip())
        if isinstance(item, dict):
            for key in ("url", "contentUrl"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return urljoin(base_url, value.strip())
    return None


_AVAILABILITY_RE = re.compile(r"(InStock|OutOfStock|PreOrder|BackOrder|SoldOut|Discontinued)",
                              re.IGNORECASE)


def _availability_from_schema(value: Optional[str]) -> Optional[str]:
    """`http://schema.org/InStock` to `InStock`. Matched rather than split on
    `/`, because the same field is written with and without a scheme, with
    `https`, and occasionally bare."""
    if not isinstance(value, str):
        return None
    m = _AVAILABILITY_RE.search(value)
    if not m:
        return None
    canonical = {"instock": "InStock", "outofstock": "OutOfStock",
                 "preorder": "PreOrder", "backorder": "BackOrder",
                 "soldout": "OutOfStock", "discontinued": "Discontinued"}
    return canonical.get(m.group(1).lower())


def parse_product(html: str, base_url: str) -> Optional[Product]:
    """One product page to one row, or None if the page is not one.

    JSON-LD is primary where it exists — on a priced locale it carries the
    price, the currency, the sku and the canonical offer URL, and
    `offers.priceCurrency` is a FACT the DOM is not allowed to overwrite
    (CLAUDE.md §4). On `int/en` and `ru` there is no JSON-LD at all and no
    price to read, so the DOM supplies name, size and breadcrumb and the row
    comes out with `price=None` rather than not at all.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    node = _jsonld_product(soup)

    name_node = soup.select_one(SELECTORS["pdp_name"])
    dom_name = _clean(name_node.get_text(" ", strip=True)) if name_node is not None else None
    title_node = soup.select_one(SELECTORS["pdp_title"])
    dom_title = _clean(title_node.get_text(" ", strip=True)) if title_node is not None else None

    canonical = soup.find("link", rel="canonical")
    canonical_url = urljoin(base_url, canonical.get("href")) if canonical is not None \
        and canonical.get("href") else None

    if node is None and dom_name is None and not is_product_url(base_url):
        return None

    locale = locale_of(base_url)
    offer = _first_offer(node) if node else {}

    price = currency = None
    source = None
    if offer:
        raw_price = offer.get("price")
        if raw_price is not None:
            try:
                price = float(str(raw_price).replace(",", "."))
            except ValueError:
                price = _to_float(str(raw_price))
        currency = _clean(offer.get("priceCurrency"))
        if price is not None:
            source = "jsonld"

    if price is None:
        # No structured price: either a showcase locale (no price exists) or
        # a markup change. `js-price-sales` is empty on the former, which is
        # why this can return None without it being an error.
        price_node = soup.select_one(SELECTORS["pdp_price"])
        text = _clean(price_node.get_text(" ", strip=True)) if price_node is not None else None
        dom_price, dom_currency = parse_money(text, locale)
        if dom_price is not None:
            price, currency, source = dom_price, currency or dom_currency, "pdp-text"

    original = None
    list_node = soup.select_one(SELECTORS["pdp_price_list"])
    if list_node is not None:
        list_price, _ = parse_money(_clean(list_node.get_text(" ", strip=True)), locale)
        if list_price is not None and price is not None and list_price > price:
            original = list_price

    url = (_clean(offer.get("url")) or canonical_url or base_url)
    # `sku` may be absent from an otherwise-valid Product block (CLAUDE.md
    # §4's last shape), so `mpn` and then the URL id are the fallbacks.
    sku = None
    if node:
        sku = _clean(node.get("sku")) or _clean(node.get("mpn"))
    # `base_url` before `url`: the requested address is the more specific of
    # the two on a showcase locale, where the page's own canonical points at
    # the MASTER (`/int/en/p/fantasque-F10100212.html` for a request for
    # `...-P000170.html`). Taking the canonical first would quietly widen a
    # variant row into its master.
    sku = sku or sku_from_url(base_url) or sku_from_url(url)

    brand = None
    if node:
        raw_brand = node.get("brand")
        if isinstance(raw_brand, dict):
            brand = _clean(raw_brand.get("name"))
        elif isinstance(raw_brand, str):
            brand = _clean(raw_brand)

    size_node = soup.select_one(SELECTORS["pdp_size"])
    crumb = soup.select_one(SELECTORS["pdp_breadcrumb"])

    return Product(
        url=url,
        sku=sku,
        title=(_clean(node.get("name")) if node else None) or dom_name or dom_title,
        image_url=(_jsonld_image(node, base_url) if node else None),
        price=price,
        currency=currency,
        category=_breadcrumb_category(crumb),
        price_source=source,
        locale=locale,
        master_id=sku_from_url(canonical_url) or sku_from_url(base_url),
        brand=brand,
        subtitle=(dom_title if dom_title and dom_title != (dom_name or "") else None),
        shade=None,
        shade_count=None,
        size=_clean(size_node.get_text(" ", strip=True)) if size_node is not None else None,
        availability=_availability_from_schema(offer.get("availability")),
        original_price=original,
        discount_pct=discount_pct(price, original),
        badge=None,
        page=1,
        row_index=0,
    )


def _breadcrumb_category(crumb) -> Optional[str]:
    """The breadcrumb trail as `fragrance/la-collection-particuliere/oud`.

    The leading "Givenchy Beauty" and the trailing product name are dropped:
    the first is the site, the second is the row's own title, and a consumer
    comparing a product row's `category` against a listing run's wants the
    path between them.
    """
    if crumb is None:
        return None
    parts = [_clean(a.get_text(" ", strip=True))
             for a in crumb.select(SELECTORS["pdp_breadcrumb_link"])]
    parts = [p for p in parts if p]
    if parts and parts[0].lower().startswith("givenchy"):
        parts = parts[1:]
    if len(parts) > 1:
        # The last crumb is the product itself, which is the row's `title`.
        parts = parts[:-1]
    return "/".join(p.lower() for p in parts) or None


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

def parse_sitemap(xml: str, allowed_only: bool = True) -> List[str]:
    """Every `<loc>` in a sitemap, in document order.

    `allowed_only` drops the entries robots.txt forbids, which is not
    hypothetical: 93 of the 445 URLs in ru's own category sitemap are
    `search?cgid=...`, a pattern robots.txt disallows (measured 2026-09-18).
    A caller feeding this straight to a fetcher would otherwise request them.
    """
    if not xml:
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if allowed_only:
        urls = [u for u in urls if is_robots_allowed(u)]
    return urls


def product_urls_from_sitemap(xml: str) -> List[str]:
    """Just the product URLs, for a caller enumerating a locale's catalogue."""
    return [u for u in parse_sitemap(xml) if is_product_url(u)]
