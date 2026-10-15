"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three scrapers.

One kind of row
---------------
Givenchy Beauty is a catalogue, so this repo carries the family's ordinary
single `Product` dataclass rather than the two classes transfermarkt-scraper
needed for people and events. The first nine columns are the family prefix —
`source`, `scraped_at`, `url`, `sku`, `title`, `image_url`, `price`,
`currency`, `category` — so a consumer already written against another repo
in this family reads them unchanged.

`sku` is the VARIANT id (`P000476`), not the master/style id, because the
variant is what is actually unique per row: a category tile, the PDP's own
JSON-LD `sku`, and the tile's `data-pid` all agree on it, while one master id
(`F20100269`) covers up to nineteen shades of the same lipstick. The master
id is kept beside it in `master_id` — it is what the product URL carries, so
a consumer needs both to get from a row back to a page.

Columns that are NOT here, and the measurements that removed them
----------------------------------------------------------------
- `lowest_price_30d`: the EU Omnibus 30-day-low disclosure that EU
  retailers print beside a reduced price. Measured 2026-09-18 over 317 tiles in 20 captures spanning
  `us`, `gb`, `int/en`, `jp` and `ru`: zero occurrences of a second struck
  price of any kind. The one struck price found is a LIST price ABOVE its
  sale price (£134.00 against £105.20), which is what `original_price`
  holds. Add the column back with a measurement showing the disclosure, not
  by analogy with mediamarkt-scraper.
- `description`: the PDP's JSON-LD carries one (several hundred words of
  marketing copy). Deliberately not exported — it would dominate every CSV
  row of a price-monitoring run. `product_parser.parse_product` reads the
  JSON-LD whole, so a caller that wants it has it.
- `rating` / `review_count`: the tiles carry a Bazaarvoice placeholder
  (`pr-category-snippet`) that is EMPTY in server-rendered HTML — the widget
  fills it client-side. Measured 0 populated ratings across the same 317
  tiles. A column that is null on every row of every run should not exist.
