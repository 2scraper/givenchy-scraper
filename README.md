# givenchy-scraper

Scrapes product data from **givenchybeauty.com** — a Salesforce B2C Commerce
(Demandware) storefront — into one row per product, as JSON and CSV. Three
browser engines plus a browserless HTTP client, one parser, one row schema.

Part of the 2scraper family: the row schema's first nine columns are
identical across every repo in it, so output from several of them merges
without translation.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --category makeup/lips --site-locale us
```

```
[+] Saved 16 row(s) -> givenchy_products.json
[+] Saved 16 row(s) -> givenchy_products.csv
[+] Wrote run metadata -> givenchy_products.meta.json (status=complete)
```

`sample_output.json` and `sample_output.csv` in this repo are that run,
unedited — `/us/makeup/lips/` on 2026-09-18.

---

## What it reads

| `--mode` | Reads | Gives |
|---|---|---|
| `category` (default) | one listing page | one row per product tile |
| `product` | one product page | one row |

```bash
# a listing, in a different locale and currency
python3 playwright_scraper.py --category makeup/lips --site-locale gb

# one product page
python3 playwright_scraper.py --mode product \
    --url https://www.givenchybeauty.com/us/p/fantasque-P000170.html

# no local browser at all, over 2Captcha's Scraper API
python3 scraper_api_client.py --key "$TWOCAPTCHA_KEY" \
    --category makeup/lips --site-locale us
```

The other two engines take the same flags:

```bash
python3 selenium_scraper.py  --category makeup/lips --site-locale us
python3 puppeteer_scraper.py --category makeup/lips --site-locale us
```

---

## Eleven locales, and two of them have no prices

The locale is a **path prefix**, not a hostname: one site serves all of them.

```
ca/en   de/de   en/ee   es   fr/fr   gb   int/en   it   jp   ru   us
```

(The site's own `sitemap-global.xml` listed exactly these 11 on 2026-09-18.)

**`int/en` and `ru` are showcase storefronts.** They render the catalogue
with no price anywhere: measured 2026-09-18, 17 of 17 tiles on
`/int/en/makeup/lips/` and 9 of 9 on a `/ru/` listing carried no price
container at all, and their product pages publish no structured data. A run
against one returns rows with `price` and `currency` both `null`. That is
the correct reading of a correct page, not a failure, and the
price-coverage warning is skipped for those two locales.

**Their tiles still advertise a currency.** A `/int/en/` tile carries
`data-currency="USD"` and `data-gtm` `"price":0`. Both are the platform's
fallbacks, not facts about the page, and this parser reads neither — no
price container means no price and no currency.

**Category paths are not shared between locales.** `/us/makeup/lips/` and
`/gb/makeup/lips/` both answer 200; `/fr/fr/makeup/lips/` answers **410**.
Take each locale's paths from its own `sitemap_1-category.xml`.

---

## Prices

Measured on 2026-09-18 across 317 tiles in 20 captured pages:

| Where the price came from | Tiles | `price_source` |
|---|---|---|
| the tile's own microdata (`itemprop="price"` + `priceCurrency`) | 284 | `tile-microdata` |
| a gift-set tile rendering a bare `£82.00` with no microdata | 7 | `tile-text` |
| no price on the page at all (every one a showcase locale) | 26 | `null` |

A product page adds a fourth: its JSON-LD `offers`, recorded as `jsonld`.

**The two views agree, so there is no overlay.** `/us/p/le-rouge-satin-silk-
lipstick-F20100269.html` publishes JSON-LD `price: "50.00"`,
`priceCurrency: "USD"`; the same product's tile in `/us/makeup/lips/`
carries `content="50.00"` with `priceCurrency` `USD`. Farfetch's
three-price disagreement is not this site, so the family's tile-price
overlay is deliberately **not** ported here.

**Currency is never guessed into existence.** It comes from the microdata or
the JSON-LD where those exist. For the 7 text-only tiles it is read from the
symbol, which is a weaker source and is why `price_source` says so. Where
there is no price, `currency` is `null` — never a defaulted `"USD"`.

**Discounts are computed, not read.** One tile in 317 carried a struck list
price: £134.00 against a sale price of £105.20, with the site printing
`-21%`. The row reports `discount_pct` **21.49**, computed from the two
figures, because a printed percentage can be the first of two compounding
discounts. A struck price is only recorded when it is **above** the sale
price.

There is no `lowest_price_30d` column: zero occurrences of a second struck
price of any kind across those 317 tiles. Add it back with a measurement,
not by analogy with a sibling repo.

---

## Pagination: there is none, and that is a decision

The site pages its listings with Demandware's `?start=N&sz=25`, requested by
a "show more" control. **`robots.txt` disallows both parameters** (`*?start=*`,
`*sz*`), along with `cgid`, `srule`, `prefn`, `prefv` and `pmin`. There is no
`link[rel=next]` on a category page — 0 occurrences across 26 listing captures — and
no numbered pager.

So this repo does not page a listing. `product_parser.page_url()` returns its
input unchanged, `--pages N` above 1 is reported and ignored, and a listing
run is one fetch returning whatever the server rendered — across
the captures here that is 0 to 32 tiles, most commonly 15 to 25. A category
with a single product is normal, and so is one with none.

### Covering a whole locale

`sitemap_0-product.xml` is robots-**allowed** and lists every product in a
locale. Measured 2026-09-18:

| locale | categories | products |
|---|---|---|
| `us` | 274 | 102 |
| `gb` | 255 | 214 |
| `int/en` | 438 | 188 |
| `jp` | 191 | 109 |
| `ru` | 445 | 150 |

```bash
python3 - <<'PY' > urls.txt
import urllib.request, product_parser as pp
xml = urllib.request.urlopen(pp.sitemap_url("us", "product")).read().decode()
print("\n".join(pp.product_urls_from_sitemap(xml)))
PY

