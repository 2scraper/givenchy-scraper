# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow SemVer as closely as a CLI toolkit can. A patch release means
**fixes**, not that every flag is frozen: a default that changes behaviour
will be called out at the top of its entry rather than left to be discovered
from a bill or an empty output file.

## [1.0.1] — 2026-09-18

A fix release cut the same day as 1.0.0, because 1.0.0 shipped a workflow
that fails on checkout.

### Fixed

> **If you took the v1.0.0 source tarball, its `tests` workflow fails
> immediately.** Cloning `main` was always fine; only the tagged snapshot is
> affected. Nothing about the scraper itself is different — the parser, the
> engines and the output are byte-identical between the two tags.

- **`.github/workflows/tests.yml` carried its own inlined copies of two
  checks that also exist in `.github/ci_checks.py`.** The sample-output copy
  still imported `Player` and `Transfer`, the row classes this repo replaced
  with a single `Product` before its first release, so the workflow failed on
  a schema that is correct. Both steps now call the shipped checker — which
  also widens the `--help` check from seven entry points to eight — and no
  python heredocs remain in that workflow.

### Added

- The README now records the canary's first live run: a bare GitHub Actions
  runner, a freshly installed Playwright Chromium and no secret configured
  scraped `/us/makeup/lips/` and reported 16 rows, 16 priced,
  `status=complete`, with every canary assertion passing. That is a third
  independent address behind the "the paid path is not required" claim,
  alongside a residential `curl` and the same connection through a real
  browser.

## [1.0.0] — 2026-09-18

First release. Scrapes givenchybeauty.com — a Salesforce B2C Commerce
storefront — into one row per product, over three browser engines plus a
browserless HTTP client.

### Added

- **`--mode category`** — one listing page, one row per product tile.
  **`--mode product`** — one product page, one row. `--category` and
  `--site-locale` build the URL; `--url` overrides both.
- **Eleven locales**, served as path prefixes on one hostname. `int/en` and
  `ru` are showcase storefronts that publish no prices; rows from them carry
  `price: null` and `currency: null`, and the price-coverage warning is
  skipped for them rather than firing on a correct page.
- **`Product`**, 24 columns, opening on the family's nine-column prefix so
  output merges with the other repos in this family without translation.
  `sku` is the variant id; `master_id` is the style id the product URL
  carries.
- **`price_source` on every row** — `tile-microdata`, `tile-text`, `jsonld`
  or `pdp-text`. `diff_runs.py` gained a fifth bucket, `source_changed`: a
  price difference that arrives with a `price_source` difference describes
  our own two instruments, not the shelf price, and `--fail-on-change`
  ignores it.
- **robots.txt awareness** (`product_parser.is_robots_allowed`), new to this
  family. The engines refuse a disallowed URL before fetching it, and
  `parse_sitemap()` filters disallowed entries — which is not hypothetical:
  93 of the 445 URLs in `ru`'s own category sitemap are `search?cgid=…`,
  which the site's robots.txt forbids.
- **A bounded wait for a self-clearing challenge**
  (`page_flow.wait_out_self_clearing_challenge`), shared by all three
  engines. This pays down an open debt the family has carried since
  mediamarkt-scraper: the engines used to report a JS challenge as blocked
  ~0.4s after the fetch, producing false exit 3s.

### Measured

Every claim below was measured on 2026-09-17/18 and is repeated in the
README beside the command that produces it.

- **317 tiles across 20 captured listings**: 284 priced from the tile's own
  microdata, 7 from a gift-set tile's bare text, 26 with no price at all
  (every one of those a showcase locale). Outside the showcase locales,
  291 of 291 tiles carried a price.
- **JSON-LD and the tile agree**, so the family's tile-price overlay is
  deliberately **not** ported: a product page publishes `price: "50.00"`,
  `priceCurrency: "USD"`, and the same product's tile carries
  `content="50.00"` with the same currency.
- **Listing pagination is robots-disallowed.** The site pages with
  `?start=N&sz=25`; robots.txt forbids both parameters, there is no
  `link[rel=next]` on a category page (0 occurrences across 26 listing
  captures), so `page_url()` returns its input unchanged and `--pages N`
  above 1 is reported and ignored. Full-locale coverage runs through
  `sitemap_0-product.xml`, which robots allows.
- **The paid path is not required.** `playwright_scraper.py` against a local
  Chromium, with no key, no proxy and no `--cdp-endpoint`, returned exit 0
  and 16 rows, 16/16 priced in USD, for `/us/makeup/lips/` from a
  residential address in Moscow.

### Traps this release already absorbs

Each is encoded in the parser with the measurement that found it:

- **`data-brand` on a tile holds the PRICE** (`data-brand="50"` on a $50.00
  lipstick). The brand is in the tile's `data-gtm` JSON.
- **A showcase tile still advertises a currency** — `data-currency="USD"`
  and `data-gtm` `"price":0` on a page with no price anywhere. Neither is
  read.
- **Three tile price shapes, not one.** A gift set uses a different
  container, which sometimes carries a struck list price and sometimes only
  a bare `£82.00`. Anchoring on the ordinary container alone silently loses
  8 of 25 tiles on `/gb/makeup/lips/`.
- **A gb price node contains two prices**: `£41.00 (£1,025.00/Kg)`, where
  the unit price's grouping is the better match for a naive money regex.
- **Product ids are not one shape.** Across 763 product URLs in five locale
  sitemaps: `F20100269`, `P000170`, `851113` and `PSETUK_00043`, in
  `/p/slug-ID.html` and `/p-ID.html` forms, with jp slugs percent-encoded
  and free to contain a `/`.

### Fixed before release

- **A challenge page read as an ordinary empty page.** Met live on
  2026-09-18 through the Scraping Browser API: an Akamai `sec-cpt`
  behavioural interstitial, 4,383 bytes, classified as `empty` — a state
  this family does not retry — so the run reported **success with a row of
  nulls**. It is now its own detected vendor, it is `blocked` rather than
  `empty`, and the engines wait it out.
- **The wait condition was wrong on the first attempt.** Waiting for the
  challenge markers to disappear stopped one stage early, on a 2,173-byte
  ThreatMetrix device-fingerprinting document that is neither the challenge
  nor the page. The wait now runs until the site's own content hooks appear.
- **`is_category_url` dropped a path with no locale prefix**, so a URL whose
  first segment merely started with a locale code parsed to no category at
  all. Caught by this repo's own suite.
- **The Scraper API client re-fetched page 1 under page number 2** when
  `--pages > 1` was passed by a caller that built its own args object.
  Caught by this repo's own suite.

[1.0.1]: https://github.com/2scraper/givenchy-scraper/releases/tag/v1.0.1
[1.0.0]: https://github.com/2scraper/givenchy-scraper/releases/tag/v1.0.0
