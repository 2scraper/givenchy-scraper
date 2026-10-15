# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and
takes about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but two things are
**not** masked: raw HTML dumps and your shell history. Before pasting any
output into an issue or a PR, replace keys, proxy passwords and full
`ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed.
That check is a backstop, not a review — a leaked key has to be rotated
whether or not the check caught it.

## Reporting a site change

Givenchy Beauty changing its markup is the normal way this stops working, and
it has its own issue template. Worth knowing before filing: **where the data
lives depends on the page kind**, and the two halves break differently.

1. **A category listing publishes no JSON-LD at all.** Every row comes off
   the tile's own attributes — `data-pid` (the variant id), the `data-gtm`
   JSON blob, and the price microdata (`span.value[itemprop=price][content]`
   plus `meta[itemprop=priceCurrency]`). A listing regression is almost
   always one of those.
2. **A product page on a priced locale publishes one complete JSON-LD
   `Product`**, and it is primary there: `offers.price`,
   `offers.priceCurrency`, `sku`. On the showcase locales (`int/en`, `ru`)
   there is none, and the DOM supplies name, size and breadcrumb with no
   price to read.
3. **The site's own class names** (`giv-ProductTile-*`, `pdp__*`,
   `price-container.set-price-volume`) everywhere else. The least durable of
   the three, and where a markup change is most likely to land.

If you are reporting a change, saying which of these three broke (and for
(3), which selector) narrows the fix a lot. `price_source` on the affected
row already tells you which instrument read the price.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a
single file of plain functions with inline HTML fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Several properties in this repo exist because they were once absent, cost
real time to find, and are now pinned by a test so a PR that breaks one fails
rather than silently regressing:

- **`sku` is the VARIANT id, not the master id.** One master id
  (`F20100269`) covers up to nineteen shades, and the product URL carries
  the master id, so reading `sku` from the URL collapses every shade of one
  lipstick into one row. The tile's own `data-pid` and the product page's
  JSON-LD `sku` agree on the variant id; `master_id` keeps the other.
- **Two price containers.** Tiles render their price in either
  `div.product-price-container` or `div.price-container`; anchoring on the
  first alone loses every gift set (8 of 25 tiles on `/gb/makeup/lips/`).
- **A listing's pagination is robots-disallowed on this site**, so
  `product_parser.page_url()` returns its input unchanged and a listing run
  is one fetch. Do not make it construct `?start=N&sz=25`: robots.txt
  forbids both parameters. Full-locale coverage goes through
  `sitemap_0-product.xml`, which robots allows.

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows on a page that was served, `5` the
  content was never obtained (load timeout, dead exit, an address that does
  not exist, a 2Captcha product call failing), `6` partial. A pipeline
  branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A
  served category that genuinely lists nothing (`/es/…/sets/` is one) is a
  correct answer to the question that was asked, not a failure. Neither is
  an address that does not exist (HTTP 410 here): that is `not_found`,
  not a block. `page_flow.STATE_POLICY` holds that for all three engines so
  they cannot disagree about it.
- **A challenge marker is not necessarily a real challenge on THIS site.**
  `product_parser.BOT_CHALLENGE_MARKERS` is broad and, unlike the DOM
  selectors above, was NOT measured against a real challenge here (see
  captcha_solver.py's closing note) — it is cheap insurance carried over
  from this family's other members. Do not narrow it on the assumption that
  Givenchy Beauty behaves like them; measure first if you touch it.
- **A `sku` already written earlier in the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`. One `gb` listing
  served the same tile twice (measured 2026-09-18). Check a change here
  against both modes, `category` and `product`.

There is also a naming check: certain phrases are banned repo-wide and the
suite fails naming them. If it trips, read the message — the phrase is wrong
for a reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it
  that way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has
  needed an explicit timeout its own API does not provide, and each has
  needed its own route out of the runtime — reporting a timeout is not the
  same as exiting on one. If you add a call to a remote browser or API,
  bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is a
  common bug class. A selector that matches the *wrong* element is worse
  than one that matches nothing, because the second one tells you.
- **Never present a guess as a fact.** A price or currency that could not
  be read is `None`, not `0`, a defaulted `"USD"` or a copied neighbour's
  value — see the showcase-locale (`int/en`, `ru`) tests in `smoke_test.py`,
  where tiles advertise `data-currency="USD"` and print no price at all.

### If your change needs a live run

Most do not — the suite covers the listing and product parsers, the
writers, the captcha classifier and the CLI contract against inline fixtures
cut from real captures. If yours genuinely needs givenchybeauty.com, say in
the PR what you ran (`--mode`, `--site-locale`, `--category` or `--url`),
whether you used a proxy, and what you got — including the exit code and
`.attempt.json`'s `stop_reason`.

Do not add anything that requires logging in, or that submits a form on the
site. This project only reads pages an anonymous visitor is served.

## Scope

This repo scrapes **public pages** on Givenchy Beauty: product listing
category listings and individual product
profiles, exactly as an anonymous visitor is served them. Out of scope:
anything behind a login, anything that submits a form, and anything that
defeats a protection rather than passing it the way an ordinary browser
does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