"""

import csv
import json
import os
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# One platform, one hostname: every locale of Givenchy Beauty is a PATH
# prefix on www.givenchybeauty.com (`/us/`, `/gb/`, `/int/en/`), not a
# country TLD the way this family's MediaMarkt and Transfermarkt repos have.
# So `source` is constant here and the locale lives in its own column.
SOURCE_DEFAULT = "givenchybeauty.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None          # variant id, e.g. "P000476" — see module docstring
    title: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[float] = None
    # Never defaulted. `int/en` and `ru` are showcase locales that print no
    # price at all, and their tiles still carry `data-currency="USD"` — the
    # platform's fallback, not a fact about the page (measured 2026-09-18 on
    # 17 `int/en` and 9 `ru` tiles, every one of them priceless). Reading
    # that attribute would put a currency on a row that has no price.
    currency: Optional[str] = None
    category: Optional[str] = None     # the listing this row came from
    # Where `price` and `currency` were read:
    #   "tile-microdata" — the tile's own `span.value[itemprop=price][content]`
    #                      plus `meta[itemprop=priceCurrency]`. 284 of 317
    #                      tiles measured 2026-09-18.
    #   "tile-text"      — a gift-set tile that renders a bare "£82.00" with
    #                      no microdata at all. 7 of 317, all `gb` sets.
    #                      Currency then comes from the symbol, which
    #                      is a guess, not a fact.
    #   "jsonld"         — a product page's JSON-LD `offers`. Authoritative:
    #                      `offers.priceCurrency` is a fact the DOM may not
    #                      overwrite.
    #   "pdp-text"       — a product page with no JSON-LD, read from
    #                      `span.js-price-sales`.
    # There is no "jsonld+dom" overlay value: the two sources were measured
    # against each other on 2026-09-18 and AGREE (JSON-LD 50.00 USD against
    # the same product's tile price $50.00), so a tile-price
    # overlay is deliberately NOT built. See README, "Prices".
    price_source: Optional[str] = None

    # ---- Givenchy-specific, appended so the family prefix above is stable ----
    locale: Optional[str] = None       # "us", "gb", "int/en", ... — a path prefix
    master_id: Optional[str] = None    # style id, the one the product URL carries
    brand: Optional[str] = None
    subtitle: Optional[str] = None     # the tile's second line / product type
    shade: Optional[str] = None        # variant name, e.g. "PINK-204"
    shade_count: Optional[int] = None  # swatches shown + the "+ 16 more" overflow
    # The shade names the tile actually draws, e.g. ["NUDE-1", "NUDE-5",
    # "PINK-204"]. Not the full range: a tile renders three swatches and
    # says "+ 16 more colour available", and the other sixteen are not in
    # the listing HTML at all -- which is why `shade_count` (19 here) and
    # this list (3) legitimately disagree, and why the count is kept
    # separately rather than derived from `len()`.
    shades: Optional[List[str]] = None
    size: Optional[str] = None         # "100 ml", product pages only
    availability: Optional[str] = None # "InStock" / "OutOfStock"
    original_price: Optional[float] = None  # struck LIST price, always above `price`
    discount_pct: Optional[float] = None    # computed from the two, never read from the badge
    badge: Optional[str] = None        # "New", "Exclusive", ...
    page: Optional[int] = None
    row_index: Optional[int] = None


# Row class by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {
    "category": Product,
    "product": Product,
}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both qualify, and deduping is not theoretical
# here: one `gb` listing served the same tile twice (`F20100141`, 25 tiles
# over 24 distinct ids, measured 2026-09-18).
UNIQUE_BY_SKU_MODES = ("category", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list (`shades`). Joining with " | " keeps the cell
# readable in a spreadsheet and round-trippable by splitting on the same
# separator; the JSON output keeps the real list.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def _atomic_write(path: str, write: "Any") -> None:
    """Write through a temp file in the same directory, then `os.replace()`.

    Opening the destination directly means a crash, a full disk or a killed
    process truncates the PREVIOUS good file — which is the one thing this
    module exists to protect (see `save`: a run that finds nothing writes
    nothing, precisely so last night's output survives). An external audit
    pointed out that the writers themselves did not honour that.

    `os.replace` is atomic within a filesystem, and the temp file is created
    beside the target so it is always the same filesystem.

    File mode: `mkstemp` creates the temp file 0600 and a rename keeps it, so
    without the chmod below every output came out readable by its owner
    alone, whatever the umask (measured 2026-10-14: 0600 under umask 022,
    002 and 077, and an existing 0664 file narrowed to 0600). An existing
    target keeps its own mode — someone may have set it on purpose — and a
    new one gets what a plain `open(path, "w")` would have given it,
    `0o666 & ~umask`. Never a flat 0644: that would narrow 0664 under
    umask 002.
    """
    import tempfile
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                               suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    _atomic_write(path, lambda f: json.dump([asdict(r) for r in rows], f,
                                            ensure_ascii=False, indent=2))


# A spreadsheet treats a cell beginning with one of these as a FORMULA, not
# as text, and the strings in these rows (titles, subtitles, shade names,
# badges) were written by whoever edits the storefront, not by this code.
# `=HYPERLINK(...)`, `+cmd|...` and `@SUM(...)` are the classic shapes; a
# leading tab, CR or LF is included because some spreadsheets strip it and
# then evaluate what follows.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")

# Prefixed to such a cell. Excel, Google Sheets and LibreOffice all read a
# leading apostrophe as "the rest of this cell is text".
CSV_FORMULA_ESCAPE = "'"


def _csv_escape(v: Any):
    """Neutralise a formula-shaped cell. Returns `(value, was_escaped)`.

    Only STRINGS are touched: escaping a number would turn `-5` into the text
    `'-5` and break every sum a consumer writes over the column.

    CSV only. The JSON output keeps the site's text exactly as served, so the
    two files deliberately differ, and `csv_cells_escaped` in the sidecar
    declares by how much.
    """
    if isinstance(v, str) and v.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + v, True
    return v, False


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> int:
    """Write the CSV and return how many cells were formula-escaped."""
    # An empty result still gets the header row, from `row_cls` rather than
    # the first row, so a mode that finds nothing still writes the columns
    # that mode would have used.
    fieldnames = [f.name for f in fields(row_cls)]
    escaped = [0]

    def _write(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = {}
            for k, v in asdict(r).items():
                # Escaped AFTER `_csv_value`: a list joined into one cell is
                # text too, and its first element can be formula-shaped while
                # the list itself is not a string.
                value, was_escaped = _csv_escape(_csv_value(v))
                row[k] = value
                escaped[0] += was_escaped
            writer.writerow(row)

    _atomic_write(path, _write)
    return escaped[0]


# Exit code used when a run completes but produced nothing.
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early.
EXIT_PARTIAL = 6

# Exit code for "the content was never obtained": a run holding NO rows whose
# page never arrived -- a navigation timeout, a dead or unauthenticated proxy
# -- or one of this project's own 2Captcha-product calls failing (the
# Fingerprint API rejecting a request, a Scraping Browser CDP connection
# failing with e.g. profile_locked, the Scraper API answering an error).
# Distinct from EXIT_BLOCKED (the target site refusing a page), from
# EXIT_NO_PRODUCTS (a served page that genuinely lists nothing) and from an
# uncaught crash (1). A run that gathered rows and THEN failed is still
# EXIT_PARTIAL. Deliberately NOT what a captcha-solve failure gets: a solver
# error is a WARNING that lets the run continue, and the run then reports
# whatever the page turned out to be.
EXIT_REMOTE_API_ERROR = 5


class RemoteAPIError(RuntimeError):
    """A 2Captcha product call (Fingerprint API, Scraping Browser CDP
    connect) failed on its own terms, not the target site blocking a page.

    Raised by fingerprint_client.get_fingerprint and by each engine's
    --cdp-endpoint connect path; caught once at each engine's entry point
    and mapped to EXIT_REMOTE_API_ERROR, so the three engines cannot drift
    on which of 1 (crash) / 3 (blocked) / 5 (remote API error) a given
    failure gets -- before this, both paths raised a bare RuntimeError with
    no engine catching it, so either failure reached the interpreter as an
    unhandled exception and exited 1 (a raw traceback, no run-metadata
    sidecar) regardless of which one it actually was.
    """


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path."""
    path = f"{out_prefix}.meta.json"
    _atomic_write(path, lambda f: json.dump(meta, f, ensure_ascii=False, indent=2))
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def write_attempt_meta(out_prefix: str, meta: dict) -> str:
    """Write the per-attempt record. Always written, even by a run that
    produced no data — see `finish_run` for why this is a second file rather
    than a change to the first."""
    path = f"{out_prefix}.attempt.json"
    _atomic_write(path, lambda f: json.dump(meta, f, ensure_ascii=False, indent=2))
    print(f"[+] Wrote attempt record -> {path} "
          f"(attempt_status={meta.get('attempt_status')}, "
          f"data_updated={meta.get('data_updated')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "category", source: str = SOURCE_DEFAULT) -> dict:
    """Build the metadata dict for a finished run. See mediamarkt-scraper's
    output_writer.py for the full rationale; unchanged here."""
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product,
         stats: Optional[dict] = None) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero rows, nothing is written at all unless `allow_empty`: a run that finds nothing must not
    silently replace yesterday's good output with an empty file.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        escaped = write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
        # Through `stats` rather than the return value, because the return
        # value IS the exit code. The caller puts it in the sidecar.
        if stats is not None:
            stats["csv_cells_escaped"] = escaped
        if escaped:
            print(f"[!] {escaped} CSV cell(s) began with a formula character "
                  f"and were prefixed with {CSV_FORMULA_ESCAPE!r} so a "
                  f"spreadsheet reads them as text. The JSON output is "
                  f"unchanged — see csv_cells_escaped in the sidecar.")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "category", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them. See mediamarkt-scraper's output_writer.py for
    the full rationale; the logic here is unchanged.
    """
    # Decided by the reason AND the evidence: a run whose loop ended for a
    # complete reason while some page failed is not complete.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    write_stats: dict = {}
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls,
              stats=write_stats)
    wrote_output = bool(rows) or allow_empty

    # `<out>.attempt.json` is written on EVERY run, including one that wrote
    # no data. `<out>.meta.json` keeps its old meaning — it describes the
    # dataset currently on disk — and that combination is deliberate:
    #
    # A failed run writes no sidecar, because a "failed" meta.json
    # sitting beside yesterday's good data would contradict it. True, but an
    # external audit showed the other half of the hazard: after a blocked
    # run, `meta.json` still said `status=complete, products=1` from the
    # PREVIOUS run, so a consumer reading files rather than exit codes reads
    # yesterday's success as today's. Both are real; one file cannot answer
    # both questions.
    #
    # So: meta.json answers "what is this data?", attempt.json answers "what
    # happened just now?". A pipeline that wants freshness reads
    # attempt.json and compares `data_updated`.
    write_attempt_meta(out_prefix, {
        "attempt_status": "complete" if (rows and complete) else (
            "partial" if rows else "failed"),
        "stop_reason": stop_reason,
        "blocked": bool(blocked),
        "data_updated": bool(wrote_output),
        "products": len(rows),
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "mode": mode,
        "source": source,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        meta = run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows))
        # The CSV/JSON divergence is a fact about THIS run's output. Merged
        # UNDER the caller's `extra`, so a key the caller supplies is never
        # silently replaced by this housekeeping one.
        merged = {**write_stats, **(extra or {})}
        for k, v in merged.items():
            meta.setdefault(k, v)
        write_run_meta(out_prefix, meta)

    if not rows:
        # Nothing gathered, and WHY decides the code:
        #   blocked            something stood between the run and the page
        #   did not complete   the page was never obtained -- a load timeout,
        #                      a dead proxy, a remote API failing
        #   completed          the page was served and genuinely lists nothing
        # Keyed on `not complete` rather than on a list of failure reasons,
        # because a list cannot cover a reason nobody has added to it yet,
        # and every such reason would fall through to "the catalogue is
        # empty".
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_REMOTE_API_ERROR}, NOT an "
                  f"empty result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the site from this run.")
            return EXIT_REMOTE_API_ERROR
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