while read url; do
    python3 playwright_scraper.py --mode product --url "$url" \
        --out "out/$(basename "$url" .html)" --format json --delay 2
done < urls.txt
```

`product_urls_from_sitemap()` filters robots-disallowed entries, which is not
hypothetical: **93 of the 445 URLs in `ru`'s own category sitemap** are
`search?cgid=…`, a pattern the site's robots.txt forbids.

---

## What a row looks like

```json
{
  "source": "givenchybeauty.com",
  "scraped_at": "2026-09-18T10:47:14.248408+00:00",
  "url": "https://www.givenchybeauty.com/us/p/le-rouge-satin-silk-lipstick-F20100269.html",
  "sku": "P000476",
  "title": "LE ROUGE SATIN SILK LIPSTICK",
  "image_url": "https://www.givenchybeauty.com/dw/image/v2/.../P000476_1.png?sw=300&sh=375",
  "price": 50.0,
  "currency": "USD",
  "category": "makeup/lips",
  "price_source": "tile-microdata",
  "locale": "us",
  "master_id": "F20100269",
  "brand": "Givenchy beauty",
  "subtitle": "The New Hydrating Long-Wear Lipstick",
  "shade": "PINK-204",
  "shade_count": 19,
  "shades": ["NUDE-1", "NUDE-5", "PINK-204"],
  "size": null,
  "availability": "InStock",
  "original_price": null,
  "discount_pct": null,
  "badge": "New",
  "page": 1,
  "row_index": 0
}
```

The first nine columns are the family prefix, in that order, in every repo in
this family.

**`sku` is the VARIANT id and `master_id` is the style id**, and they are not
interchangeable: one master (`F20100269`) covers up to nineteen shades of the
same lipstick, while `sku` is what the tile's `data-pid`, the product page's
JSON-LD `sku` and this repo's dedupe key all agree on. The **product URL
carries the master id**, so `sku` cannot be read from it.

**`shade_count` and `shades` disagree on purpose.** A tile draws three
swatches and says "+ 16 more colour available"; the count is 19, the list has
the 3 names the page actually contains. The other sixteen are not in the
listing HTML to be read.

---

## Traps that look like bugs

- **A category can legitimately return zero products.** `/es/…/sets/` and
  `/it/…/sets/` are both served, both HTTP 200, and both list nothing
  (measured 2026-09-17). The run reports exit 4, not a block.
- **A listing can serve the same tile twice.** `/gb/makeup/lips/` returned 25
  tiles over 24 distinct ids on 2026-09-18. The extra is deduped on `sku`, so
  the saved row count is one lower than the parsed count — that log line is
  not an error.
- **`data-brand` on a tile holds the PRICE.** It reads `data-brand="50"` on a
  $50.00 lipstick. The brand is inside the tile's `data-gtm` JSON, which is
  where this parser reads it from.
- **A gb price node contains two prices.** It renders `£41.00 (£1,025.00/Kg)`
  — the shelf price and a unit price, and the unit price's grouping is the
  better match for a naive money regex. Parenthesised groups are stripped
  before matching.
- **Product ids are not all one shape.** Across 763 product URLs in five
  locale sitemaps: `F20100269`, `P000170`, `851113` and `PSETUK_00043`, in
  `/p/slug-ID.html` and `/p-ID.html` forms, with jp slugs that are
  percent-encoded and may contain a `/`.
- **`brand` is "Givenchy beauty" on a tile and "Givenchy" on a product
  page.** The site writes it both ways; neither is normalised here, because
  normalising would hide that the two sources are different.

---

## Blocks, and what the paid products buy

Every plain `curl` fetch made while building this repo — from a
**residential address in Moscow** on 2026-09-17/18 — returned HTTP 200. The
artifacts are on disk and countable: 26 listing and product pages, 9 locale
home pages, 16 sitemaps and `robots.txt` — and **0 occurrences** of every
vendor marker this repo detects appears across any of them. (`captures/` is
gitignored — it is evidence for whoever has the working copy, not repo
content.)

Measured through this repo's own engine rather than `curl`, from the same
address on 2026-09-18: `playwright_scraper.py` against a **local** Chromium,
with no key, no proxy and no `--cdp-endpoint`, returned **exit 0, 16 rows,
16/16 priced in USD** for `/us/makeup/lips/` and one complete row for a
product page. So on that connection, on that day, the paid path was not
needed at all.

That is not a claim about the site in general, and a different exit did
behave differently:

**The Scraping Browser API exit behaved differently, twice over.** First,
through the Scraping Browser API on 2026-09-18, a product page came back as
an Akamai `sec-cpt` behavioural interstitial — 4,383 bytes with the real
page nowhere in it. That page **clears itself**: it ships a script that
reloads once its challenge completes. This repo waits it out (30s budget,
polled once a second) and re-reads, which is what the page is asking for;
in the measured case it cleared after 4 seconds and the run parsed the full
product row.

The wait condition is "the site's own content hooks appeared", not "the
challenge markers went away", because the interstitial resolved first into a
**2,173-byte ThreatMetrix device-fingerprinting document** (326 bytes of it
once the auto-solve extension's own injected script tags are stripped) — no
longer the challenge, and not the page either. A loop waiting for the markers to
disappear stopped there and produced a row of nulls while reporting success.

Second, after roughly ten runs through the same profile within an hour on
2026-09-18, that exit stopped completing a TLS handshake at all:
`net::ERR_SSL_PROTOCOL_ERROR` and `net::ERR_CONNECTION_CLOSED`, three
attempts each, while a plain `curl` from this machine fetched the same URL
in 0.54s with HTTP 200. That is the exit, not the site and not the scraper.
A run that never loads its page writes **no** `.meta.json` sidecar, which is
how it is told apart from a genuinely empty category — both exit 4, only one
of them leaves a sidecar saying so.

What the paid 2Captcha products actually buy here: volume from many
addresses (`--proxy`, `--proxy-file proxylist.txt`, `--proxy-sessions`), a
specific country and its currency (`--site-locale` picks the storefront;
the Scraping Browser endpoint's `country-` segment picks the exit), no local
browser infrastructure (`--cdp-endpoint`, `scraper_api_client.py`), and a
consistent device identity (`--fingerprint`). This repo does not implement a
paid solve for the Akamai interstitial: that page carries no widget and no
sitekey, so there is nothing to hand a solver.

---

## Configuration

Credentials live in `.env` and nowhere else — never in `argv`, which `ps`
can read. Copy `.env.example` and fill it in:

```
TWOCAPTCHA_KEY=...
GIVENCHY_CDP_ENDPOINT=ws://...@cb.2captcha.com:9222
GIVENCHY_PROXY=http://user:pass@host:port
GIVENCHY_URL=https://www.givenchybeauty.com/us/makeup/lips/
```

A proxy list goes in **`proxylist.txt`**, one URL per line; `.gitignore`
already refuses that name, and `--proxy-file proxylist.txt` reads it.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked |
| 4 | zero products |
| 5 | remote API error (the Fingerprint/Scraper API, not the site) |
| 6 | partial |

Every run writes `<out>.meta.json` beside its output with `status`,
`stop_reason` and which pages failed **by number**. A failed run writes no
sidecar and leaves the previous good output in place: a run that finds
nothing writes nothing, unless you pass `--allow-empty`.

## Comparing two runs

```bash
python3 diff_runs.py --old prices.2026-09-01.json --new prices.2026-09-08.json
```

Five buckets keyed on `sku`: added, removed, changed, **read differently**
and unmatched. The fourth exists because this site states a price four ways:
a price difference that arrives together with a `price_source` difference is
reported separately and ignored by `--fail-on-change`, because it describes
our own two instruments rather than the shelf price.

## Tests

```bash
python3 smoke_test.py      # or: pytest
```

No pytest fixtures, no network, no engine library required — the suite runs
green with none of the three installed and reports which groups it skipped.
Its fixtures are real captures, trimmed and scrubbed.

## Licence

MIT — see `LICENSE`.
