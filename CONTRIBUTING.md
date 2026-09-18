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
it has its own issue template. Worth knowing before filing: this site
publishes **no JSON-LD anywhere** (checked on every page kind this repo
covers — a ranking page, a squad page, a transfer list, a player profile).
So there is no "which of two paths broke" question the way an e-commerce
scraper has one; every field here comes straight off the DOM, anchored on
one of three kinds of hook:

1. **URL patterns** — `/profil/spieler/{id}`, `/verein/{id}`,
   `/jumplist/transfers/spieler/{id}/transfer_id/{id}`. A contract with
   search engines, and the most durable anchor this repo has.
2. **`schema.org/Person` microdata** on a player's own profile header
   (`itemprop="birthDate"`, `"nationality"`, `"height"`, `"affiliation"`) —
   real structured data, just not JSON-LD.
3. **The site's own class names** everywhere else (`data-header__label`,
   `posrela`, `hauptlink`, `table.items`) or column position within a row.
   The least durable of the three, and where a markup change is most likely
   to land.

If you are reporting a change, saying which of these three broke (and for
(3), which selector) narrows the fix a lot.

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

- **`data-header__label` appears on TWO different tags in a player's profile
  header** — `<li>` for birth date/position/agent, `<span>` for
  Joined/Contract expires — and selecting only `li` silently drops the
  latter. `_label_map()`'s docstring in `product_parser.py` has the story;
  the fix is a comma-separated selector, not a second function.
- **A club or player title attribute can be doubled on this site's own
  markup** — `title="Without ClubWithout Club"` is real, observed markup for
  the "Without Club" free-agent placeholder, not a scraping bug. Read the
  TEXT-bearing anchor (`td.hauptlink a`), not the first matching link in the
  cell (which can be an image-only wrapper with an empty text node), and let
  `_detitle()` catch the doubling as a second line of defence.
- **A listing's pagination is robots-disallowed on this site**, so
  `product_parser.page_url()` returns its input unchanged and a listing run
  is one fetch. Do not make it construct `?start=N&sz=25`: robots.txt
  forbids both parameters. Full-locale coverage goes through
  `sitemap_0-product.xml`, which robots allows.

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `6` partial. A pipeline branches on
  these.
- **An EMPTY page is never retried and never counted as blocked.** An
  served category that genuinely lists nothing (`/es/…/sets/` is one) or a
  roster data are correct answers to the question that was asked, not
  failures. `page_flow.STATE_POLICY` holds that for all three engines so
  they cannot disagree about it.
- **A challenge marker is not necessarily a real challenge on THIS site.**
  `product_parser.BOT_CHALLENGE_MARKERS` is broad and, unlike the DOM
  selectors above, was NOT measured against a real challenge here (see
  captcha_solver.py's closing note) — it is cheap insurance carried over
  from this family's other members. Do not narrow it on the assumption that
  Givenchy Beauty behaves like them; measure first if you touch it.
- **A `sku` already written by an earlier page of the same run is dropped,
  not duplicated.** See `dedupe_by_key` in `output_writer.py`. Note that
  `sku` means something different per mode — a player id for
  the variant id on every row here (the master id covers many shades, so it
  player can transfer more than once in one run's data) — so a change here
  should be checked against all four modes, not just one.

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
- **Never present a guess as a fact.** A market value or fee that could not
  be read is `None`, not `0` or a copied neighbour's value — see
  `parse_market_value`'s handling of `"-"` and the null-price tests in
  `smoke_test.py`.

### If your change needs a live run

Most do not — the suite covers the four parsers, the writers, the captcha
classifier and the CLI contract against inline fixtures cut from real
captures. If yours genuinely needs transfermarkt.com, say in the PR what you
ran (`--mode`, and `--club-id`/`--player-id`/`--season` if relevant), whether
you used a proxy, and what you got — including the exit code and
`.meta.json`'s `stop_reason`. Whether a datacentre address is refused here is
genuinely unmeasured for v0.1 (see README's "known limitations"), so "it
returned nothing" from a bare CI runner or a VPS is itself useful data, not
noise — say so explicitly rather than treating it as a normal negative
result.

Do not add anything that requires logging in, or that submits a form on the
site. This project only reads pages an anonymous visitor is served.

## Scope

This repo scrapes **public pages** on Givenchy Beauty: product listing
rankings, club squads, the latest-transfers listing and individual player
profiles, exactly as an anonymous visitor is served them. Out of scope:
anything behind a login, anything that submits a form, and anything that
defeats a protection rather than passing it the way an ordinary browser
does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
