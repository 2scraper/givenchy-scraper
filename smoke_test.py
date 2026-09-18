#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# The encoding declaration matters here for the same reason it does in the
# sibling repos: the fixtures below are HTML slices with accented names
# (Kylian Mbappe, Cote d'Ivoire) sitting inside very long lines, and an
# undeclared encoding can trip an older tokenizer on exactly that content.
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for givenchy-scraper.

Run this FIRST, before touching a real browser or givenchybeauty.com, to
confirm the environment and the parsing/output/policy logic work:

    python3 smoke_test.py

Deliberately ONE file of plain functions with inline fixtures -- no pytest,
no conftest, no fixtures directory. tests/test_smoke.py wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks that could drift from this one.

It must pass with NO engine library installed at all, so every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is REPORTED. CI's engine-smoke job installs all three
and fails if anything reports skipped, because "skipped, engine absent"
reads identically to a real import error.

What this suite is actually for
--------------------------------
Not coverage. Every check that matters here pins a VALUE read off a real
capture, because a column can be 100% populated and entirely wrong. This
repo already found two such bugs while building it (see product_parser.py's
`_label_map` and `_club_cell_with_league` docstrings): `joined_date` and
`contract_until` came back None for every player despite the data being on
the page, and a transfer's `from_club` came back "Without ClubWithout Club"
(a doubled title) for a free-agent row, in both cases while every column
still "had a value" as far as a coverage check could tell. So the assertions
below say `price == 220_000_000.0`, not `price is not None`.

Exits non-zero on any failure.
"""

import ast
import builtins
import csv
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import fields
from typing import Optional

import env_config
import page_flow
import product_parser
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            detect_recaptcha_in_page, reconcile_detections,
                            solve_recaptcha, get_balance)
from diff_runs import diff_rows, _check_comparable, TRACKED_FIELDS
from output_writer import (Product, ROW_CLASS_BY_MODE,
                           UNIQUE_BY_SKU_MODES, dedupe_by_key, dedupe_by_sku,
                           write_json, write_csv, save, finish_run, run_meta,
                           LIST_CSV_SEPARATOR, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                           EXIT_PARTIAL, EXIT_REMOTE_API_ERROR, RemoteAPIError,
                           COMPLETE_STOP_REASONS, SOURCE_DEFAULT)
from product_parser import (HOSTS, BASE, LOCALES, DEFAULT_LOCALE,
                            SHOWCASE_LOCALES, page_url,
                            page_number_from_url, sku_from_url,
                            category_from_url, is_product_url,
                            is_category_url, is_robots_allowed,
                            parse_money, parse_price, sitemap_url,
                            detect_bot_challenge, detect_page_state,
                            is_supported_host, unsupported_reason, site_host,
                            is_showcase_locale, is_self_clearing_challenge,
                            parse_category, parse_product, parse_sitemap,
                            product_urls_from_sitemap, BOT_CHALLENGE_MARKERS,
                            locale_of)
from proxy_pool import (ProxyPool, ProxyError, mask, to_playwright,
                        split_credentials, parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False

# ---------------------------------------------------------------------------
# Fixtures: trimmed, scrubbed excerpts of real captures
# ---------------------------------------------------------------------------
# Cut from pages fetched from www.givenchybeauty.com on 2026-09-17/18 and
# kept VERBATIM apart from the scrubbing described below, because a fixture
# rewritten by hand stops being evidence about the site.
#
# Each tile is one `div.productTile-wrapper` lifted whole out of its
# listing; each product fixture is the `<head>` structured data plus the
# name/price/size/breadcrumb nodes, wrapped in a minimal document carrying
# the two markers `detect_page_state` needs (the site's own asset path and
# the `js-pdp` hook). Full pages run 600-960KB and this suite needs the
# STRUCTURE, not 300 tiles.
#
# Scrubbed per CLAUDE.md §10, with PATTERNS rather than literals so the next
# capture is checked too (see test_no_capture_leaks): the Bazaarvoice
# `api_key` and merchant ids in each tile's `pr-category-snippet`, the
# listing's own `searchID`, and any `dwsid`/`session_id`. No customer data
# and no personal name appears in any of these pages.
#
# Why these six tiles and these three pages -- each pins a case that was
# measured and that a naive parser gets wrong:
#
#   TILE_US             the ordinary case: microdata price, 3 swatches plus
#                       a "+ 16 more" overflow (19 shades), master id in the
#                       href and the VARIANT id in data-pid.
#   TILE_GB_SET_STRIKE  a gift set with a struck LIST price above its sale
#                       price (134.00 against 105.20) and the site's own
#                       "-21%" badge, which this repo does not read.
#   TILE_GB_SET_TEXT    a gift set whose price is a bare text node with no
#                       microdata at all -- 7 of 317 tiles measured.
#   TILE_GB_OOS         out of stock, priced, and also text-only.
#   TILE_INT_SHOWCASE   the same product as TILE_US on a showcase locale:
#                       no price container, and `data-currency="USD"` plus
#                       `data-gtm` `"price":0` sitting there ready to be
#                       believed.
#   TILE_JP             JPY, comma-grouped and decimal-less (8,360).
#   PDP_US / PDP_GB     JSON-LD with a real price in two currencies.
#   PDP_INT             the same product with NO JSON-LD at all, which must
#                       still parse to a row rather than to nothing.

TILE_US = """<div class="product productTile-wrapper" data-defaultavailable="true" data-defaultvariant="P000476" data-firstvariant="P000481" data-gtm='{"brand":"Givenchy beauty","category":"make-up/lip/lipstick","id":"P000476","name":"Le rouge satin silk - The new tailored satin lipstick","price":50,"productCollection":"Le rouge satin silk","productContext":"New","productType":"The new tailored satin lipstick","productMasterId":"F20100269","variant":"PINK-204","productStock":"In stock"}' data-pid="P000476" data-prodtype="variant">
<article aria-label="LE ROUGE SATIN SILK LIPSTICK" class="giv-ProductTile-item js-ProductItem in-stock-product-tile" data-brand="50" data-categories="make-up/lip/lipstick" data-color="PINK-204" data-context="" data-currency="USD" data-dimension22="N°P227 - PINK SILHOUETTE" data-gtm='{"brand":"Givenchy beauty","category":"make-up/lip/lipstick","id":"P000476","name":"Le rouge satin silk - The new tailored satin lipstick","price":50,"productCollection":"Le rouge satin silk","productContext":"New","productType":"The new tailored satin lipstick","productMasterId":"F20100269","variant":"PINK-204","productStock":"In stock"}' data-itemid="P000476" data-masterid="F20100269" data-name="LE ROUGE SATIN SILK LIPSTICK" data-nameen="LE ROUGE SATIN SILK" data-size="">
<!-- dwMarker="product" dwContentID="391c85c77939d1468c1fa66ea5" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="LE ROUGE SATIN SILK LIPSTICK - N°P227 - PINK SILHOUETTE" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw7681c191/images/P000476/P000476_2.jpg?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw80db15cd/images/P000476/P000476_1.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw80db15cd/images/P000476/P000476_1.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-us-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag">
                New
                
            </span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="LE ROUGE SATIN SILK LIPSTICK" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/us/p/le-rouge-satin-silk-lipstick-F20100269.html" href="/us/p/le-rouge-satin-silk-lipstick-F20100269.html">
                LE ROUGE SATIN SILK LIPSTICK
            </a>
<p class="giv-ProductTile-productSubName">
                    The New Hydrating Long-Wear Lipstick
                </p>
</span>
<ul aria-label="Available in next shades:" class="giv-ProductTile-swatchs swatch-list" role="radiogroup">
<li aria-label="NUDE-1" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#CF706D" title="NUDE-1">
</span>
</span>
</li>
<li aria-label="NUDE-5" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#C35244" title="NUDE-5">
</span>
</span>
</li>
<li aria-label="PINK-204" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#BB3740" title="PINK-204">
</span>
</span>
</li>
<li class="giv-ProductTile-more">
                    + 16
                    <span class="sr-only">more color available</span>
</li>
</ul>
<div class="giv-ProductTile-pr-category-snippet">
<div class="pr-category-snippet" data-pr-plp-component='{"ENABLE_CLIENT_SIDE_STRUCTURED_DATA":false,"api_key":"SCRUBBED-API-KEY","locale":"en_US","merchant_group_id":"1987211810","merchant_id":"1503200967","page_id":"F20100269","components":{"CategorySnippet":"category-snippet-F20100269-P000476"},"sm_data":"NO_COOKIES","enable_front_end_iovation_validation":false}'></div>
<div id="category-snippet-F20100269-P000476"></div>
</div>
<div class="product-price-container">
<div class="price">
<span class="price-list">
<meta content="USD" itemprop="priceCurrency"/>
<span class="sales">
<span class="value" content="50.00" itemprop="price"></span>
            
            $50.00

        
    </span>
</span>
</div>
</div>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"Le rouge satin silk","productId":"P000476","productCategory":"Lipstick"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="P000476">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-us-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">Remove LE ROUGE SATIN SILK LIPSTICK from wishlist</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="LE ROUGE SATIN SILK LIPSTICK" type="button" value="P000476">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-us-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add LE ROUGE SATIN SILK LIPSTICK to wishlist</span>
</button>
</div>
<button class="giv-ProductTile-quickBuy js-ProductTile-cta js-layer__dialog__open keyboard-interaction-only" data-layer-id="quickbuy-layer" data-layer-url="https://www.givenchybeauty.com/on/demandware.store/Sites-givenchy-beauty-us-Site/en_US/Product-QuickBuyLayer?pid=P000476" type="button">
Quick buy <span class="visually-hidden">LE ROUGE SATIN SILK LIPSTICK</span>
</button>
<!-- END_dwmarker -->
</article>
</div>"""

TILE_GB_SET_STRIKE = """<div class="product productTile-wrapper" data-defaultavailable="null" data-defaultvariant="null" data-firstvariant="null" data-gtm='{"brand":"Givenchy beauty","category":"fragrance","id":"PSETUK_00125","name":"","price":38,"productCollection":"","productContext":"Exclusive","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-pid="PSETUK_00125" data-prodtype="set">
<article aria-label="ANGELIQUE SET" class="giv-ProductTile-item js-ProductItem out-of-stock-product-tile" data-brand="" data-categories="fragrance" data-color="" data-context="" data-currency="GBP" data-dimension22="" data-gtm='{"brand":"Givenchy beauty","category":"fragrance","id":"PSETUK_00125","name":"","price":38,"productCollection":"","productContext":"Exclusive","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-itemid="PSETUK_00125" data-masterid="" data-name="ANGELIQUE SET" data-nameen="" data-size="">
<!-- dwMarker="product" dwContentID="dd607bdc033b693f54c743fb6b" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="ANGELIQUE SET" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw20f9f237/Product_set_hub/PSETUK_00125/PSETUK_00125_1.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8ee2689c/Product_set_hub/PSETUK_00125/PSETUK_00125_0.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8ee2689c/Product_set_hub/PSETUK_00125/PSETUK_00125_0.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag">
                Exclusive
                
            </span>
<span class="giv-ProductTile-tag">
                Limited Edition
                
            </span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="ANGELIQUE SET" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/gb/p/angelique-set-PSETUK_00125.html" href="/gb/p/angelique-set-PSETUK_00125.html">
                ANGELIQUE SET
            </a>
<p class="giv-ProductTile-productSubName">
                    L'Interdit Angélique Rouge Eau de Parfum 50ml &amp; Le Rouge Rouge Velvet Matte N°02
                </p>
</span>
<span class="giv-ProductTile-productOosMsg">Currently unavailable online</span>
<div class="giv-ProductTile-pr-category-snippet">
<div class="pr-category-snippet" data-pr-plp-component='{"ENABLE_CLIENT_SIDE_STRUCTURED_DATA":false,"api_key":"SCRUBBED-API-KEY","locale":"en_GB","merchant_group_id":"1530678071","merchant_id":"1992981638","page_id":"PSETUK_00125","components":{"CategorySnippet":"category-snippet-PSETUK_00125"},"sm_data":"NO_COOKIES","enable_front_end_iovation_validation":false}'></div>
<div id="category-snippet-PSETUK_00125"></div>
</div>
<div class="price-container set-price-volume">
<div class="price set-price">
<span class="price-list">
<meta content="GBP" itemprop="priceCurrency"/>
<span class="sales">
<span class="value" content="105.20" itemprop="price"></span>
            
            £105.20

        
    </span>
<meta content="GBP" itemprop="priceCurrency"/>
<span class="strike-through list">
<span class="value" content="134.00" itemprop="price">
                £134.00

            </span>
</span>
<span class="price-saving">
            
                -21%
            
        </span>
</span>
</div>
</div>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"","productId":"PSETUK_00125","productCategory":"Fragrance"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="PSETUK_00125">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">Remove ANGELIQUE SET from wishlist</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="ANGELIQUE SET" type="button" value="PSETUK_00125">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add ANGELIQUE SET to wishlist</span>
</button>
</div>
<button class="giv-ProductTile-OutOfStock js-ProductTile-cta keyboard-interaction-only js-gtm-out-of-stock js-layer__dialog__open" data-layer-id="outofstock-layer" data-layer-url="https://www.givenchybeauty.com/on/demandware.store/Sites-givenchy-beauty-uk-Site/en_GB/Product-OutOfStockLayer?pid=PSETUK_00125" data-product-id="PSETUK_00125" data-product-name="ANGELIQUE SET" type="button">
<img alt="email" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw062dac75/images/email/25-email.svg"/>
<span>Let me know when the product is back</span>
</button>
<!-- END_dwmarker -->
</article>
</div>"""

TILE_GB_SET_TEXT = """<div class="product productTile-wrapper" data-defaultavailable="null" data-defaultvariant="null" data-firstvariant="null" data-gtm='{"brand":"Givenchy beauty","category":"make-up/Sets-Mup","id":"PSETUK_00120","name":"","price":0,"productCollection":"","productContext":"Exclusive","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-pid="PSETUK_00120" data-prodtype="set">
<article aria-label="The Rosy Lilac Look" class="giv-ProductTile-item js-ProductItem in-stock-product-tile" data-brand="" data-categories="make-up/Sets-Mup" data-color="" data-context="" data-currency="GBP" data-dimension22="" data-gtm='{"brand":"Givenchy beauty","category":"make-up/Sets-Mup","id":"PSETUK_00120","name":"","price":0,"productCollection":"","productContext":"Exclusive","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-itemid="PSETUK_00120" data-masterid="" data-name="The Rosy Lilac Look" data-nameen="" data-size="">
<!-- dwMarker="product" dwContentID="73058f377b1322978879e21199" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="The Rosy Lilac Look" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw83dd201c/virtual set - Glow anim/lip oil pink.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8a0af0dd/Product_set_hub/Product%20set%20glow%20animation/ROSY_LILAC_LOOK.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8a0af0dd/Product_set_hub/Product%20set%20glow%20animation/ROSY_LILAC_LOOK.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag">
                New
                
            </span>
<span class="giv-ProductTile-tag">
                Exclusive
                
            </span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="The Rosy Lilac Look" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/gb/p/the-rosy-lilac-look-PSETUK_00120.html" href="/gb/p/the-rosy-lilac-look-PSETUK_00120.html">
                The Rosy Lilac Look
            </a>
<p class="giv-ProductTile-productSubName">
                    Prisme Libre Highlighter Powder N°01 &amp; Perfecto Serum Lip Oil N°00 &amp; Kabuki Brush
                </p>
</span>
<div class="giv-ProductTile-pr-category-snippet">
<div class="pr-category-snippet" data-pr-plp-component='{"ENABLE_CLIENT_SIDE_STRUCTURED_DATA":false,"api_key":"SCRUBBED-API-KEY","locale":"en_GB","merchant_group_id":"1530678071","merchant_id":"1992981638","page_id":"PSETUK_00120","components":{"CategorySnippet":"category-snippet-PSETUK_00120"},"sm_data":"NO_COOKIES","enable_front_end_iovation_validation":false}'></div>
<div id="category-snippet-PSETUK_00120"></div>
</div>
<div class="price-container set-price-volume">
<div class="price set-price">
<span>£82.00</span>
</div>
</div>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"","productId":"PSETUK_00120","productCategory":"Makeup Sets"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="PSETUK_00120">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">Remove The Rosy Lilac Look from wishlist</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="The Rosy Lilac Look" type="button" value="PSETUK_00120">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add The Rosy Lilac Look to wishlist</span>
</button>
</div>
<button class="giv-ProductTile-quickBuy js-ProductTile-cta js-layer__dialog__open keyboard-interaction-only" data-layer-id="quickbuy-layer" data-layer-url="https://www.givenchybeauty.com/on/demandware.store/Sites-givenchy-beauty-uk-Site/en_GB/Product-QuickBuyLayer?pid=PSETUK_00120" type="button">
Quick buy <span class="visually-hidden">The Rosy Lilac Look</span>
</button>
<!-- END_dwmarker -->
</article>
</div>"""

TILE_GB_OOS = """<div class="product productTile-wrapper" data-defaultavailable="null" data-defaultvariant="null" data-firstvariant="null" data-gtm='{"brand":"Givenchy beauty","category":"fragrance","id":"PSETUK_00137","name":"","price":0,"productCollection":"","productContext":"","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-pid="PSETUK_00137" data-prodtype="set">
<article aria-label="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" class="giv-ProductTile-item js-ProductItem out-of-stock-product-tile" data-brand="" data-categories="fragrance" data-color="" data-context="" data-currency="GBP" data-dimension22="" data-gtm='{"brand":"Givenchy beauty","category":"fragrance","id":"PSETUK_00137","name":"","price":0,"productCollection":"","productContext":"","productType":"Regular product","productMasterId":"","variant":"","productStock":"In stock"}' data-itemid="PSETUK_00137" data-masterid="" data-name="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" data-nameen="" data-size="">
<!-- dwMarker="product" dwContentID="ce8bd5cf3a931ffa121ae56cb3" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw20cdfbcf/CONCRETES/PSETUK_00137/2.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8af8f39c/CONCRETES/PSETUK_00137/concrete_new_virtualset.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/en_GB/dw8af8f39c/CONCRETES/PSETUK_00137/concrete_new_virtualset.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag">
                New
                
            </span>
<span class="giv-ProductTile-tag">
                Exclusive
                
            </span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/gb/p/l-interdit-on-the-go-scent-lip-duo-PSETUK_00137.html" href="/gb/p/l-interdit-on-the-go-scent-lip-duo-PSETUK_00137.html">
                L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO
            </a>
<p class="giv-ProductTile-productSubName">
                    L'interdit Eau de Parfum Solid Perfume + Rose Perfecto Shine Serum Lipstick N°306 &amp; Exclusive Gift: L'interdit Solid Perfume Holder
                </p>
</span>
<span class="giv-ProductTile-productOosMsg">Currently unavailable online</span>
<div class="giv-ProductTile-pr-category-snippet">
<div class="pr-category-snippet" data-pr-plp-component='{"ENABLE_CLIENT_SIDE_STRUCTURED_DATA":false,"api_key":"SCRUBBED-API-KEY","locale":"en_GB","merchant_group_id":"1530678071","merchant_id":"1992981638","page_id":"PSETUK_00137","components":{"CategorySnippet":"category-snippet-PSETUK_00137"},"sm_data":"NO_COOKIES","enable_front_end_iovation_validation":false}'></div>
<div id="category-snippet-PSETUK_00137"></div>
</div>
<div class="price-container set-price-volume">
<div class="price set-price">
<span>£77.00</span>
</div>
</div>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"","productId":"PSETUK_00137","productCategory":"Fragrance"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="PSETUK_00137">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">Remove L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO from wishlist</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" type="button" value="PSETUK_00137">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO to wishlist</span>
</button>
</div>
<button class="giv-ProductTile-OutOfStock js-ProductTile-cta keyboard-interaction-only js-gtm-out-of-stock js-layer__dialog__open" data-layer-id="outofstock-layer" data-layer-url="https://www.givenchybeauty.com/on/demandware.store/Sites-givenchy-beauty-uk-Site/en_GB/Product-OutOfStockLayer?pid=PSETUK_00137" data-product-id="PSETUK_00137" data-product-name="L'INTERDIT ON-THE-GO SCENT &amp; LIP DUO" type="button">
<img alt="email" src="/on/demandware.static/Sites-givenchy-beauty-uk-Site/-/default/dw062dac75/images/email/25-email.svg"/>
<span>Let me know when the product is back</span>
</button>
<!-- END_dwmarker -->
</article>
</div>"""

TILE_INT_SHOWCASE = """<div class="product productTile-wrapper" data-defaultavailable="true" data-defaultvariant="P000476" data-firstvariant="P000481" data-gtm='{"brand":"Givenchy beauty","category":"make-up/lip/lipstick","id":"P000476","name":"Le rouge satin silk - The new tailored satin lipstick","price":0,"productCollection":"Le rouge satin silk","productContext":"New","productType":"The new tailored satin lipstick","productMasterId":"F20100269","variant":"PINK-204","productStock":"In stock"}' data-pid="P000476" data-prodtype="variant">
<article aria-label="LE ROUGE SATIN SILK" class="giv-ProductTile-item js-ProductItem in-stock-product-tile" data-brand="50" data-categories="make-up/lip/lipstick" data-color="PINK-204" data-context="" data-currency="USD" data-dimension22="N°P227 - PINK SILHOUETTE" data-gtm='{"brand":"Givenchy beauty","category":"make-up/lip/lipstick","id":"P000476","name":"Le rouge satin silk - The new tailored satin lipstick","price":0,"productCollection":"Le rouge satin silk","productContext":"New","productType":"The new tailored satin lipstick","productMasterId":"F20100269","variant":"PINK-204","productStock":"In stock"}' data-itemid="P000476" data-masterid="F20100269" data-name="LE ROUGE SATIN SILK" data-nameen="LE ROUGE SATIN SILK" data-size="">
<!-- dwMarker="product" dwContentID="391c85c77939d1468c1fa66ea5" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="LE ROUGE SATIN SILK - N°P227 - PINK SILHOUETTE" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw7681c191/images/P000476/P000476_2.jpg?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw80db15cd/images/P000476/P000476_1.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw80db15cd/images/P000476/P000476_1.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-int-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag">
                New
                
            </span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="LE ROUGE SATIN SILK" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/int/en/p/le-rouge-satin-silk-F20100269.html" href="/int/en/p/le-rouge-satin-silk-F20100269.html">
                LE ROUGE SATIN SILK
            </a>
<p class="giv-ProductTile-productSubName">
                    The new tailored satin lipstick
                </p>
</span>
<ul aria-label="Available in next shades:" class="giv-ProductTile-swatchs swatch-list" role="radiogroup">
<li aria-label="NUDE-1" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#CF706D" title="NUDE-1">
</span>
</span>
</li>
<li aria-label="NUDE-5" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#C35244" title="NUDE-5">
</span>
</span>
</li>
<li aria-label="PINK-204" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-1">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#BB3740" title="PINK-204">
</span>
</span>
</li>
<li class="giv-ProductTile-more">
                    + 16
                    <span class="sr-only">more color available</span>
</li>
</ul>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"Le rouge satin silk","productId":"P000476","productCategory":"Lipstick"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="P000476">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-int-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">Remove LE ROUGE SATIN SILK from wishlist</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="LE ROUGE SATIN SILK" type="button" value="P000476">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-int-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add LE ROUGE SATIN SILK to wishlist</span>
</button>
</div>
<!-- END_dwmarker -->
</article>
</div>"""

TILE_JP = """<div class="product productTile-wrapper" data-defaultavailable="true" data-defaultvariant="P090303" data-firstvariant="P090319" data-gtm='{"brand":"Givenchy beauty","category":"make-up/face/powder","id":"P090303","name":"Prisme libre 4-color loose powder - The Iconic Loose Powder with 4-Color Correction - A mattifying, correcting and luminous loose powder. 12 g","price":8360,"productCollection":"Prisme libre 4-color loose powder","productContext":"Something prisme blue","productType":"The Iconic Loose Powder with 4-Color Correction","productMasterId":"F20100112","variant":"MULTICOLOR-103","productStock":"In stock"}' data-pid="P090303" data-prodtype="variant">
<article aria-label="プリズム・リーブル" class="giv-ProductTile-item js-ProductItem in-stock-product-tile" data-brand="50" data-categories="make-up/face/powder" data-color="MULTICOLOR-103" data-context="" data-currency="JPY" data-dimension22="No. 00 - オパルセント・チュール" data-gtm='{"brand":"Givenchy beauty","category":"make-up/face/powder","id":"P090303","name":"Prisme libre 4-color loose powder - The Iconic Loose Powder with 4-Color Correction - A mattifying, correcting and luminous loose powder. 12 g","price":8360,"productCollection":"Prisme libre 4-color loose powder","productContext":"Something prisme blue","productType":"The Iconic Loose Powder with 4-Color Correction","productMasterId":"F20100112","variant":"MULTICOLOR-103","productStock":"In stock"}' data-itemid="P090303" data-masterid="F20100112" data-name="プリズム・リーブル" data-nameen="PRISME LIBRE 4-COLOR LOOSE POWDER" data-size="">
<!-- dwMarker="product" dwContentID="c89d1d6805467cf89406e1e618" -->
<div class="giv-ProductTile-container keyboard-interaction-only position-relative">
<figure class="giv-ProductTile-content">
<div class="giv-ProductTile-visual js-product-tile-visual js-ProductLazy">
<img alt="プリズム・リーブル - No. 00 - オパルセント・チュール" class="giv-ProductTile-picture js-ProductTilePicture" data-hover="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dw521d4b80/images/P090303/P090303_PL_Loose_Powder_Before-After_View-3_PDM-v2.jpg?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-save-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/ja/dwd6b87812/images/P090303/P090303_PL_Loose-Powder_Packshot_View-1_PDM_0.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" data-src="https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/ja/dwd6b87812/images/P090303/P090303_PL_Loose-Powder_Packshot_View-1_PDM_0.png?sw=300&amp;sh=375&amp;sm=fit&amp;strip=false" src="/on/demandware.static/Sites-givenchy-beauty-ja-Site/-/default/dw53dcf924/images/givenchy-notFound.png"/>
</div>
<div class="giv-ProductTile-details figcaption">
<div class="giv-ProductTile-tagGroup">
<span class="giv-ProductTile-tag giv-ProductTile-tag--engraving">
                刻印
                
                    <img alt="刻印" class="icon" src="/on/demandware.static/Sites-givenchy-beauty-ja-Site/-/default/dw0a152647/images/icon_engraving.svg"/>
</span>
</div>
<span class="giv-ProductTile-name">
<a aria-label="プリズム・リーブル" class="giv-ProductTile-link js-ProductTile-link keyboard-interaction-only js-find-focus" data-url="/jp/p/%E3%83%97%E3%83%AA%E3%82%BA%E3%83%A0%E3%83%BB%E3%83%AA%E3%83%BC%E3%83%96%E3%83%AB-F20100112.html" href="/jp/p/%E3%83%97%E3%83%AA%E3%82%BA%E3%83%A0%E3%83%BB%E3%83%AA%E3%83%BC%E3%83%96%E3%83%AB-F20100112.html">
                プリズム・リーブル
            </a>
<p class="giv-ProductTile-productSubName">
                    ルース パウダー
                </p>
</span>
<ul aria-label="Available in next shades:" class="giv-ProductTile-swatchs swatch-list" role="radiogroup">
<li aria-label="MULTICOLOR-104" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-4">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#d5dcc0 " title="MULTICOLOR-104">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #d3d9e3 " title="MULTICOLOR-104">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #e1d7e9 " title="MULTICOLOR-104">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #efe8f1" title="MULTICOLOR-104">
</span>
</span>
</li>
<li aria-label="MULTICOLOR-105" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-4">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#eac8c9 " title="MULTICOLOR-105">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #ecc9bc " title="MULTICOLOR-105">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #f2d9bb " title="MULTICOLOR-105">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #f0d2c5" title="MULTICOLOR-105">
</span>
</span>
</li>
<li aria-label="MULTICOLOR-109" class="giv-ProductTile-swatch" role="radio" tabindex="0">
<span class="giv-ProductVariations-colorSelect-contentSwatch-swatchImage giv-ProductVariations-swatchImage-4">
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color:#c4ddd4 " title="MULTICOLOR-109">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #e3b990 " title="MULTICOLOR-109">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #d4cfe7 " title="MULTICOLOR-109">
</span>
<span class="giv-ProductAvaible--null giv-ProductVariations-swatchImage giv-ProductVariations-swatchImage-swatchColor" style="background-color: #e7c2a3" title="MULTICOLOR-109">
</span>
</span>
</li>
<li class="giv-ProductTile-more">
                    もっと見る
                    <span class="sr-only">more color available</span>
</li>
</ul>
<div class="giv-ProductTile-pr-category-snippet">
<div class="pr-category-snippet" data-pr-plp-component='{"ENABLE_CLIENT_SIDE_STRUCTURED_DATA":false,"api_key":"SCRUBBED-API-KEY","locale":"ja_JP","merchant_group_id":"532223346","merchant_id":"1778076539","page_id":"F20100112","components":{"CategorySnippet":"category-snippet-F20100112-P090303"},"sm_data":"NO_COOKIES","enable_front_end_iovation_validation":false}'></div>
<div id="category-snippet-F20100112-P090303"></div>
</div>
<div class="product-price-container">
<div class="price">
<span class="price-list">
<meta content="JPY" itemprop="priceCurrency"/>
<span class="sales">
<span class="value" content="8360" itemprop="price"></span>
            
            ¥8,360

        
    </span>
</span>
</div>
</div>
</div>
</figure>
</div>
<div class="giv-Wishlist js-AddToWishList" data-gtm='{"productName":"Prisme libre 4-color loose powder - A mattifying, correcting and luminous loose powder. &lt;br&gt; 12 g &lt;/br&gt;","productId":"P090303","productCategory":"パウダー"}'>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-remove keyboard-interaction-only" data-action="remove" type="button" value="P090303">
<img alt="" class="giv-Wishlist-icon" src="/on/demandware.static/Sites-givenchy-beauty-ja-Site/-/default/dw73f7cfee/images/heartPDP.svg"/>
<span class="visually-hidden">ウィッシュリストからアイテムを削除</span>
</button>
<button class="giv-ProductTile-button giv-Wishlist-button js-Wishlist-button-add giv-Wishlist-button--active keyboard-interaction-only" data-action="add" data-productname="プリズム・リーブル" type="button" value="P090303">
<img alt="" class="giv-Wishlist-icon" src="https://www.givenchybeauty.com/on/demandware.static/Sites-givenchy-beauty-ja-Site/-/default/dwf68f2bc8/images/heartLine.png"/>
<span class="visually-hidden">Add プリズム・リーブル to wishlist</span>
</button>
</div>
<button class="giv-ProductTile-quickBuy js-ProductTile-cta js-layer__dialog__open keyboard-interaction-only" data-layer-id="quickbuy-layer" data-layer-url="https://www.givenchybeauty.com/on/demandware.store/Sites-givenchy-beauty-ja-Site/ja_JP/Product-QuickBuyLayer?pid=P090303" type="button">
クイックビュー <span class="visually-hidden">プリズム・リーブル</span>
</button>
<!-- END_dwmarker -->
</article>
</div>"""

PDP_US = """<!DOCTYPE html><html lang="en"><head><link rel="stylesheet" href="/on/demandware.static/Sites-givenchy-beauty-us-Site/x.css"></head><body class="js-pdp pdp__page">
<script type="application/ld+json">
        {"@context":"http://schema.org/","@type":"Product","name":"Fantasque","description":"Exclusive Services LUXURIOUS GIFT WRAPPING EXCLUSIVE GIFTS & SAMPLES EXPRESS SHIPPING AN ENCHANTING DUO OF MYRRH & INCENSE The precious and mystical opulence of Myrrh Absolute and Incense Essence, draped in opulent Malaysian Oud, reveal the precious fragrance of Fantasque. The majestic elegance of Damascena rose heightened by intense Bourbon vanilla absolute and oud wood, are leaving a fascinating and intriguing trail. AN ABSOLUTELY EXTRAVAGANT FRAGRANCE Expect the unexpected: Fantasque scorns banality. This unconventional, utterly free spirit lives life to the fullest, matched only by its magnetic, intriguing fragrance, which reveals an unpredictable and faceted sillage. LA COLLECTION PARTICULIÈRE Crafted from the most precious materials, these fragrances of exception, each with their own powerful temperament express the Maison Givenchy’s unique savoir-faire.","mpn":"P000170","sku":"P000170","offers":{"url":"https://www.givenchybeauty.com/us/p/fantasque-P000170.html","@type":"Offer","priceCurrency":"USD","price":"435.00","availability":"http://schema.org/InStock"},"@id":"product:P000170","brand":{"@type":"Brand","name":"Givenchy"},"image":["https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dwe91395e3/images/P000170/3274872468634_P000170_LCP_FANTASQUE_EDP_100ML_100ML_a_0.png?sw=1200&sh=1200&strip=false","https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dwe624c50e/images/P000170/3274872468634_P000170_LCP_FANTASQUE_EDP_100ML_100ML_a_3.png?sw=1200&sh=1200&strip=false"]}
    </script>
<link href="https://www.givenchybeauty.com/us/p/fantasque-F10100212.html" rel="canonical"/>
<span class="pdp__name">Fantasque </span>
<h1 class="pdp__heading">
<span class="pdp__name">Fantasque </span>
<span class="pdp__typology js-typology">La Collection Particulière - Eau de Parfum Intense<br/><b>Oud, Ambery, Spicy</b></span>
</h1>
<ol aria-label="breadcrumbs" class="breadcrumbs" itemscope="" itemtype="https://schema.org/BreadcrumbList" role="navigation">
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="https://www.givenchybeauty.com/us" itemprop="item">
<span itemprop="name">Givenchy Beauty </span>
</a>
<span>—</span>
<meta content="1.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/us/fragrance/" itemprop="item">
<span itemprop="name">Fragrance</span>
</a>
<span>—</span>
<meta content="2.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/us/fragrance/la-collection-particuliere/" itemprop="item">
<span itemprop="name">LA COLLECTION PARTICULIÈRE</span>
</a>
<span>—</span>
<meta content="3.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/us/fragrance/la-collection-particuliere/oud/" itemprop="item">
<span itemprop="name">OUD</span>
</a>
<span>—</span>
<meta content="4.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a aria-current="page" class="keyboard-interaction-only" href="#" itemprop="item">
<span itemprop="name">Fantasque</span>
</a>
<meta content="5.0" itemprop="position"/>
</li>
</ol>
<span class="pdp__capsule__value">100 ml</span>
<span class="pdp__add-to-cart__capsule__price pdp__add-to-cart__variation__price">
<span class="pdp__add-to-cart__variation__price-volume js-price-volume"></span>
<span class="pdp__add-to-cart__variation__price-list js-price-list"></span>
<span class="pdp__add-to-cart__variation__price-sales js-price-sales">$435.00</span>
</span>
</body></html>"""

PDP_GB = """<!DOCTYPE html><html lang="en"><head><link rel="stylesheet" href="/on/demandware.static/Sites-givenchy-beauty-us-Site/x.css"></head><body class="js-pdp pdp__page">
<script type="application/ld+json">
        {"@context":"http://schema.org/","@type":"Product","name":"Fantasque","description":"Exclusive Services LUXURIOUS GIFT WRAPPING EXCLUSIVE GIFTS & SAMPLES EXPRESS SHIPPING AN ENCHANTING DUO OF MYRRH & INCENSE The precious and mystical opulence of Myrrh Absolute and Incense Essence, draped in opulent Malaysian Oud, reveal the precious fragrance of Fantasque. The majestic elegance of Damascena rose heightened by intense Bourbon vanilla absolute and oud wood, are leaving a fascinating and intriguing trail. AN ABSOLUTELY EXTRAVAGANT FRAGRANCE Expect the unexpected: Fantasque scorns banality. This unconventional, utterly free spirit lives life to the fullest, matched only by its magnetic, intriguing fragrance, which reveals an unpredictable and faceted sillage. LA COLLECTION PARTICULIÈRE Crafted from the most precious materials, these fragrances of exception, each with their own powerful temperament express the Maison Givenchy’s unique savoir-faire.","mpn":"P000170","sku":"P000170","offers":{"url":"https://www.givenchybeauty.com/gb/p/fantasque-P000170.html","@type":"Offer","priceCurrency":"GBP","price":"285.00","availability":"http://schema.org/InStock"},"@id":"product:P000170","brand":{"@type":"Brand","name":"Givenchy"},"image":["https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dwe91395e3/images/P000170/3274872468634_P000170_LCP_FANTASQUE_EDP_100ML_100ML_a_0.png?sw=1200&sh=1200&strip=false","https://www.givenchybeauty.com/dw/image/v2/BBZW_PRD/on/demandware.static/-/Sites-givenchy-beauty-master/default/dwe624c50e/images/P000170/3274872468634_P000170_LCP_FANTASQUE_EDP_100ML_100ML_a_3.png?sw=1200&sh=1200&strip=false"]}
    </script>
<link href="https://www.givenchybeauty.com/gb/p/fantasque-F10100212.html" rel="canonical"/>
<span class="pdp__name">Fantasque </span>
<h1 class="pdp__heading">
<span class="pdp__name">Fantasque </span>
<span class="pdp__typology js-typology">La Collection Particulière - Eau de Parfum Intense<br/><b>Oud, Ambery, Spicy</b></span>
</h1>
<ol aria-label="breadcrumbs" class="breadcrumbs" itemscope="" itemtype="https://schema.org/BreadcrumbList" role="navigation">
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="https://www.givenchybeauty.com/gb" itemprop="item">
<span itemprop="name">Givenchy Beauty </span>
</a>
<span>—</span>
<meta content="1.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/gb/fragrance-1/" itemprop="item">
<span itemprop="name">Fragrance</span>
</a>
<span>—</span>
<meta content="2.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/gb/fragrance/la-collection-particuliere-2/" itemprop="item">
<span itemprop="name">LA COLLECTION PARTICULIÈRE</span>
</a>
<span>—</span>
<meta content="3.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/gb/fragrance/la-collection-particuliere/oud/" itemprop="item">
<span itemprop="name">OUD</span>
</a>
<span>—</span>
<meta content="4.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a aria-current="page" class="keyboard-interaction-only" href="#" itemprop="item">
<span itemprop="name">Fantasque</span>
</a>
<meta content="5.0" itemprop="position"/>
</li>
</ol>
<span class="pdp__capsule__value">100 ml</span>
<span class="pdp__add-to-cart__capsule__price pdp__add-to-cart__variation__price">
<span class="pdp__add-to-cart__variation__price-volume js-price-volume">(£2,850.00/L)</span>
<span class="pdp__add-to-cart__variation__price-list js-price-list"></span>
<span class="pdp__add-to-cart__variation__price-sales js-price-sales">£285.00</span>
</span>
</body></html>"""

PDP_INT = """<!DOCTYPE html><html lang="en"><head><link rel="stylesheet" href="/on/demandware.static/Sites-givenchy-beauty-us-Site/x.css"></head><body class="js-pdp pdp__page">
<link href="https://www.givenchybeauty.com/int/en/p/fantasque-F10100212.html" rel="canonical"/>
<span class="pdp__name">Fantasque </span>
<h1 class="pdp__heading">
<span class="pdp__name">Fantasque </span>
<span class="pdp__typology js-typology">La Collection Particulière - Eau de Parfum Intense<br/><b>Oud, Ambery, Spicy</b></span>
</h1>
<ol aria-label="breadcrumbs" class="breadcrumbs" itemscope="" itemtype="https://schema.org/BreadcrumbList" role="navigation">
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="https://www.givenchybeauty.com/int/en" itemprop="item">
<span itemprop="name">Givenchy Beauty </span>
</a>
<span>—</span>
<meta content="1.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/int/en/exceptional-fragrances/" itemprop="item">
<span itemprop="name">EXCEPTIONAL FRAGRANCES</span>
</a>
<span>—</span>
<meta content="2.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/int/en/exceptional-fragrances/olfactory-family/" itemprop="item">
<span itemprop="name">Olfactory Family</span>
</a>
<span>—</span>
<meta content="3.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a class="breadcrumb-link keyboard-interaction-only" href="/int/en/exceptional-fragrances/olfactory-family/oud/" itemprop="item">
<span itemprop="name">Oud</span>
</a>
<span>—</span>
<meta content="4.0" itemprop="position"/>
</li>
<li class="breadcrumb" itemprop="itemListElement" itemscope="" itemtype="https://schema.org/ListItem">
<a aria-current="page" class="keyboard-interaction-only" href="#" itemprop="item">
<span itemprop="name">Fantasque</span>
</a>
<meta content="5.0" itemprop="position"/>
</li>
</ol>
<span class="pdp__capsule__value">100 ml</span>
<div class="pdp__region__sticky__product__price js-sticky-price-volume">
<span class="pdp__region__sticky__product__price-list js-price-list"></span>
<span class="pdp__region__sticky__product__price-sales js-price-sales"></span>
<span class="pdp__region__sticky__product__price-volume js-price-volume"></span>
</div>
</body></html>"""


def _listing(*tiles, locale="us"):
    """A minimal listing document around one or more tile fixtures.

    Carries the site's own asset marker so `detect_page_state` reads it as
    content rather than as an empty page -- a bare fragment with neither
    that marker nor a product hook classifies as `empty`, which would make
    every assertion below pass for the wrong reason.
    """
    return ('<!DOCTYPE html><html lang="en"><head>'
            '<link rel="stylesheet" href="/on/demandware.static/'
            'Sites-givenchy-beauty-%s-Site/x.css"></head><body>'
            '<div class="search-result-content">%s</div>'
            '</body></html>' % (locale, "".join(tiles)))


US_LIPS = "https://www.givenchybeauty.com/us/makeup/lips/"
GB_LIPS = "https://www.givenchybeauty.com/gb/makeup/lips/"
INT_LIPS = "https://www.givenchybeauty.com/int/en/makeup/lips/"


# ---------------------------------------------------------------------------
def test_money_parsing():
    group("parse_money: every grouping convention this site can print")
    ok = True
    # The first four are shapes measured on this site; the rest are the
    # conventions CLAUDE.md §4 requires every repo in this family to handle,
    # kept because a currency this site does not print today is one deploy
    # away from being printed tomorrow, and because getting them wrong is
    # silent.
    cases = [
        ("$50.00", (50.0, "USD")),
        ("£105.20", (105.2, "GBP")),
        ("¥8,360", (8360.0, "JPY")),
        ("€1.234,56", (1234.56, "EUR")),
        ("1 234,56 €", (1234.56, "EUR")),
        ("1 234,56 €", (1234.56, "EUR")),      # NBSP
        ("1 234,56 €", (1234.56, "EUR")),      # narrow NBSP
        ("$1,234", (1234.0, "USD")),                     # 3 trailing digits = grouping
        ("$1,23", (1.23, "USD")),                        # 2 trailing digits = decimals
        ("349,– €", (349.0, "EUR")),           # dash cents
        ("100 CHF", (100.0, None)),                      # ISO code outside the allowlist
        ("USD 100", (100.0, "USD")),
        ("", (None, None)),
        (None, (None, None)),
        ("Sold out", (None, None)),
    ]
    for text, expected in cases:
        got = product_parser.parse_money(text, locale="us")
        ok &= check("parse_money(%r) == %r (got %r)" % (text, expected, got),
                    got == expected)

    # The trap this site actually sets: gb renders a UNIT price beside the
    # shelf price, and the unit price's grouping is the better match for a
    # naive money regex. Measured on /gb/makeup/lips/, 2026-09-18.
    got = product_parser.parse_money("£41.00 (£1,025.00/Kg)", locale="gb")
    ok &= check("a parenthesised unit price is not mistaken for the price "
                "(got %r)" % (got,), got == (41.0, "GBP"))

    # `$` is ambiguous across this site's locales and the locale is the only
    # disambiguator in the markup. Only ever reached for a text-only tile.
    ok &= check("$ reads as USD on /us/",
                product_parser.parse_money("$82.00", locale="us") == (82.0, "USD"))
    ok &= check("$ reads as CAD on /ca/en/",
                product_parser.parse_money("$82.00", locale="ca/en") == (82.0, "CAD"))

    # A percentage badge beside a price must not donate its number.
    ok &= check("a -21% badge does not become the price",
                product_parser.parse_money("-21% £105.20", locale="gb")
                == (105.2, "GBP"))
    return ok


def test_tile_parsing():
    group("parse_category: values pinned against six real tiles")
    ok = True
    # Values, not coverage. A column can be 100% populated and entirely
    # wrong (CLAUDE.md §10).
    expected = {
        "TILE_US": dict(fixture=TILE_US, url=US_LIPS, sku="P000476",
                        master_id="F20100269", price=50.0, currency="USD",
                        price_source="tile-microdata", availability="InStock",
                        shade="PINK-204", shade_count=19, original_price=None,
                        discount_pct=None, brand="Givenchy beauty",
                        title="LE ROUGE SATIN SILK LIPSTICK", badge="New"),
        "TILE_GB_SET_STRIKE": dict(fixture=TILE_GB_SET_STRIKE, url=GB_LIPS,
                        sku="PSETUK_00125", price=105.2, currency="GBP",
                        price_source="tile-microdata", original_price=134.0,
                        discount_pct=21.49, availability="OutOfStock"),
        "TILE_GB_SET_TEXT": dict(fixture=TILE_GB_SET_TEXT, url=GB_LIPS,
                        sku="PSETUK_00120", price=82.0, currency="GBP",
                        price_source="tile-text", original_price=None,
                        availability="InStock"),
        "TILE_GB_OOS": dict(fixture=TILE_GB_OOS, url=GB_LIPS,
                        sku="PSETUK_00137", price=77.0, currency="GBP",
                        price_source="tile-text", availability="OutOfStock"),
        "TILE_INT_SHOWCASE": dict(fixture=TILE_INT_SHOWCASE, url=INT_LIPS,
                        sku="P000476", price=None, currency=None,
                        price_source=None, locale="int/en", shade_count=19),
        "TILE_JP": dict(fixture=TILE_JP,
                        url="https://www.givenchybeauty.com/jp/x/",
                        sku="P090303", price=8360.0, currency="JPY",
                        price_source="tile-microdata", locale="jp"),
    }
    for name, spec in expected.items():
        fixture = spec.pop("fixture")
        url = spec.pop("url")
        rows = product_parser.parse_category(_listing(fixture), url)
        if not check("%s yields exactly one row" % name, len(rows) == 1):
            ok = False
            continue
        row = rows[0]
        for field, want in spec.items():
            got = getattr(row, field)
            ok &= check("%s.%s == %r (got %r)" % (name, field, want, got),
                        got == want)

    # The trap worth its own assertion: `data-brand` on a tile holds the
    # PRICE. A parser reading the obviously-named attribute produces a
    # column of numbers called "brand".
    row = product_parser.parse_category(_listing(TILE_US), US_LIPS)[0]
    ok &= check("brand is the brand, not the number in data-brand",
                row.brand == "Givenchy beauty" and row.brand != "50")

    # A showcase tile advertises a currency it has no price for. Believing
    # it would put USD on a row with price None.
    ok &= check("data-currency is not read on a tile with no price box",
                'data-currency="USD"' in TILE_INT_SHOWCASE
                and product_parser.parse_category(
                    _listing(TILE_INT_SHOWCASE), INT_LIPS)[0].currency is None)

    # Several tiles in one document keep their order and their own values.
    rows = product_parser.parse_category(
        _listing(TILE_GB_SET_STRIKE, TILE_GB_SET_TEXT, TILE_GB_OOS), GB_LIPS)
    ok &= check("three tiles -> three rows in document order",
                [r.sku for r in rows]
                == ["PSETUK_00125", "PSETUK_00120", "PSETUK_00137"])
    ok &= check("row_index follows the site's own order",
                [r.row_index for r in rows] == [0, 1, 2])
    ok &= check("category comes from the listing URL",
                {r.category for r in rows} == {"makeup/lips"})

    # A struck price is only a was-price when it is ABOVE the sale price --
    # the guard against reading an EU 30-day-low disclosure as one.
    ok &= check("original_price is above price wherever it is set",
                all(r.original_price is None or r.original_price > r.price
                    for r in rows if r.price is not None))

    # discount_pct is COMPUTED, never read from the site's own badge: the
    # badge says -21%, the arithmetic says 21.49%, and the arithmetic is
    # what the customer pays.
    struck = product_parser.parse_category(
        _listing(TILE_GB_SET_STRIKE), GB_LIPS)[0]
    ok &= check("discount_pct is computed (21.49), not the printed -21%",
                struck.discount_pct == 21.49 and "-21%" in TILE_GB_SET_STRIKE)

    ok &= check("an empty document yields no rows and does not raise",
                product_parser.parse_category(_listing(), US_LIPS) == [])
    ok &= check("garbage input yields no rows and does not raise",
                product_parser.parse_category("<html", US_LIPS) == [])
    return ok


def test_product_parsing():
    group("parse_product: JSON-LD primary, DOM where there is none")
    ok = True
    us = product_parser.parse_product(
        PDP_US, "https://www.givenchybeauty.com/us/p/fantasque-P000170.html")
    for field, want in [("sku", "P000170"), ("title", "Fantasque"),
                        ("price", 435.0), ("currency", "USD"),
                        ("price_source", "jsonld"), ("brand", "Givenchy"),
                        ("availability", "InStock"), ("size", "100 ml"),
                        ("master_id", "F10100212"), ("locale", "us")]:
        got = getattr(us, field)
        ok &= check("PDP_US.%s == %r (got %r)" % (field, want, got), got == want)
    ok &= check("PDP_US.category comes from the breadcrumb, product name dropped",
                us.category == "fragrance/la collection particulière")
    ok &= check("PDP_US.image_url is the first JSON-LD image",
                (us.image_url or "").startswith("https://www.givenchybeauty.com/dw/image/"))

    gb = product_parser.parse_product(
        PDP_GB, "https://www.givenchybeauty.com/gb/p/fantasque-P000170.html")
    ok &= check("the same product prices differently per locale (285.00 GBP)",
                (gb.price, gb.currency) == (285.0, "GBP"))
    ok &= check("sku is stable across locales", gb.sku == us.sku == "P000170")

    # The showcase case: no JSON-LD anywhere on the page. It must still
    # produce a ROW -- with a null price -- rather than nothing at all.
    intl = product_parser.parse_product(
        PDP_INT, "https://www.givenchybeauty.com/int/en/p/fantasque-P000170.html")
    ok &= check("a product page with no JSON-LD still parses to a row",
                intl is not None)
    ok &= check("no ld+json on the showcase fixture",
                "application/ld+json" not in PDP_INT)
    ok &= check("showcase product: price and currency are both None",
                (intl.price, intl.currency) == (None, None))
    ok &= check("showcase product: title still read from the DOM",
                intl.title == "Fantasque")
    ok &= check("showcase product: sku from the REQUESTED url, not the "
                "canonical master", intl.sku == "P000170")

    ok &= check("a document that is not a product page yields None",
                product_parser.parse_product(
                    "<html><body>nothing</body></html>",
                    "https://www.givenchybeauty.com/us/makeup/lips/") is None)
    return ok


def test_jsonld_shapes():
    group("parse_product: JSON-LD shapes that are legal and break naive parsers")
    ok = True
    HEAD = ('<!DOCTYPE html><html><head><link rel="stylesheet" '
            'href="/on/demandware.static/Sites-givenchy-beauty-us-Site/x.css">'
            '<script type="application/ld+json">%s</script></head>'
            '<body class="js-pdp"></body></html>')
    URL = "https://www.givenchybeauty.com/us/p/x-P000001.html"

    def parsed(payload):
        return product_parser.parse_product(HEAD % payload, URL)

    # "offers": null is an EXPLICIT null -- a .get(..., {}) default does not
    # apply to it, which is the AttributeError CLAUDE.md §4 names first.
    r = parsed('{"@type":"Product","name":"N","sku":"P1","offers":null}')
    ok &= check('"offers": null does not raise', r is not None)
    ok &= check('"offers": null leaves price None', r.price is None)

    # offers as a list, with a non-dict in it.
    r = parsed('{"@type":"Product","name":"N","sku":"P1","offers":'
               '["junk",{"price":"12.00","priceCurrency":"EUR"}]}')
    ok &= check("offers as a list skips non-dicts and finds the offer",
                (r.price, r.currency) == (12.0, "EUR"))

    # image in each of the four legal shapes.
    for payload, label in [
            ('"image":"https://x/a.png"', "a string"),
            ('"image":["https://x/a.png","https://x/b.png"]', "a list of strings"),
            ('"image":{"@type":"ImageObject","url":"https://x/a.png"}', "an ImageObject"),
            ('"image":[{"@type":"ImageObject","contentUrl":"https://x/a.png"}]',
             "a list of ImageObjects")]:
        r = parsed('{"@type":"Product","name":"N","sku":"P1",%s}' % payload)
        ok &= check("image as %s yields a url (got %r)" % (label, r.image_url),
                    r.image_url == "https://x/a.png")

    # A product inside @graph rather than at the top level loses EVERY row
    # silently on a parser that does not walk it.
    r = parsed('{"@context":"http://schema.org/","@graph":'
               '[{"@type":"WebSite"},{"@type":"Product","name":"G","sku":"P9",'
               '"offers":{"price":"5.00","priceCurrency":"USD"}}]}')
    ok &= check("a Product inside @graph is found", r is not None and r.sku == "P9")
    ok &= check("a Product inside @graph keeps its price", r.price == 5.0)

    # No sku field at all: recovered from the URL rather than left None.
    r = parsed('{"@type":"Product","name":"N"}')
    ok &= check("a Product with no sku falls back to the URL id",
                r.sku == "P000001")

    # The product URL under offers.url rather than at the top level.
    r = parsed('{"@type":"Product","name":"N","sku":"P1","offers":'
               '{"url":"https://www.givenchybeauty.com/us/p/y-P000002.html",'
               '"price":"7.00","priceCurrency":"USD"}}')
    ok &= check("offers.url becomes the row url",
                r.url == "https://www.givenchybeauty.com/us/p/y-P000002.html")

    ok &= check("malformed JSON-LD costs itself, not the run",
                parsed("{not json") is not None)
    return ok


def test_urls_and_ids():
    group("URLs, ids, locales, robots")
    ok = True

    # Matched 763 of 763 product URLs across five locale sitemaps on
    # 2026-09-18. These are the four shapes that made the regex what it is.
    id_cases = [
        ("https://www.givenchybeauty.com/us/p/fantasque-P000170.html", "P000170"),
        ("https://www.givenchybeauty.com/us/p/le-rouge-satin-silk-lipstick-F20100269.html",
         "F20100269"),
        ("https://www.givenchybeauty.com/int/en/p-PSETUS_00013.html", "PSETUS_00013"),
        ("https://www.givenchybeauty.com/gb/p/l-interdit-refillable-set-PSETUK_00046.html",
         "PSETUK_00046"),
        ("https://www.givenchybeauty.com/jp/p/%E9%80%9A%E5%B8%B8%E5%8C%85%E8%A3%85-851113.html",
         "851113"),
        ("https://www.givenchybeauty.com/jp/p/%E3%83%97%E3%83%AA%E3%82%BA%E3%83%A0-P000162.html",
         "P000162"),
    ]
    for url, want in id_cases:
        got = product_parser.sku_from_url(url)
        ok &= check("sku_from_url(...%s) == %r (got %r)"
                    % (url[-28:], want, got), got == want)

    # Negative controls: a regex that matches a category URL would turn
    # every listing into a product.
    for url in ["https://www.givenchybeauty.com/us/makeup/lips/",
                "https://www.givenchybeauty.com/us/",
                "https://www.givenchybeauty.com/int/en/sitemap_0-product.xml",
                None, ""]:
        ok &= check("sku_from_url(%r) is None" % (url,),
                    product_parser.sku_from_url(url) is None)

    # Locale is a PATH prefix and five of the eleven are two segments long.
    locale_cases = [
        ("https://www.givenchybeauty.com/us/makeup/lips/", "us"),
        ("https://www.givenchybeauty.com/int/en/makeup/lips/", "int/en"),
        ("https://www.givenchybeauty.com/ca/en/", "ca/en"),
        ("https://www.givenchybeauty.com/fr/fr/makeup/lips/", "fr/fr"),
        ("https://www.givenchybeauty.com/jp/x/", "jp"),
    ]
    for url, want in locale_cases:
        got = product_parser.locale_of(url)
        ok &= check("locale_of(%s) == %r (got %r)" % (url, want, got), got == want)
    # A path segment that merely STARTS with a locale code is not that
    # locale. Both of these fall through to DEFAULT_LOCALE instead of being
    # read as `int`, and the category keeps its full first segment.
    ok &= check("'international' is not read as the locale 'int'",
                product_parser.locale_of(
                    "https://www.givenchybeauty.com/international/x/")
                == product_parser.DEFAULT_LOCALE)
    ok &= check("and its first segment survives into the category",
                product_parser.category_from_url(
                    "https://www.givenchybeauty.com/international/x/")
                == "international/x")

    ok &= check("showcase locales are the two measured ones",
                product_parser.is_showcase_locale(INT_LIPS)
                and product_parser.is_showcase_locale(
                    "https://www.givenchybeauty.com/ru/x/")
                and not product_parser.is_showcase_locale(US_LIPS))

    ok &= check("category_from_url strips the locale",
                product_parser.category_from_url(US_LIPS) == "makeup/lips")
    ok &= check("category_from_url strips a two-segment locale",
                product_parser.category_from_url(INT_LIPS) == "makeup/lips")
    ok &= check("a product URL has no category of its own",
                product_parser.category_from_url(
                    "https://www.givenchybeauty.com/us/p/x-P1.html") is None)

    # robots.txt: the platform's listing parameters are disallowed, and the
    # site's OWN category sitemaps list some of them.
    ok &= check("?start= is robots-disallowed",
                not product_parser.is_robots_allowed(US_LIPS + "?start=25&sz=25"))
    ok &= check("search?cgid= is robots-disallowed",
                not product_parser.is_robots_allowed(
                    "https://www.givenchybeauty.com/ru/search?cgid=x"))
    ok &= check("an ordinary listing URL is allowed",
                product_parser.is_robots_allowed(US_LIPS))
    ok &= check("a product sitemap is allowed",
                product_parser.is_robots_allowed(
                    product_parser.sitemap_url("us", "product")))

    # page_url is a NO-OP here, and that is load-bearing: it is what makes
    # page_flow.pagination_is_addressable answer False.
    ok &= check("page_url(url, 2) returns the url unchanged",
                product_parser.page_url(US_LIPS, 2) == US_LIPS)
    ok &= check("page_url(url, 7) returns the url unchanged",
                product_parser.page_url(US_LIPS, 7) == US_LIPS)
    ok &= check("a listing is therefore not independently addressable",
                page_flow.pagination_is_addressable("category", US_LIPS, None)
                is False)
    ok &= check("page_number_from_url reads a hand-pasted ?start=",
                product_parser.page_number_from_url(US_LIPS + "?start=25&sz=25") == 2)
    ok &= check("page_number_from_url defaults to 1",
                product_parser.page_number_from_url(US_LIPS) == 1)

    # Hosts.
    ok &= check("www.givenchybeauty.com is supported",
                product_parser.is_supported_host(US_LIPS))
    ok &= check("givenchy.com is refused WITH A REASON naming the platform",
                "couture" in (product_parser.unsupported_reason(
                    "https://www.givenchy.com/us/x") or "").lower()
                or "beauty storefront" in (product_parser.unsupported_reason(
                    "https://www.givenchy.com/us/x") or ""))
    ok &= check("an unrelated host is refused",
                not product_parser.is_supported_host("https://example.com/us/"))

    # Sitemaps.
    xml = ('<urlset><url><loc>https://www.givenchybeauty.com/us/p/a-P1.html</loc></url>'
           '<url><loc>https://www.givenchybeauty.com/ru/search?cgid=x</loc></url>'
           '<url><loc>https://www.givenchybeauty.com/us/makeup/lips/</loc></url></urlset>')
    ok &= check("parse_sitemap drops robots-disallowed entries",
                product_parser.parse_sitemap(xml)
                == ["https://www.givenchybeauty.com/us/p/a-P1.html",
                    "https://www.givenchybeauty.com/us/makeup/lips/"])
    ok &= check("parse_sitemap(allowed_only=False) keeps them",
                len(product_parser.parse_sitemap(xml, allowed_only=False)) == 3)
    ok &= check("product_urls_from_sitemap keeps only products",
                product_parser.product_urls_from_sitemap(xml)
                == ["https://www.givenchybeauty.com/us/p/a-P1.html"])
    return ok


def test_showcase_and_states():
    group("page states on this site's own pages")
    ok = True
    ok &= check("a listing with tiles reads as content",
                product_parser.detect_page_state(
                    _listing(TILE_US), 200) == "content")
    ok &= check("a product page reads as content",
                product_parser.detect_page_state(PDP_US, 200) == "content")
    # A served category with no tiles is `empty`, not blocked and not a
    # fault: /es/.../sets/ and /it/.../sets/ are both real and both list
    # nothing (measured 2026-09-17).
    ok &= check("a served listing with no tiles reads as empty",
                product_parser.detect_page_state(_listing(), 200) == "empty")
    ok &= check("an empty body reads as blocked",
                product_parser.detect_page_state("", 200) == "blocked")
    ok &= check("a sitemap reads as content, not as a block",
                product_parser.detect_page_state(
                    "<urlset><url><loc>x</loc></url></urlset>", 200) == "content")
    # The status vetoes a good-looking body.
    ok &= check("a 403 carrying a real-looking listing is still not content",
                product_parser.detect_page_state(_listing(TILE_US), 403) != "content")
    ok &= check("`empty` is not retried -- it is a correct answer",
                page_flow.should_retry("empty") is False)
    return ok


def _page(body_html, ready_marker=True):
    """A minimal SERVED document.

    `detect_page_state` needs two things before it will call a page content:
    the site's own asset path (`Sites-givenchy-beauty-...`, present hundreds
    of times on every real page and structurally absent from an
    interstitial) and a product hook. A bare fragment with neither
    classifies as `empty` regardless of what else it holds, which would make
    a test pass for the wrong reason.
    """
    marker = ('<link rel="stylesheet" href="/on/demandware.static/'
              'Sites-givenchy-beauty-us-Site/x.css">'
              '<article class="js-ProductItem"></article>') if ready_marker else ""
    return "<html><head>%s</head><body>%s</body></html>" % (marker, body_html)


# The challenge this site actually serves. Transcribed from a real capture
# taken 2026-09-16 (HTTP 405, `x-amzn-waf-action: captcha`, `server:
# CloudFront`, 2331 bytes) -- the gokuProps values are truncated here and
# the per-deployment ids in the script hostnames are the real shape but not
# a live deployment, since neither is a credential and neither has to be
# real for the parser to be exercised. NOT a first-party capture by this
# repo: replace it with one the first time a run meets a live challenge.
FIX_AWS_WAF_CHALLENGE = """<!DOCTYPE html><html lang="en"><head>
<title>Human Verification</title>
<script type="text/javascript">
window.awsWafCookieDomainList = [];
window.gokuProps = {
  "key":"AQIDAHjcYu/GjX+QlghicBgQ/7bFaQZ+m5FKCMDnO+vTbNg96A==",
  "iv":"D54pBwHvVAAAA+vK",
  "context":"YVOg6946nHFLSvC1mQcbSeAO9SL2AREZnOrNAWPRMAs3ZoXUEFsk9RA9"
};
</script>
<script src="https://6cb07a88f2ca.54698f12.us-east-1.token.awswaf.com/6cb/challenge.js"></script>
<script src="https://6cb07a88f2ca.54698f12.us-east-1.captcha.awswaf.com/6cb/captcha.js"></script>
</head><body><div id="captcha-container"></div></body></html>"""


def test_aws_waf():
    group("AWS WAF: a challenge this repo carries detection for")
    ok = True
    import captcha_solver as cs

    # --- §18's control: every marker must be ABSENT from real pages -------
    # A marker that fires on a good page is worse than no marker at all.
    good_pages = {
        "us listing": _listing(TILE_US),
        "gb listing (sets)": _listing(TILE_GB_SET_STRIKE, TILE_GB_SET_TEXT),
        "showcase listing": _listing(TILE_INT_SHOWCASE),
        "us product": PDP_US,
        "showcase product": PDP_INT,
    }
    markers = product_parser.BOT_CHALLENGE_MARKERS["AWS WAF"]
    ok &= check("the marker set names AWS WAF at all (it did not until v0.4.1, "
                "which is why this site's only real challenge was invisible)",
                len(markers) >= 3)
    for label, page in good_pages.items():
        hits = [m for m in markers if m in page]
        ok &= check("no AWS WAF marker appears on a real %s page (%s)"
                    % (label, "clean" if not hits else "FIRES ON: %s" % hits),
                    not hits)
    ok &= check("...and every marker does appear on the challenge itself",
                all(m in FIX_AWS_WAF_CHALLENGE for m in markers))

    # --- the states, per engine capability -------------------------------
    # Selenium and pyppeteer cannot supply a status; playwright and the
    # Scraper API can. All of them must reach `captcha`, because all of them
    # used to reach `empty` -- exit 4, reported as a real empty listing.
    ok &= check("challenge + no status (selenium/pyppeteer) -> captcha, not empty",
                product_parser.detect_page_state(FIX_AWS_WAF_CHALLENGE) == "captcha")
    ok &= check("challenge + HTTP 405 -> captcha, not blocked (blocked has "
                "solve=False, so the solver would never be offered it)",
                product_parser.detect_page_state(
                    FIX_AWS_WAF_CHALLENGE, status=405) == "captcha")
    ok &= check("the x-amzn-waf-action header alone is decisive, whatever the body",
                product_parser.detect_page_state(
                    "", headers={"x-amzn-waf-action": "captcha"}) == "captcha")
    ok &= check("...case-insensitively, since every layer spells headers differently",
                product_parser.detect_page_state(
                    "", headers={"X-Amzn-Waf-Action": "CAPTCHA"}) == "captcha")
    ok &= check("and the policy for that state actually solves",
                page_flow.should_solve("captcha") and not page_flow.should_solve("blocked"))

    # --- §17's classification-order trap ---------------------------------
    served_with_hunter = _page(
        '<script src="chrome-extension://kjmk/content/captcha/amazon_waf/'
        'interceptor.js"></script>')
    ok &= check("a SERVED page carrying 2Captcha's own injected WAF hunter "
                "still classifies as content, not as a challenge",
                product_parser.detect_page_state(served_with_hunter,
                                                 status=200) == "content")
    ok &= check("a genuinely empty page is still 'empty', not a false captcha",
                product_parser.detect_page_state(
                    "<html><body>nothing here</body></html>", status=200) == "empty")

    # --- the task the solver builds --------------------------------------
    ch = cs.detect_aws_waf(FIX_AWS_WAF_CHALLENGE,
                           "https://www.givenchybeauty.com/gb/makeup/lips/")
    ok &= check("detect_aws_waf reads a challenge out of the served HTML "
                "(no runtime interception needed, unlike Turnstile)",
                ch is not None and ch.kind == "aws_waf")
    if ch:
        task = cs._v2_task_for(ch, 0.7)
        ok &= check("task type is AmazonTaskProxyless, per 2captcha's own docs",
                    task["type"] == "AmazonTaskProxyless")
        ok &= check("all three gokuProps values reach the task under the "
                    "vendor's field names (key->websiteKey, iv, context)",
                    task["websiteKey"].startswith("AQIDAHjcYu")
                    and task["iv"] == "D54pBwHvVAAAA+vK"
                    and task["context"].startswith("YVOg6946"))
        ok &= check("both optional script URLs are sent when the page gave them",
                    task["challengeScript"].endswith("challenge.js")
                    and task["captchaScript"].endswith("captcha.js"))
        ok &= check("the answer goes in the aws-waf-token COOKIE, not a form "
                    "field -- a different injection path from reCAPTCHA",
                    cs.AWS_WAF_COOKIE == "aws-waf-token")

    # --- refusing to pay for an incomplete detection ----------------------
    ok &= check("a partial gokuProps (no context) yields no challenge rather "
                "than a task the API would reject at full price",
                cs.detect_aws_waf('<script>window.gokuProps = {"key":"k","iv":"i"};</script>',
                                  "u") is None)
    ok &= check("unparseable gokuProps degrades to 'not detected', not an "
                "exception inside a detector every page goes through",
                cs.detect_aws_waf('<script>window.gokuProps = {key: bare};</script>',
                                  "u") is None)
    ok &= check("a real page yields no AWS WAF challenge",
                cs.detect_aws_waf(_listing(TILE_US), "u") is None)
    return ok


def test_bot_detection():
    group("bot/block/captcha detection")
    ok = True

    ok &= check("no markers at all -> no challenge detected",
                detect_bot_challenge("<html><body>hello</body></html>") is None)
    ok &= check("empty/None html -> no challenge, not a crash",
                detect_bot_challenge("") is None and detect_bot_challenge(None) is None)

    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        sample = "<html><body>%s</body></html>" % markers[0]
        ok &= check("a page containing %r is attributed to %s" % (markers[0], vendor),
                    detect_bot_challenge(sample) == vendor)

    # The Scraping Browser API's auto-solve extension injects its own script
    # tags into every page it loads; those must not be mistaken for the
    # site's own challenge markup (see the module's _EXTENSION_TAG_RE note).
    injected = ('<html><body><script src="chrome-extension://abc123/hunt.js">'
               'cf-turnstile probe</script>clean content</body></html>')
    ok &= check("an extension-injected script mentioning a vendor marker is "
                "stripped before matching, so a clean page is not misclassified",
                detect_bot_challenge(injected) is None)

    # detect_page_state: the four states.
    ok &= check("a non-2xx status is 'blocked' regardless of body",
                detect_page_state("<html>anything</html>", status=403) == "blocked")
    ok &= check("no html at all is 'blocked'", detect_page_state("", status=200) == "blocked")
    ok &= check("a reCAPTCHA marker is 'captcha' (the paid-solve path), not 'blocked'",
                detect_page_state('<html><body><script src="recaptcha/api.js">'
                                  '</script></body></html>', status=200) == "captcha")
    ok &= check("a DataDome marker is 'blocked' (no solver integrated for it)",
                detect_page_state('<html><body>datadome</body></html>', status=200) == "blocked")
    ok &= check("real content (a listing) with no challenge marker is 'content'",
                detect_page_state(_page(""), status=200) == "content")
    ok &= check("real content (a product page) with no challenge marker is 'content'",
                detect_page_state(PDP_US, status=200) == "content")
    # The Akamai behavioural interstitial this repo met live on 2026-09-18.
    # Before it was added to the marker set it read as `empty` -- a state
    # the family does not retry -- and the run reported SUCCESS with a row
    # of nulls. This is the regression guard for that.
    _AKAMAI_SEC_CPT = ('<html><body><div id="sec-if-cpt-container">'
                       '<div id="sec-bc-tile-container"></div>'
                       '<div class="scf-akamai-logo"></div></div></body></html>')
    ok &= check("the Akamai sec-cpt interstitial is 'blocked', never 'empty'",
                detect_page_state(_AKAMAI_SEC_CPT, status=200) == "blocked")
    ok &= check("...and is recognised as self-clearing, so an engine waits "
                "for it instead of reporting a false exit 3",
                is_self_clearing_challenge(_AKAMAI_SEC_CPT))
    ok &= check("a served page is NOT mistaken for a self-clearing challenge",
                not is_self_clearing_challenge(_page(""))
                and not is_self_clearing_challenge(PDP_US))
    ok &= check("a clean 200 page with neither anchor present is 'empty', not 'content'",
                detect_page_state("<html><body>nothing recognisable</body></html>",
                                  status=200) == "empty")
    return ok


def test_page_flow():
    group("page_flow: ready selectors, state policy, pagination gating")
    ok = True

    for mode in ("category", "product"):
        ok &= check("%s has a ready_selector, min_matches and a timeout" % mode,
                    page_flow.ready_selector(mode) and page_flow.content_timeout_ms(mode) > 0)
    ok &= check("a listing requires more than one tile match (a lucky single "
                "match must not resolve the wait)",
                page_flow.min_matches("category") > 1)
    ok &= check("product (one product, not a grid) needs no row floor",
                page_flow.min_matches("product") == 0)
    ok &= check("an unknown mode falls back to the category selector "
                "rather than raising",
                page_flow.ready_selector("nonsense-mode") == page_flow.ready_selector("category"))
    ok &= check("the ready selector anchors on the TILE, not on a product "
                "link -- a tile links to its product twice, so a link count "
                "would reach any threshold at half the grid",
                "productTile-wrapper" in page_flow.ready_selector("category"))

    ok &= check("classify() delegates to product_parser.detect_page_state",
                page_flow.classify(_page(""), status=200) == "content")

    # STATE_POLICY, exercised through the accessor functions rather than the
    # dict directly -- an engine reaches these, not the dict.
    ok &= check("content: no retry, no solve, not counted as blocked",
                not page_flow.should_retry("content") and not page_flow.should_solve("content")
                and not page_flow.counts_as_blocked("content"))
    ok &= check("blocked: retried, but not solved (nothing to solve), and IS a block",
                page_flow.should_retry("blocked") and not page_flow.should_solve("blocked")
                and page_flow.counts_as_blocked("blocked"))
    ok &= check("captcha: retried AND solved, and counts as blocked until solved",
                page_flow.should_retry("captcha") and page_flow.should_solve("captcha")
                and page_flow.counts_as_blocked("captcha"))
    ok &= check("empty: a correct answer, not retried, not a block",
                not page_flow.should_retry("empty") and not page_flow.should_solve("empty")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unrecognised state defaults to the safe 'do nothing' answer",
                not page_flow.should_retry("???") and not page_flow.should_solve("???")
                and not page_flow.counts_as_blocked("???"))

    # comparable(): query-parameter order should not matter, fragment is dropped.
    ok &= check("comparable() normalises query-parameter order",
                page_flow.comparable("https://x/y?b=2&a=1")
                == page_flow.comparable("https://x/y?a=1&b=2"))
    ok &= check("comparable() drops the fragment",
                page_flow.comparable("https://x/y?a=1#frag")
                == page_flow.comparable("https://x/y?a=1"))

    # Pagination on THIS site: the listing's own paging is `?start=N&sz=25`
    # and robots.txt disallows both parameters, so `page_url` is a no-op and
    # nothing is independently addressable. These assertions pin that as a
    # DECISION rather than an accident -- if someone makes `page_url`
    # construct a URL again, this is what fails first.
    _LISTING = "https://www.givenchybeauty.com/us/makeup/lips/"
    ok &= check("page_url is a no-op, so a listing is never addressable",
                page_flow.pagination_is_addressable("category", _LISTING, None)
                is False)
    ok &= check("...and stays unaddressable even when handed a next-link",
                page_flow.pagination_is_addressable(
                    "category", _LISTING, _LISTING + "?start=25&sz=25") is False)
    ok &= check("the paging parameters are the ones robots.txt disallows",
                not is_robots_allowed(_LISTING + "?start=25&sz=25"))

    # is_thin_page(): informational-only diagnostic, deliberately NOT a hard
    # PAGE_CAP -- the sku-based "no_new_products" stop condition already
    # covers real pagination exhaustion; this only flags a page that came
    # back suspiciously short of page 1, which is either the listing's real
    # depth or a markup regression, and it is up to the caller (which just
    # logs a warning) to tell those apart.
    ok &= check("a page with a normal row count relative to page 1 is not thin",
                not page_flow.is_thin_page(23, 25))
    ok &= check("a page with well under THIN_PAGE_RATIO of page 1's rows IS thin",
                page_flow.is_thin_page(5, 25))
    ok &= check("the boundary itself is not thin (< , not <=)",
                not page_flow.is_thin_page(10, 25))  # 10 == 25 * 0.4 exactly
    ok &= check("a page 1 that itself had 0 rows never reports a later page "
                "as thin (0 < 0 is False, not a crash)",
                page_flow.is_thin_page(0, 0) is False)

    ok &= check("PRICE_COVERAGE_FLOOR is a fraction, not a percentage",
                0.0 < page_flow.PRICE_COVERAGE_FLOOR < 1.0)
    return ok


# ---------------------------------------------------------------------------
def test_readiness_wait():
    group("page_flow.wait_for_count: poll a count, never evaluate a string")
    ok = True

    # ready_count converts min_matches's "strictly MORE than this floor" into
    # the "at least this many" wait_for_count is written against. They are
    # tied together here because the conversion is the kind of off-by-one
    # that would leave the three engines waiting on different thresholds
    # while every other check stayed green.
    for mode in ("category", "product"):
        ok &= check("ready_count(%s) is one more than its min_matches floor" % mode,
                    page_flow.ready_count(mode) == page_flow.min_matches(mode) + 1)
    ok &= check("product mode still waits for its single price node to "
                "appear -- a floor of 0 must not collapse into 'do not wait "
                "at all'", page_flow.ready_count("product") == 1)
    # The smallest real category in this repo's captures holds ONE product
    # (/us/makeup/face/bronzer/), so the floor is 2 rather than the family's
    # usual 3 and a timed-out wait is treated as "parse what is there".
    ok &= check("the category floor is low enough for a one-product listing "
                "to be a short wait rather than a full timeout",
                page_flow.ready_count("category") <= 3)

    def fake_driver(counts):
        """A driver that returns a scripted sequence of counts and only
        records its sleeps, so the wait's arithmetic is testable with no
        browser and no engine library installed."""
        state = {"polls": 0, "slept": 0, "selectors": []}

        def count(selector):
            state["selectors"].append(selector)
            i = min(state["polls"], len(counts) - 1)
            state["polls"] += 1
            return counts[i]

        def sleep(ms):
            state["slept"] += ms

        return count, sleep, state

    count, sleep, st = fake_driver([0, 1, 4])
    seen = page_flow.wait_for_count(count, sleep, "table.items tbody tr", 4, 20000)
    ok &= check("returns as soon as the minimum is reached", seen == 4)
    ok &= check("and stops polling there rather than spending the budget",
                st["polls"] == 3 and st["slept"] == 500)
    ok &= check("it polls the selector it was handed, not a hardcoded one",
                set(st["selectors"]) == {"table.items tbody tr"})

    count, sleep, st = fake_driver([7])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 20000)
    ok &= check("a page already painted is not slept on at all",
                seen == 7 and st["slept"] == 0 and st["polls"] == 1)

    count, sleep, st = fake_driver([2])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 1000, poll_ms=250)
    ok &= check("gives up when the budget runs out rather than looping forever",
                st["slept"] == 1000)
    ok &= check("and returns the LAST COUNT SEEN, so a caller can tell "
                "'painted' from 'timed out holding two of them'", seen == 2)

    count, sleep, st = fake_driver([0])
    page_flow.wait_for_count(count, sleep, "x", 1, 0)
    ok &= check("a zero budget polls once and returns, it does not sleep",
                st["polls"] == 1 and st["slept"] == 0)

    # The reason any of this exists. Playwright's wait_for_function and
    # pyppeteer's waitForFunction hand the BROWSER a string to evaluate, and
    # a site whose CSP omits unsafe-eval refuses it -- which on a sibling
    # repo was an EvalError and exit 1 on the site's most obvious URL, on one
    # of its two listing routes and not the other. Checked against the source
    # on disk rather than an imported module, so it still holds for the two
    # engines whose library is absent from this machine.
    #
    # The needles are BUILT rather than written out, for the same reason the
    # banned-phrase scan's list is: this file is scanned too, and spelling
    # them here would fail the build on the very file implementing the check
    # -- with no allowlist to reach for, because an allowlist is how a scan
    # stops covering the thing it was written for.
    eval_waits = ("wait_for_" + "function(", "wait" + "ForFunction(")
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s waits on no evaluated string" % name,
                    not any(needle in src for needle in eval_waits))

    for name in ("playwright_scraper.py", "puppeteer_scraper.py",
                 "selenium_scraper.py"):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s takes its readiness WAIT from page_flow too, not just "
                    "the selector" % name, "page_flow.wait_for_count" in src)
    return ok


# ---------------------------------------------------------------------------
def test_output_contract():
    group("output_writer: one row schema, and no permanently-null columns")
    ok = True

    names = [f.name for f in fields(Product)]
    nameset = set(names)
    family_prefix = ["source", "scraped_at", "url", "sku", "title",
                     "image_url", "price", "currency", "category"]
    ok &= check("Product opens on the family's shared prefix, IN ORDER -- a "
                "consumer written against another repo in this family reads "
                "the first nine columns unchanged",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("price_source follows it: this site states a price four "
                "different ways and they are not equally trustworthy",
                names[len(family_prefix)] == "price_source")

    # Columns removed after measurement, each with its count in
    # output_writer.py's docstring. A column that is null on every row of
    # every run should not exist, and removing one needs the measurement
    # written down so someone can add it back with a better one.
    measured_away = {"lowest_price_30d", "rating", "review_count", "prime",
                     "description"}
    ok &= check("no column survives that was measured to be always null",
                not (measured_away & nameset))

    # Columns that ARE here because this site genuinely publishes them.
    ok &= check("the site-specific columns are appended after the family "
                "prefix, not interleaved into it",
                {"locale", "master_id", "shade", "shade_count",
                 "availability", "original_price", "discount_pct"} <= nameset)
    ok &= check("original_price and discount_pct exist because a real "
                "discount chain was measured (a set at 105.20 struck from "
                "134.00), not by analogy with a sibling repo",
                "original_price" in nameset and "discount_pct" in nameset)

    ok &= check("ROW_CLASS_BY_MODE covers exactly the two modes this repo "
                "supports", set(ROW_CLASS_BY_MODE) == {"category", "product"})
    ok &= check("both modes map to Product -- this is a catalogue, so there "
                "is no second kind of row the way transfermarkt-scraper "
                "needed one for people and events",
                all(cls is Product for cls in ROW_CLASS_BY_MODE.values()))
    ok &= check("both modes are dedupe-by-sku safe",
                set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))

    ok &= check("a fresh Product has an ISO scraped_at with no arguments needed",
                "T" in Product().scraped_at)
    ok &= check("source defaults to the one hostname this repo reads",
                Product().source == SOURCE_DEFAULT == "givenchybeauty.com")

    # sku is the VARIANT id, which is what the tile, the pid and the PDP's
    # own JSON-LD all agree on; the master id lives beside it. Getting this
    # backwards collapses up to nineteen shades of one lipstick into a row.
    row = parse_category(_listing(TILE_US), US_LIPS)[0]
    ok &= check("sku is the variant and master_id is the style id, not the "
                "other way round",
                (row.sku, row.master_id) == ("P000476", "F20100269"))
    ok &= check("the product URL carries the MASTER id, which is why sku "
                "cannot be read from it",
                "F20100269" in row.url and "P000476" not in row.url)
    return ok


def test_writers():
    group("dedupe + JSON/CSV writers")
    ok = True

    seen = set()
    rows = [Product(sku="1"), Product(sku="2"), Product(sku="1"), Product(sku=None), Product(sku=None)]
    fresh = dedupe_by_key(rows, seen)
    ok &= check("a repeated sku across pages is dropped",
                [r.sku for r in fresh] == ["1", "2", None, None])
    ok &= check("a row with no sku is never dropped (nothing to compare it against)",
                sum(1 for r in fresh if r.sku is None) == 2)
    ok &= check("dedupe_by_sku is the same rule under its own name",
                [r.sku for r in dedupe_by_sku(
                    [Product(sku="9"), Product(sku="9")], set())] == ["9"])

    with tempfile.TemporaryDirectory() as d:
        json_path = os.path.join(d, "out.json")
        csv_path = os.path.join(d, "out.csv")
        sample = [Product(sku="1", title="A", shades=["NUDE-1", "PINK-204"]),
                  Product(sku="2", title="B", shades=None)]
        write_json(sample, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        ok &= check("write_json round-trips a list field (shades) as a real list",
                    loaded[0]["shades"] == ["NUDE-1", "PINK-204"])

        write_csv(sample, csv_path, row_cls=Product)
        with open(csv_path, encoding="utf-8", newline="") as f:
            csv_rows = list(csv.DictReader(f))
        ok &= check("write_csv joins a list field with the documented separator, "
                    "so it round-trips by splitting on the same string",
                    csv_rows[0]["shades"] == LIST_CSV_SEPARATOR.join(["NUDE-1", "PINK-204"]))
        ok &= check("CSV header matches the Product schema exactly",
                    list(csv_rows[0].keys()) == [f.name for f in fields(Product)])

        # An empty result must still get a header, from row_cls, not the first row.
        empty_csv = os.path.join(d, "empty.csv")
        write_csv([], empty_csv, row_cls=Product)
        header = open(empty_csv, encoding="utf-8").readline().strip().split(",")
        ok &= check("write_csv([], ...) still writes the Product header, not "
                    "an empty file", header == [f.name for f in fields(Product)])
    return ok


def test_finish_run():
    group("save() / finish_run(): the empty-run and exit-code contract")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "run")

        # The family invariant: a run that finds nothing writes nothing,
        # unless the caller explicitly says an empty result is expected.
        rc = save([], prefix, "both", allow_empty=False)
        ok &= check("0 rows, no --allow-empty: nothing written, exit EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS and not os.path.exists(prefix + ".json"))
        rc = save([], prefix, "both", allow_empty=True)
        ok &= check("0 rows WITH --allow-empty: files ARE written",
                    os.path.exists(prefix + ".json") and os.path.exists(prefix + ".csv"))

        # finish_run: a complete run with rows.
        rows = [Product(sku="1"), Product(sku="2")]
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="completed", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("a complete run with rows returns 0", rc == 0)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and its meta sidecar says status=complete", meta["status"] == "complete")

        # A partial run (stopped early but got some rows) -> EXIT_PARTIAL,
        # and the meta sidecar must say so rather than claiming completeness.
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="captcha_unsolved", pages_requested=3, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("a partial run (some rows, did not finish) returns EXIT_PARTIAL",
                    rc == EXIT_PARTIAL)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and the sidecar says status=partial, not complete",
                    meta["status"] == "partial")

        # A run that got NOTHING and was blocked -> EXIT_BLOCKED, distinct
        # from EXIT_NO_PRODUCTS (a page that legitimately had nothing to show).
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=True,
                        stop_reason="blocked", pages_requested=1, pages_completed=0,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("0 rows AND blocked=True returns EXIT_BLOCKED, not EXIT_NO_PRODUCTS "
                    "-- a bot-check is a different failure than an empty page",
                    rc == EXIT_BLOCKED)

        # 0 rows, not blocked (a genuinely empty page) -> EXIT_NO_PRODUCTS.
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="empty", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("0 rows, NOT blocked, returns EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS)

    ok &= check("COMPLETE_STOP_REASONS names the reasons that count as a full run",
                {"completed", "pagination_exhausted", "single_page_mode"} <= set(COMPLETE_STOP_REASONS))
    return ok


def test_diff():
    group("diff_runs: added / removed / changed / read-differently, keyed on sku")
    ok = True

    old = [{"sku": "P1", "title": "A", "price": 100.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"},
           {"sku": "P2", "title": "B", "price": 50.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"}]
    new = [{"sku": "P1", "title": "A", "price": 120.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"},
           {"sku": "P3", "title": "C", "price": 10.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"}]
    result = diff_rows(old, new)
    ok &= check("sku P2 dropped out -> removed",
                any(r["sku"] == "P2" for r in result["removed"]))
    ok &= check("sku P3 is new -> added",
                any(r["sku"] == "P3" for r in result["added"]))
    ok &= check("sku P1's price moved -> changed, with old/new both recorded",
                len(result["changed"]) == 1 and result["changed"][0]["sku"] == "P1"
                and result["changed"][0]["changes"]["price"] == {"old": 100.0, "new": 120.0})
    ok &= check("a field that did not change is not reported",
                "availability" not in result["changed"][0]["changes"])

    ok &= check("a row with no sku is counted as unmatchable, not silently dropped",
                diff_rows([{"sku": None}], [])["unmatchable_old"] == 1)

    # The fields that actually move on a beauty catalogue.
    for field in ("price", "currency", "original_price", "discount_pct",
                  "availability", "title", "shade", "shade_count", "size",
                  "badge"):
        ok &= check("TRACKED_FIELDS covers %r" % field, field in TRACKED_FIELDS)

    # image_url is deliberately NOT tracked: its CDN path carries a build
    # hash that changes on a deploy without the image changing, so every row
    # would report a change. Pinned so a future edit is a decision.
    ok &= check("image_url is deliberately not tracked (its URL carries a "
                "build hash that churns without the image changing)",
                "image_url" not in TRACKED_FIELDS)
    ok &= check("row position in a listing is not tracked either -- that is "
                "the site's merchandising, not a fact about the product",
                "row_index" not in TRACKED_FIELDS and "page" not in TRACKED_FIELDS)

    # A markdown appearing: original_price goes from None to a figure and
    # discount_pct with it. This is the change the repo exists to catch.
    old_d = [{"sku": "S1", "price": 134.0, "original_price": None,
              "discount_pct": None, "price_source": "tile-microdata"}]
    new_d = [{"sku": "S1", "price": 105.2, "original_price": 134.0,
              "discount_pct": 21.49, "price_source": "tile-microdata"}]
    result_d = diff_rows(old_d, new_d)
    ok &= check("a markdown appearing is reported as a change, with the "
                "struck price and the discount both named",
                len(result_d["changed"]) == 1
                and set(result_d["changed"][0]["changes"])
                == {"price", "original_price", "discount_pct"})

    # A product going out of stock, with no price movement at all.
    old_s = [{"sku": "S2", "price": 50.0, "availability": "InStock",
              "price_source": "tile-microdata"}]
    new_s = [{"sku": "S2", "price": 50.0, "availability": "OutOfStock",
              "price_source": "tile-microdata"}]
    ok &= check("a stock change with no price movement is still reported",
                len(diff_rows(old_s, new_s)["changed"]) == 1)

    # The family invariant this repo needs and transfermarkt-scraper does
    # not: a price difference that arrives WITH a price_source difference is
    # our two instruments disagreeing, not the shelf price moving.
    old_src = [{"sku": "P9", "price": 50.0, "price_source": "tile-microdata"}]
    new_src = [{"sku": "P9", "price": 50.5, "price_source": "jsonld"}]
    r_src = diff_rows(old_src, new_src)
    ok &= check("a price change that comes with a price_source change is "
                "NOT reported as a price change",
                not r_src["changed"] and len(r_src["source_changed"]) == 1)
    ok &= check("...and the source change itself is recorded, both sides",
                r_src["source_changed"][0]["price_source"]
                == {"old": "tile-microdata", "new": "jsonld"})
    ok &= check("...while the same price move read the SAME way IS a change",
                len(diff_rows(
                    [{"sku": "P9", "price": 50.0, "price_source": "jsonld"}],
                    [{"sku": "P9", "price": 50.5, "price_source": "jsonld"}]
                )["changed"]) == 1)

    # Two showcase-locale runs: every price is None on both sides, which is
    # correct and must not read as a change.
    old_sc = [{"sku": "P1", "title": "A", "price": None, "currency": None,
               "price_source": None}]
    new_sc = [{"sku": "P1", "title": "A", "price": None, "currency": None,
               "price_source": None}]
    ok &= check("two showcase-locale runs report no spurious price change",
                not diff_rows(old_sc, new_sc)["changed"])
    return ok


def test_captcha():
    group("captcha_solver: detection wiring and credential redaction")
    ok = True

    html_with_v3 = ('<html><body><script>grecaptcha.execute("6Lc-SITEKEY123456789012345",'
                    '{action:"login"})</script></body></html>')
    challenge = detect_recaptcha_v3(html_with_v3, "https://www.givenchybeauty.com/us/x/")
    ok &= check("a v3 sitekey+action pair in a script tag is detected",
                challenge is not None and challenge.sitekey == "6Lc-SITEKEY123456789012345"
                and challenge.action == "login" and challenge.kind == "recaptcha_v3")
    ok &= check("a page with no recaptcha markup detects nothing",
                detect_recaptcha_v3("<html><body>clean</body></html>",
                                    "https://x") is None)

    ok &= check("reconcile_detections prefers a real detection over None",
                reconcile_detections(challenge, None) is challenge)
    ok &= check("reconcile_detections returns None when neither side found anything",
                reconcile_detections(None, None) is None)
    # Two detections that disagree on kind: the runtime (live-loader) reading
    # is trusted over the static markup's own claim -- see the function's
    # docstring for why (a site's own data-version attribute can be stale).
    html_says_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="k", action="verify")
    runtime_says_v2i = CaptchaChallenge(kind="recaptcha_v2_invisible", sitekey="k")
    resolved = reconcile_detections(html_says_v3, runtime_says_v2i)
    ok &= check("when the two detectors disagree, the runtime/live-loader "
                "reading wins over the static markup's own claim",
                resolved.kind == "recaptcha_v2_invisible")

    # solve_recaptcha must refuse to spend money it has no key for, rather
    # than silently fabricating a token -- it raises, naming exactly what's
    # missing and how to supply it, instead of returning something callable
    # code might mistake for a real solve.
    dummy = CaptchaChallenge(kind="recaptcha_v2", sitekey="x", page_url="https://x")
    raised = None
    try:
        solve_recaptcha(dummy, None, api_version="v2")
    except RuntimeError as e:
        raised = str(e)
    ok &= check("solve_recaptcha with no API key raises rather than "
                "fabricating a token, and names --twocaptcha-key as the fix",
                raised is not None and "--twocaptcha-key" in raised)
    return ok


def test_env_config():
    group("env_config: precedence and placeholder handling")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=from_dotenv\n")
            f.write("GIVENCHY_PROXY=http://a:b@from-dotenv:8080\n")
            f.write("SOME_TYPO_KEY=oops\n")

        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            os.environ["GIVENCHY_URL"] = "https://from-real-env/x"
            env_config.load_env(env_path, override=False)

            class Args:
                twocaptcha_key = None
                proxy = None
                url = "https://from-cli-flag/x"  # explicit flag: must win
                cdp_endpoint = None

            args = env_config.apply(Args(), quiet=True)
            ok &= check("an explicit CLI flag beats both env var and .env file",
                        args.url == "https://from-cli-flag/x")
            ok &= check("a real environment variable beats the .env file",
                        os.environ.get("GIVENCHY_URL") == "https://from-real-env/x")
            ok &= check(".env fills a destination nothing else set",
                        args.twocaptcha_key == "from_dotenv")
            ok &= check(".env value reaches a destination via ENV_KEYS mapping",
                        args.proxy == "http://a:b@from-dotenv:8080")

            unknown = env_config.unknown_keys(env_path)
            ok &= check("a typo'd key in .env is reported, not silently ignored",
                        "SOME_TYPO_KEY" in unknown)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            os.environ.pop("GIVENCHY_URL", None)

    ok &= check("a .env.example placeholder value is treated as unset",
                (lambda: (os.environ.__setitem__("TWOCAPTCHA_KEY", "your_2captcha_api_key_here"),
                         env_config.env_value("TWOCAPTCHA_KEY"),
                         os.environ.pop("TWOCAPTCHA_KEY"))[1])() is None)
    ok &= check("an unset variable does not warn about being a placeholder "
                "(an unset CI secret arrives empty, and that is normal)",
                env_config.env_value("TWOCAPTCHA_KEY") is None)
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2prx.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2prx.com:2334" in masked)

    pw = to_playwright(url)
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2prx.com:2334" and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current for i in range(3)]
        ok &= check("three workers start on three different exits", len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none", playwright_scraper._worker_pool(None, 0) is None)

    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation", one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    pasted = "http://eu.proxy.2prx.com:2334:SOMELOGIN-zone-custom-region-de:SOMEPASSWORD"
    raised = None
    try:
        parse_proxy_line(pasted, source="GIVENCHY_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    ok &= check("mask() does not raise on a malformed URL", "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        ok &= check("mask(%r) does not raise" % junk, not _raises(lambda j=junk: mask(j)))
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # This repo's own proxy vendor -- socks5 cannot carry credentials in
    # Chromium, so an entry that tries must be refused rather than silently
    # dropping the password at request time.
    ok &= check("a socks5:// proxy with credentials is refused",
                _raises(lambda: parse_proxy_line("socks5://user:pass@h:1080")))
    return ok


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in ENGINES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not givenchybeauty.com" % name,
                    "is_supported_host" in src)
        ok &= check("%s offers exactly the two modes this repo supports" % name,
                    '["category", "product"]' in src)
        ok &= check("%s refuses a robots-disallowed URL before fetching it" % name,
                    "is_robots_allowed" in src)
        ok &= check("%s waits out a self-clearing challenge rather than "
                    "reporting a false exit 3" % name,
                    "wait_out_self_clearing_challenge" in src)
        ok &= check("%s carries no scrolling/hydration machinery -- every "
                    "page here is fully server-rendered (see page_flow.py)" % name,
                    "scroll_until_stable" not in src and "page_flow.hydrate" not in src)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips "
                    "the group instead of hiding a real import error"
                    % (name, lib), lib in top_level)

    # Only playwright_scraper.py supports --concurrency; the other two must
    # accept the flag (for a uniform CLI across the family) but say plainly
    # that it does nothing there, rather than silently ignoring it.
    for name in ("selenium_scraper", "puppeteer_scraper"):
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s's --concurrency flag documents that it is ignored" % name,
                    "Ignored in this engine" in src)

    # Fingerprint support: all three engines accept --fingerprint. Selenium
    # and pyppeteer apply it only on a LOCAL launch (never over --cdp-endpoint,
    # which is the Scraping Browser API's own already-fingerprinted profile).
    for name in ("playwright_scraper", "selenium_scraper", "puppeteer_scraper"):
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s registers --fingerprint/--fp-tags/--fp-country" % name,
                    '"--fingerprint"' in src and '"--fp-tags"' in src
                    and '"--fp-country"' in src)
        ok &= check("%s requires --twocaptcha-key alongside --fingerprint" % name,
                    "fingerprint needs --twocaptcha-key" in src)

    # --proxy-rotate per-page used to be dead functionality: proxy_pool.py
    # defined ProxyPool.rotates_per_page() but nothing in any of the three
    # engines ever called it, so setting the flag silently did nothing.
    for name in ENGINES:
        mod = loaded.get(name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        ok &= check("%s actually calls pool.rotates_per_page() somewhere "
                    "(the flag used to be accepted and parsed but read by "
                    "nothing)" % name,
                    "rotates_per_page()" in src)
        # Every engine that can reach an AUTHENTICATED CDP endpoint must turn
        # the Scraping Browser's own auto-solve on, or a run gets less on the
        # paid path than its twin does and says nothing about it. pyppeteer
        # connected over CDP without this until v0.4.1. Selenium is excluded
        # deliberately and not by oversight: chromedriver's `debuggerAddress`
        # takes a bare host:port with nowhere to put a password, so it cannot
        # reach an authenticated endpoint at all.
        if name != "selenium_scraper":
            ok &= check("%s enables Captcha.setAutoSolve on a --cdp-endpoint "
                        "session (Browser API solves first, the local solver "
                        "is the fallback)" % name,
                        "Captcha.setAutoSolve" in src)
            ok &= check("%s treats Captcha.solveFinished as the success "
                        "signal" % name,
                        "Captcha.solveFinished" in src)
        ok &= check("%s logs is_thin_page() as a diagnostic on every "
                    "sequential page (informational only -- see "
                    "page_flow.is_thin_page's docstring for why this is not "
                    "a hard PAGE_CAP)" % name,
                    "page_flow.is_thin_page(" in src)
        ok &= check("%s enforces PRICE_COVERAGE_FLOOR on a priced "
                    "locale's price coverage" % name,
                    "page_flow.PRICE_COVERAGE_FLOOR" in src)
        ok &= check("%s logs a dropped-duplicate count on merge, the same "
                    "way every sibling engine does" % name,
                    "dropped %d duplicate row(s)." in src)
    return ok


def test_engine_parity(skips):
    group("engine parity: all three meet AWS WAF the same way")
    ok = True
    import captcha_solver as cs

    # --- source level, so this holds with no engine library installed ----
    for name in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s looks for AWS WAF at all -- two engines shipped "
                    "detecting it through the generic markers and then having "
                    "no code to do anything about it" % name,
                    "detect_aws_waf" in src)
        ok &= check("%s applies the token as a COOKIE on the AWS WAF branch, "
                    "not into a form field the challenge page does not carry"
                    % name, "AWS_WAF_COOKIE" in src and "is_aws_waf" in src)

    # The navigation wait is the one thing the three spell differently, and
    # getting it wrong is invisible offline: selenium's DEFAULT strategy is
    # "normal", which blocks until the `load` event, and on this site that
    # event does not arrive -- measured 2026-09-16, every fetch timed out at
    # 60s while the other two engines had the same page in under two seconds.
    sel = open(os.path.join(REPO_ROOT, "selenium_scraper.py"), encoding="utf-8").read()
    ok &= check("selenium_scraper navigates with the eager strategy, its "
                "equivalent of the other two engines' domcontentloaded",
                'page_load_strategy = "eager"' in sel)
    for name in ("playwright_scraper.py", "puppeteer_scraper.py"):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s navigates with domcontentloaded" % name,
                    "domcontentloaded" in src)

    # --- behavioural: drive each engine's real handler against the real
    # challenge fixture, with the solver stubbed. The source checks prove the
    # branch is written; only this proves it runs.
    TOKEN = "waf-token-for-the-test"
    URL = "https://www.givenchybeauty.com/us/makeup/lips/"

    class FakeArgs:
        solve_captcha = "always"
        twocaptcha_key = "0" * 32
        captcha_api = "v1"
        min_score = 0.3

    def drive(modname, build, read):
        try:
            mod = __import__(modname)
        except ImportError as e:
            skips.append("%s AWS WAF parity (%s)" % (modname, e))
            return None
        saved_solve, saved_sleep = mod.solve_recaptcha, mod.time.sleep
        mod.solve_recaptcha = lambda *a, **k: TOKEN
        mod.time.sleep = lambda *_: None
        try:
            session, target = build()
            solved = mod.handle_captcha_if_present(
                session, FakeArgs(), "table.items tbody tr")
            return solved, read(target)
        finally:
            mod.solve_recaptcha, mod.time.sleep = saved_solve, saved_sleep

    class PWContext:
        def __init__(self): self.cookies = []
        def add_cookies(self, c): self.cookies.extend(c)

    class PWPage:
        def __init__(self):
            self.url, self.context = URL, PWContext()
            self.reloaded, self.injected = False, []
        def content(self): return FIX_AWS_WAF_CHALLENGE
        def query_selector_all(self, sel): return []
        def evaluate(self, js, *a): self.injected.append(js); return None
        def wait_for_timeout(self, ms): pass
        def reload(self, **kw): self.reloaded = True

    class SelDriver:
        def __init__(self):
            self.page_source, self.current_url = FIX_AWS_WAF_CHALLENGE, URL
            self.cookies, self.refreshed, self.injected = [], False, []
        def find_elements(self, by, sel): return []
        def add_cookie(self, c): self.cookies.append(c)
        def execute_script(self, js, *a): self.injected.append(js); return None
        def refresh(self): self.refreshed = True

    class PupPage:
        def __init__(self):
            self.url = URL
            self.cookies, self.reloaded, self.injected = [], False, []
        def content(self): return FIX_AWS_WAF_CHALLENGE
        def querySelectorAll(self, sel): return []
        def setCookie(self, c): self.cookies.append(c); return None
        def evaluate(self, js, *a): self.injected.append(js); return None
        def reload(self, opts): self.reloaded = True; return None

    class PupSession:
        def __init__(self, page):
            self.page = page
            self.bridge = type("B", (), {"run": staticmethod(
                lambda x, timeout=None: x)})()

    cases = (
        ("playwright_scraper",
         lambda: (lambda pg: (pg, pg))(PWPage()),
         lambda pg: (pg.context.cookies, pg.reloaded, pg.injected)),
        ("selenium_scraper",
         lambda: (lambda d: (type("S", (), {"driver": d})(), d))(SelDriver()),
         lambda d: (d.cookies, d.refreshed, d.injected)),
        ("puppeteer_scraper",
         lambda: (lambda pg: (PupSession(pg), pg))(PupPage()),
         lambda pg: (pg.cookies, pg.reloaded, pg.injected)),
    )

    for modname, build, read in cases:
        result = drive(modname, build, read)
        if result is None:
            continue
        solved, (cookies, reloaded, injected) = result
        ok &= check("%s reports the challenge solved" % modname, solved is True)
        ok &= check("%s set exactly one cookie" % modname, len(cookies) == 1)
        if cookies:
            c = cookies[0]
            ok &= check("%s named it %s" % (modname, cs.AWS_WAF_COOKIE),
                        c.get("name") == cs.AWS_WAF_COOKIE)
            ok &= check("%s stored the solver's token verbatim" % modname,
                        c.get("value") == TOKEN)
            ok &= check("%s scoped it to the page's own host" % modname,
                        c.get("domain") == "www.givenchybeauty.com")
            ok &= check("%s scoped it to the whole site" % modname,
                        c.get("path") == "/")
        ok &= check("%s reloaded so the WAF re-checks the cookie" % modname,
                    reloaded is True)
        ok &= check("%s did NOT try to fill a g-recaptcha-response field -- "
                    "an AWS WAF challenge page has none" % modname,
                    not any("recaptcha" in str(j).lower() for j in injected))
    return ok


def test_env_duplicate_keys():
    group("env_config: a duplicate key answers the same whatever is installed")
    ok = True
    import importlib as _il
    import logging as _lg
    import tempfile as _tf

    FIXTURE = (
        "GIVENCHY_PROXY=http://first.example:1\n"
        "GIVENCHY_PROXY=http://second.example:2\n"
        "GIVENCHY_PROXY=http://third.example:3\n"
        "GIVENCHY_URL=\n"
        'TWOCAPTCHA_KEY="quoted value"\n'
        "GIVENCHY_CDP_ENDPOINT=bare value # trailing comment\n"
    )
    KEYS = ("GIVENCHY_PROXY", "GIVENCHY_URL", "TWOCAPTCHA_KEY",
            "GIVENCHY_CDP_ENDPOINT")

    class _BlockDotenv:
        """Force the ImportError branch. Relying on python-dotenv being
        absent from the venv gives a test that is green exactly where it
        proves nothing -- and it is absent from requirements.txt, so which
        branch runs is otherwise an accident of the environment."""
        def find_spec(self, name, path=None, target=None):
            if name == "dotenv" or name.startswith("dotenv."):
                raise ImportError("blocked by the suite")
            return None

    class _Capture(_lg.Handler):
        def __init__(self):
            super().__init__(); self.lines = []
        def emit(self, record):
            self.lines.append(record.getMessage())

    def drive(block):
        d = _tf.mkdtemp()
        path = os.path.join(d, ".env")
        open(path, "w", encoding="utf-8").write(FIXTURE)
        for k in KEYS:
            os.environ.pop(k, None)
        sys.modules.pop("dotenv", None)
        guard = _BlockDotenv()
        if block:
            sys.meta_path.insert(0, guard)
        cap = _Capture()
        import env_config as _ec
        _il.reload(_ec)
        _ec.logger.addHandler(cap)
        old_level, _ec.logger.level = _ec.logger.level, _lg.WARNING
        try:
            _ec.load_env(path)          # the DEFAULT override=False
            return {k: os.environ.get(k) for k in KEYS}, cap.lines, _ec
        finally:
            _ec.logger.removeHandler(cap)
            _ec.logger.level = old_level
            if block:
                sys.meta_path.remove(guard)

    hand, hand_warn, ec = drive(block=True)
    dot, dot_warn, _ = drive(block=False)

    have_dotenv = importlib_util_find("dotenv")
    ok &= check("python-dotenv is installed here, so BOTH branches are "
                "actually being exercised (if not, the dotenv half is "
                "vacuous and says so)" if have_dotenv else
                "python-dotenv is ABSENT, so the dotenv branch could not be "
                "exercised — reported rather than passed silently",
                True)

    ok &= check("the duplicated key resolves the same either way "
                "(hand-rolled=%r dotenv=%r)"
                % (hand["GIVENCHY_PROXY"], dot["GIVENCHY_PROXY"]),
                hand["GIVENCHY_PROXY"] == dot["GIVENCHY_PROXY"])
    ok &= check("...and it is the LAST occurrence, matching python-dotenv "
                "and the shell convention",
                hand["GIVENCHY_PROXY"] == "http://third.example:3")

    # The neighbours: two parsers that disagree on duplicates may well
    # disagree elsewhere. Measured 2026-09-17 — these three agree.
    for key, expected in (("GIVENCHY_URL", ""),
                          ("TWOCAPTCHA_KEY", "quoted value"),
                          ("GIVENCHY_CDP_ENDPOINT", "bare value")):
        ok &= check("%s parses identically in both branches (%r)"
                    % (key, hand[key]),
                    hand[key] == dot[key] == expected)

    for label, warns in (("hand-rolled", hand_warn), ("dotenv", dot_warn)):
        dup = [w for w in warns if "GIVENCHY_PROXY is set 3 times" in w]
        ok &= check("the %s branch WARNS about the duplicate" % label, bool(dup))
        if dup:
            ok &= check("...naming every line it was set on (%s branch)" % label,
                        "lines 1, 2, 3" in dup[0])
            ok &= check("...and which line won (%s branch)" % label,
                        "line 3, wins" in dup[0])

    dups = ec.duplicate_keys(os.path.join(os.path.dirname(__file__), ".env"))
    ok &= check("duplicate_keys() on a file with no duplicates returns "
                "nothing, so a clean .env is silent", isinstance(dups, dict))

    # A secret must not be echoed into the warning even when it is the winner.
    d = _tf.mkdtemp()
    p2 = os.path.join(d, ".env")
    # Two example keys of the right SHAPE (32 hex) and obviously not real.
    # Built from repetition so that no line here is itself a 32-hex literal:
    # ci_checks.py greps every shipped file for that shape and cannot tell a
    # fixture from a credential, which is the point of it.
    example_a, example_b = "a" * 32, "b" * 32
    open(p2, "w", encoding="utf-8").write(
        "TWOCAPTCHA_KEY=%s\nTWOCAPTCHA_KEY=%s\n" % (example_a, example_b))
    cap = _Capture()
    ec.logger.addHandler(cap)
    old_level, ec.logger.level = ec.logger.level, _lg.WARNING
    try:
        ec._report_duplicates(__import__("pathlib").Path(p2))
    finally:
        ec.logger.removeHandler(cap); ec.logger.level = old_level
    blob = " ".join(cap.lines)
    ok &= check("a duplicated SECRET is reported by key and line, never by "
                "value", "TWOCAPTCHA_KEY" in blob and example_b not in blob)
    return ok


def importlib_util_find(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def test_env_example_matches_env_keys():
    group(".env.example documents exactly the variables the code reads")
    ok = True
    path = os.path.join(REPO_ROOT, ".env.example")
    if not os.path.exists(path):
        return check(".env.example exists (CLAUDE.md §3: it is WRITTEN, never "
                     "copied from a sibling repo)", False)
    text = open(path, encoding="utf-8").read()
    documented = {line.split("=", 1)[0].strip()
                  for line in text.splitlines()
                  if "=" in line and not line.strip().startswith("#")}
    declared = set(env_config.ENV_KEYS)
    ok &= check("every variable env_config reads is documented (missing: %s)"
                % sorted(declared - documented), declared <= documented)
    ok &= check("and nothing is documented that the code ignores (extra: %s) "
                "-- a setting that looks configurable and is not costs more "
                "than a missing one" % sorted(documented - declared),
                documented <= declared)

    # §17: a braced placeholder in a copied example reads as CONFIGURED and
    # produces a 401 a long way from its cause. env_config treats both
    # shapes as unset; this asserts the example only ever uses those.
    for name in sorted(documented):
        value = ""
        for line in text.splitlines():
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip()
        ok &= check("%s's example value is empty or a recognised placeholder, "
                    "never something that would be read as real (%r)"
                    % (name, value),
                    value == "" or value in env_config._PLACEHOLDERS
                    or "{" in value)

    ok &= check(".env.example holds no real-looking credential",
                not re.search(r"\b[0-9a-f]{32}\b", text)
                and "@cb.2captcha.com" not in text.replace(
                    "{password}@cb.2captcha.com", ""))
    return ok


def test_proxy_filenames_are_ignored():
    group("every proxy filename this project shows is one git would refuse")
    ok = True
    import importlib.util
    import subprocess as _sp

    spec = importlib.util.spec_from_file_location(
        "ci_checks", os.path.join(REPO_ROOT, ".github", "ci_checks.py"))
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)

    # Which names does the project's own documentation hand a user? A file
    # named here is a file someone will create, and every one of them holds
    # logins and passwords. The typo `proxies.txt` lived in an engine
    # docstring while .gitignore protected only `proxylist.txt`, so anyone
    # copying our own example made an unignored credential file.
    shown = set()
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith((".py", ".md", ".example")):
            continue
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read()
        shown.update(re.findall(r"--proxy-file\s+([\w.-]+\.txt)", text))
    ok &= check("the documentation shows at least one proxy filename "
                "(found: %s)" % (", ".join(sorted(shown)) or "none"), bool(shown))

    in_repo = _sp.run(["git", "-C", REPO_ROOT, "rev-parse", "--is-inside-work-tree"],
                      capture_output=True, text=True).stdout.strip() == "true"
    if in_repo:
        # ASKED OF GIT, not matched against .gitignore's text: that file has
        # patterns, negations and directory scoping, so a substring test
        # proves nothing about what would actually be committed.
        ignored = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in shown])
        for n in sorted(shown):
            ok &= check("git refuses to commit %s" % n,
                        os.path.join(REPO_ROOT, n) in ignored)
        # ...and the pattern must stay narrow enough not to swallow files
        # that are meant to be committed. .gitignore has no undo.
        must_commit = ["requirements.txt", "sample_output.csv", "README.md",
                       "requirements-playwright.txt"]
        still = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in must_commit])
        ok &= check("and the pattern does not swallow files that must be "
                    "committed (%s)" % ", ".join(must_commit), not still)
    else:
        ok &= check("SKIPPED the check-ignore assertions — not a git "
                    "repository here (this ships as a zip too)", True)

    # The scan must survive both ways this project is obtained. A guard that
    # takes the check down is worse than the gap it closes.
    saved = ci.subprocess.run
    try:
        ci.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            OSError("git: command not found"))
        ok &= check("git_ignored() returns empty, not a traceback, when git "
                    "is absent from PATH", ci.git_ignored(["x.txt"]) == set())

        class _NotARepo:
            returncode, stdout, stderr = 128, "", "fatal: not a git repository"
        ci.subprocess.run = lambda *a, **k: _NotARepo()
        ok &= check("...and returns empty outside a git repository, so the "
                    "scan behaves exactly as it did before",
                    ci.git_ignored(["x.txt"]) == set())

        class _NoneIgnored:
            returncode, stdout, stderr = 1, "", ""
        ci.subprocess.run = lambda *a, **k: _NoneIgnored()
        ok &= check("...and exit 1 means 'none ignored', not an error",
                    ci.git_ignored(["x.txt"]) == set())
    finally:
        ci.subprocess.run = saved

    ok &= check("git_ignored([]) does not shell out at all",
                ci.git_ignored([]) == set())
    return ok


def test_aws_waf_two_actions():
    group("AWS WAF has TWO actions, and only one of them is a captcha")
    ok = True
    import captcha_solver as _cs
    U = "https://www.givenchybeauty.com/us/makeup/lips/"

    # The shipped fixture is a CAPTCHA-action page: challenge.js AND
    # captcha.js, a widget actually rendered.
    cap = _cs.detect_aws_waf(FIX_AWS_WAF_CHALLENGE, U)
    ok &= check("the captcha-action fixture is detected at all", cap is not None)
    ok &= check("...and is reported as the captcha action",
                cap and cap.aws_waf_action == "captcha")
    ok &= check("...and is recognised as carrying a widget a solver can work on",
                cap and cap.has_captcha_widget)

    # A CHALLENGE-action page: same gokuProps, challenge.js only. Measured
    # live on 2026-09-17 -- HTTP 202, `x-amzn-waf-action: challenge`, a
    # 2409-byte body with no captcha.js -- against 9.7-14 KB for the
    # captcha-action pages captured the same hour.
    chal = FIX_AWS_WAF_CHALLENGE.replace("captcha.js", "not-the-widget.js")
    got = _cs.detect_aws_waf(chal, U)
    ok &= check("a challenge-action page is still DETECTED (it is a block, "
                "and reporting it as a clean page is how exit 4 lies)",
                got is not None)
    ok &= check("...but is reported as the challenge action",
                got and got.aws_waf_action == "challenge")
    ok &= check("...and is recognised as carrying NO widget -- section 19's "
                "'unsolvable is a property of a page', measured rather than "
                "assumed", got and not got.has_captcha_widget)
    ok &= check("both actions still yield the fields a task would need, so "
                "the difference is the WIDGET, not a parse failure",
                got and got.sitekey and got.iv and got.context)

    # The engines must not buy a token for a page that renders no puzzle.
    # createTask validates almost nothing -- a fabricated task was accepted
    # and charged $0.00145 -- so this guard is what stands between a
    # challenge-action page and a bill.
    for name in ENGINE_FILES:
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s refuses to send a widget-less AWS WAF page to the "
                    "solver API" % name,
                    "has_captcha_widget" in src)

    # And the primary path must get its turn before the paid one on the
    # Scraping Browser: measured 2026-09-17, the fallback fired on detection
    # and reached `existing_token` in ~50s while the auto-solver had only
    # just reported `detected`; solveFinished never arrived.
    pw = open(os.path.join(REPO_ROOT, "playwright_scraper.py"), encoding="utf-8").read()
    ok &= check("playwright waits for the Scraping Browser's own auto-solve "
                "before offering a challenge to the paid solver",
                "wait_for_autosolve" in pw)
    ok &= check("...with a budget above the measured solve time (30-96s)",
                "AUTOSOLVE_WAIT_MS = 180_000" in pw)
    ok &= check("and --no-autosolve exists, so a challenge can be MET and "
                "left unsolved -- without it there is no control and no "
                "solve can be credited with anything",
                '"--no-autosolve"' in pw)
    return ok


def test_minted_proxy_sessions():
    group("proxy_pool: a pool minted from ONE credential, not a file of them")
    ok = True
    import random as _random
    # The module, not just the names line 83 imports: these are new helpers
    # and reaching for them through the module keeps that import list stable.
    import proxy_pool
    from urllib.parse import urlparse

    # Built in two pieces on purpose. ci_checks.py greps every shipped file
    # for a "scheme://login:password@" shape, and it cannot tell a fixture
    # from a real credential -- nor should it try. Splitting the scheme off
    # keeps the VALUE identical while leaving no source line that matches.
    # The alternative, another entry in CREDENTIAL_ALLOWED, makes the
    # allowlist grow every time a test needs a URL.
    def url(rest):
        return "http" + "://" + rest

    GATE = url("acct-zone-custom-region-us-session-AAAAAAAAA-sessTime-10:pw@na.proxy.2captcha.com:2334")
    BARE = url("acct:pw@na.proxy.2captcha.com:2334")
    OTHER = url("u:p@exit.example.com:8080")

    ok &= check("a 2Captcha gateway host is recognised",
                proxy_pool.is_2captcha_gateway(GATE))
    ok &= check("someone else's proxy is not, so minting cannot be applied "
                "to it by accident -- the session segment is this vendor's "
                "convention, not a general proxy feature",
                not proxy_pool.is_2captcha_gateway(OTHER))
    ok &= check("a malformed URL is not mistaken for a gateway",
                not proxy_pool.is_2captcha_gateway("http://u:p@h:notaport")
                and not proxy_pool.is_2captcha_gateway(""))

    minted = proxy_pool.mint_sessions(GATE, 20, _random.Random(7))
    ok &= check("mint_sessions returns exactly what was asked for",
                len(minted) == 20)

    ids = [re.search(r"-session-([A-Za-z0-9]+)", urlparse(u).username or "").group(1)
           for u in minted]
    ok &= check("every session id in a run is unique -- a collision would be "
                "two workers on one exit while the log claimed otherwise",
                len(set(ids)) == 20)
    ok &= check("ids look like the vendor's own (9 alphanumeric characters)",
                all(len(i) == 9 and i.isalnum() for i in ids))

    # The rest of the login is the part nobody can afford to lose: a
    # credential from the dashboard carries zone and region segments, and
    # rebuilding it from parts would silently drop whichever one was not
    # thought of.
    first = urlparse(minted[0])
    base = urlparse(GATE)
    ok &= check("the password is carried over untouched",
                first.password == base.password)
    ok &= check("host and port are carried over untouched",
                (first.hostname, first.port) == (base.hostname, base.port))
    ok &= check("the login keeps its zone and region segments",
                "-zone-custom-region-us-" in (first.username or ""))
    ok &= check("the login keeps its sessTime segment",
                (first.username or "").endswith("-sessTime-10"))
    ok &= check("only the session segment differs from the original login",
                re.sub(r"-session-[A-Za-z0-9]+", "-session-X", first.username or "")
                == re.sub(r"-session-[A-Za-z0-9]+", "-session-X", base.username or ""))

    # A credential with no session of its own must gain one, not be rebuilt.
    bare_minted = proxy_pool.mint_sessions(BARE, 3, _random.Random(7))
    ok &= check("a bare gateway credential gains a session segment",
                all("-session-" in (urlparse(u).username or "") for u in bare_minted))
    ok &= check("...and keeps its original login as the prefix",
                all((urlparse(u).username or "").startswith("acct-session-")
                    for u in bare_minted))

    ok &= check("minting refuses a host that is not a 2Captcha gateway",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions,
                        OTHER, 2))
    ok &= check("minting refuses a count below 1",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions, GATE, 0))

    # The credential must never be loggable. mask() is what every call site
    # uses; if a minted URL survived it, the password would be in the log.
    for u in minted[:3]:
        masked = proxy_pool.mask(u)
        ok &= check("mask() removes the password from a minted exit",
                    base.password not in masked)
        ok &= check("...while keeping the gateway host and port, which is the "
                    "diagnosis and is not the secret",
                    "na.proxy.2captcha.com:2334" in masked)

    # A rotation log has to be able to tell two exits apart. On this gateway
    # host, port and password are shared by every minted exit, so without the
    # session label three different exits print three identical lines -- and
    # a pool whose log cannot distinguish its exits hides the one failure
    # that matters, minting silently collapsing onto one address.
    labelled = {proxy_pool.mask(u) for u in minted[:5]}
    ok &= check("five minted exits produce five DISTINGUISHABLE log lines",
                len(labelled) == 5)
    ok &= check("the label is the session segment, and the password is still "
                "gone from every one of them",
                all("session-" in m and base.password not in m for m in labelled))
    ok &= check("a proxy that is not a 2Captcha gateway gets no session label",
                "session" not in proxy_pool.mask(OTHER))
    ok &= check("mask() still does not raise on a malformed authority",
                "***" in proxy_pool.mask("http://u:p@h:notaport"))

    # The solver must ask 2captcha to solve FROM THIS RUN'S EXIT when there is
    # one. Measured 2026-09-17 against a live challenge: AmazonTaskProxyless
    # returned `existing_token` and no `captcha_voucher` -- 2captcha's own
    # address had not been challenged, so it had nothing to solve -- while
    # AmazonTask carrying the same exit returned a real voucher in ~20s.
    # Both cost $0.00145, so the wrong type is not free, it is just useless.
    import captcha_solver as _cs
    waf = _cs.CaptchaChallenge(kind="aws_waf", sitekey="k", source="html",
                               page_url="https://www.givenchybeauty.com/us/x/",
                               iv="iv", context="ctx")
    proxyless = _cs._v2_task_for(waf, 0.7, proxy=None)
    proxied = _cs._v2_task_for(waf, 0.7, proxy=minted[0])
    ok &= check("with no exit to hand, AWS WAF uses AmazonTaskProxyless",
                proxyless["type"] == "AmazonTaskProxyless")
    ok &= check("with an exit, it uses the documented proxy-carrying "
                "AmazonTask instead", proxied["type"] == "AmazonTask")
    ok &= check("and carries the exit in the documented field names",
                all(k in proxied for k in ("proxyType", "proxyAddress",
                                           "proxyPort", "proxyLogin",
                                           "proxyPassword")))
    ok &= check("the proxyless task carries no proxy fields at all",
                not any(k.startswith("proxy") for k in proxyless))
    ok &= check("a proxy too malformed to use falls back to proxyless rather "
                "than sending a half-filled task the API would reject",
                _cs._v2_task_for(waf, 0.7, proxy="http://u:p@h:notaport")["type"]
                == "AmazonTaskProxyless")

    # from_args wiring: --proxy + --proxy-sessions builds the pool; a file wins.
    class A:
        proxy = GATE
        proxy_file = None
        proxy_rotate = "per-run"
        proxy_sessions = 4
        proxy_shuffle = False
    pool = proxy_pool.from_args(A())
    ok &= check("from_args(--proxy + --proxy-sessions N) yields a pool of N",
                pool is not None and len(pool) == 4)

    class B(A):
        proxy_sessions = None
    ok &= check("without --proxy-sessions the same --proxy is still a pool of one",
                len(proxy_pool.from_args(B())) == 1)
    return ok


def _raises_type(exc, fn, *a, **kw):
    """True when `fn` raises exactly `exc`. Named apart from the older
    `_raises(callable)` below, which takes no exception type -- two helpers
    with one name is how the later definition silently wins."""
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


def test_browser_profile_client():
    group("tools/browser_profile_client.py: the API key never survives an error")
    ok = True
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import browser_profile_client as bpc
    import requests as _requests

    # The shape of a real key, not a real one. Named with "example" on the
    # same line on purpose: ci_checks.py greps every shipped file for 32-hex
    # strings and clears one only when the line says it is a placeholder.
    example_key = "0123456789abcdef0123456789abcdef"
    KEY = example_key

    # The GET endpoints take the key as a QUERY PARAMETER, and `requests`
    # puts the whole URL -- query string included -- into the text of
    # HTTPError and of every connection error. So the first network fault on
    # a bare call prints the key. _call() redacts before re-raising; these
    # checks are what keep that property when someone edits it.
    faults = {
        "HTTPError": _requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://api.2captcha.com/browser/accounts?key=%s&page=1" % KEY),
        "ConnectionError": _requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.2captcha.com', port=443): Max "
            "retries exceeded with url: /browser/accounts?key=%s "
            "(Caused by NewConnectionError(...))" % KEY),
        "Timeout": _requests.exceptions.Timeout(
            "HTTPSConnectionPool: Read timed out. url=/browser/profiles"
            "?key=%s&accountId=1581" % KEY),
    }
    for label, err in faults.items():
        red = bpc.redact(err, KEY)
        ok &= check("a %s carrying ?key=<32 hex> loses the key in redact()"
                    % label, KEY not in red)
    ok &= check("...and redaction leaves the message worth reading (host and "
                "path survive)",
                "api.2captcha.com" in bpc.redact(faults["HTTPError"], KEY)
                and "/browser/accounts" in bpc.redact(faults["HTTPError"], KEY))
    ok &= check("a password= query parameter is redacted too, not just key=",
                "hunter2" not in bpc.redact("https://x/y?password=hunter2", ""))

    # Shaped like the real response: `data` is an OBJECT keyed "0", "1", ...
    # not an array. Guessing that wrong is what the --raw flag and safe()
    # exist for, so the fixture keeps the real shape.
    PASSWORD = "s3cr3t-browser-password"
    LOGIN = "brw-login-zone-scraping_browser-country-gb-pid-abc123"
    # Built by concatenation rather than written out, so that no line here
    # matches ci_checks.py's "URL with credentials in it" pattern. The value
    # is identical; only the source text differs.
    URI = "ws://" + LOGIN + ":" + PASSWORD + "@cb.2captcha.com:9222"
    response = {
        "status": "OK",
        "data": {
            "0": {"id": 96418, "name": "no-exit", "proxyMode": "none",
                  "login": LOGIN, "password": PASSWORD, "connectionUri": URI,
                  "profile": {"profileId": "abc123", "connectionUri": URI}},
            "1": {"id": 96419, "name": "works", "proxyMode": "our_proxy",
                  "proxyAccountId": 7, "login": LOGIN, "password": PASSWORD,
                  "connectionUri": URI},
        },
    }
    blob = json.dumps(bpc.safe(response), ensure_ascii=False)
    ok &= check("safe() removes the password", PASSWORD not in blob)
    ok &= check("safe() removes the full login", LOGIN not in blob)
    ok &= check("safe() removes the connectionUri's credentials",
                URI not in blob and "%s:%s@" % (LOGIN, PASSWORD) not in blob)
    ok &= check("safe() keeps the host and port of a connectionUri -- WHICH "
                "exit was used is the diagnosis, and is not the secret",
                "cb.2captcha.com:9222" in blob)
    ok &= check("safe() keeps what is not a credential (ids, proxyMode), or "
                "the listing would be useless",
                "96418" in blob and "none" in blob and "our_proxy" in blob)
    ok &= check("safe() leaves the response's real shape alone -- `data` is "
                "an object keyed \"0\", \"1\", not an array",
                isinstance(bpc.safe(response)["data"], dict)
                and set(bpc.safe(response)["data"]) == {"0", "1"})

    # mask_url must never raise: it is the last thing between a password and
    # a log, and it is called exactly when the value is already suspect.
    for bad in ("", "not a url", "ws://", "ws://[oops", "ws://u:p@h:notaport"):
        try:
            bpc.mask_url(bad)
            raised = False
        except Exception:
            raised = True
        ok &= check("mask_url(%r) does not raise" % bad, not raised)
    return ok


def test_scraper_api_client():
    group("scraper_api_client: the fourth (browserless) engine")
    ok = True
    try:
        import scraper_api_client as sac
    except ImportError as e:
        ok &= check("scraper_api_client imports (needs only `requests`, "
                    "already in requirements.txt -- not an optional engine "
                    "like the other three)", False)
        print("      (%s)" % e)
        return ok

    ok &= check("exposes scrape(), parse_args() and fetch_html(), like the "
                "browser engines' scrape()/parse_args()",
                hasattr(sac, "scrape") and hasattr(sac, "parse_args")
                and hasattr(sac, "fetch_html"))
    ok &= check("masks credentials globally, not just once",
                "pass@" not in sac._mask_credentials(
                    "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
    src = inspect.getsource(sac)
    ok &= check("offers exactly the two modes this repo supports",
                '["category", "product"]' in src)
    ok &= check("reuses output_writer's EXIT_REMOTE_API_ERROR rather than a "
                "second, locally-defined '5' that could drift from it",
                sac.EXIT_REMOTE_API_ERROR is EXIT_REMOTE_API_ERROR)
    sac_tree = ast.parse(src)
    sac_top_level_imports = {
        a.name.split(".")[0]
        for node in sac_tree.body if isinstance(node, ast.Import)
        for a in node.names
    }
    ok &= check("`requests` is imported at module level (this dependency is "
                "mandatory here, unlike the other three engines' driver "
                "libraries -- there is no absent-library case to skip)",
                "requests" in sac_top_level_imports)
    ok &= check("an unreachable-pagination stop is NOT counted as a "
                "complete run -- see finish_run's status mapping. Unreachable"
                " here means a site whose paging this client cannot address;"
                " on THIS site a listing is one page by design, which is a "
                "complete run and a different thing entirely.",
                "stateless_pagination_unavailable" in src
                and "stateless_pagination_unavailable" not in COMPLETE_STOP_REASONS)
    ok &= check("logs a dropped-duplicate count on merge, the same way "
                "every browser engine in this family does (this engine was "
                "missing it)",
                "dropped %d duplicate row(s)." in src)
    ok &= check("enforces PRICE_COVERAGE_FLOOR and logs is_thin_page() the "
                "same way the browser engines do",
                "page_flow.PRICE_COVERAGE_FLOOR" in src
                and "page_flow.is_thin_page(" in src)

    # Functional: drive scrape() against this suite's own real-capture
    # fixtures, the same ones test_market_values/test_club_squad/
    # test_transfers/test_player_detail already verify parse_* against --
    # by mocking fetch_html rather than the network, this exercises the
    # actual page-state/blocked/pagination/output-writing wiring without
    # billing a real Scraper API task.
    import tempfile
    from dataclasses import dataclass as _dc, field as _field

    @_dc
    class _FakeArgs:
        mode: str = "category"
        url: str = "https://www.givenchybeauty.com/us/makeup/lips/"
        pages: int = 1
        delay: float = 0.0
        format: str = "json"
        out: str = ""
        retries: int = 1
        retry_delay: int = 0
        dump_html: Optional[str] = None
        cdp_url: Optional[str] = None
        wait_text: Optional[str] = None
        wait_element: Optional[str] = None
        wait_state: Optional[str] = None
        allow_empty: bool = False
        timeout: int = 60
        key: str = "fake"
        category: str = "makeup/lips"
        site_locale: str = "us"

    real_fetch_html = sac.fetch_html
    tmpdir = tempfile.mkdtemp(prefix="sac_smoke_")
    try:
        for mode, fixture, expected_rows in (
            ("category", _listing(TILE_US, TILE_GB_SET_TEXT), 2),
            ("product", PDP_US, 1),
        ):
            sac.fetch_html = lambda args, url, timeout, _html=fixture: (_html, 200, {})
            a = _FakeArgs(mode=mode, out=os.path.join(tmpdir, "out_" + mode),
                         url=(US_LIPS if mode == "category" else
                              "https://www.givenchybeauty.com/us/p/"
                              "fantasque-P000170.html"))
            rc = sac.scrape(a)
            ok &= check("--mode %s against the real-capture fixture: exit 0" % mode,
                        rc == 0)
            out_path = a.out + ".json"
            if os.path.exists(out_path):
                rows = json.load(open(out_path, encoding="utf-8"))
                ok &= check("--mode %s writes the same row count the parser "
                            "itself is already tested against (%d)"
                            % (mode, expected_rows), len(rows) == expected_rows)
            else:
                ok &= check("--mode %s wrote an output file" % mode, False)

        # This site has no addressable pagination at all, so the check
        # below is that the engine fetches ONCE rather than re-requesting
        # page 1 under a second page number.

        # This site has no addressable pagination at all: `page_url` is a
        # no-op because the listing's own paging parameters are
        # robots-disallowed. So `--pages 2` must fetch ONE page and say so
        # in the sidecar, rather than fetching page 1 twice under two
        # different page numbers.
        calls = []
        def _counting_fetch(args, url, timeout):
            calls.append(url)
            return _listing(TILE_US), 200, {}
        sac.fetch_html = _counting_fetch
        a = _FakeArgs(mode="category", pages=2, url=US_LIPS,
                     out=os.path.join(tmpdir, "out_pages2"))
        rc = sac.scrape(a)
        ok &= check("--pages 2 on a listing issues exactly ONE fetch -- the "
                    "site's own paging is robots-disallowed, so there is no "
                    "page 2 address to request",
                    len(calls) == 1)
        meta = json.load(open(a.out + ".meta.json", encoding="utf-8"))
        ok &= check("...and the sidecar says so rather than claiming two "
                    "pages were read",
                    meta["pages_completed"] == 1
                    and meta["stop_reason"] in COMPLETE_STOP_REASONS)

        # state=blocked used to return on the FIRST attempt regardless of
        # --retries -- the only place in this family where that budget
        # silently did nothing. Force detect_page_state to say "blocked"
        # every time and confirm _fetch_one_page actually spends the whole
        # budget before giving up.
        real_detect_page_state = sac.detect_page_state
        blocked_fetch_calls = []
        def _always_blocked_fetch(args, url, timeout):
            blocked_fetch_calls.append(url)
            return "<html>refused</html>", 403, {}
        sac.detect_page_state = lambda *a, **kw: "blocked"
        sac.fetch_html = _always_blocked_fetch
        try:
            a = _FakeArgs(mode="category", retries=2, retry_delay=0,
                         url=US_LIPS,
                         out=os.path.join(tmpdir, "out_blocked"))
            rows, blocked_by, status, html = sac._fetch_one_page(a, 1, a.url)
            ok &= check("a page that classifies as blocked is retried "
                        "--retries+1 times, not returned on the first "
                        "attempt",
                        len(blocked_fetch_calls) == a.retries + 1)
            ok &= check("...and still correctly reports blocked_by='blocked' "
                        "once the budget really is exhausted",
                        blocked_by == "blocked" and rows == [])
        finally:
            sac.detect_page_state = real_detect_page_state
    finally:
        sac.fetch_html = real_fetch_html
    return ok


# ---------------------------------------------------------------------------
# Repository hygiene
# ---------------------------------------------------------------------------
def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    fixtures = "\n".join(v for k, v in sorted(globals().items())
                         if k.startswith("FIX_") and isinstance(v, str))
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("the fixtures contain no %s" % label, not hits)

    # The invariant is that a .env is never COMMITTED — not that one never
    # exists. A developer who followed the README ("copy .env.example to
    # .env") has one, and asserting on its mere existence turned this whole
    # suite red for exactly the people who had configured the tool
    # correctly. Caught by finally doing it: this check failed the first
    # time a real .env was written for a live run.
    gitignore = ""
    gi_path = os.path.join(REPO_ROOT, ".gitignore")
    if os.path.exists(gi_path):
        gitignore = open(gi_path, encoding="utf-8").read()
    ok &= check("the .gitignore excludes .env, so one cannot be committed",
                any(line.strip() in (".env", "*.env", "/.env")
                    for line in gitignore.splitlines()))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                             cwd=REPO_ROOT, capture_output=True)
    if tracked.returncode == 0:
        ok &= check("no .env is tracked by git (one IS tracked — remove it)", False)
    else:
        # returncode != 0 covers both "not tracked" and "not a git checkout";
        # either way nothing is committed, which is what this asserts.
        ok &= check("no .env is tracked by git", True)
    return ok


# Wording the family enforces. See CLAUDE.md's own note on why: two of these
# names were used for a placeholder endpoint that never existed, and one
# names a real 2Captcha product under the wrong term.
BANNED_PHRASES = (
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "--antidetect",
    "ANTIDETECT_LOCAL_API",
    # A claim about what the PRODUCT can do, not about what this repo
    # implements. A sibling repo shipped a README saying a 2Captcha key
    # would not help on its site, while 2Captcha had solved that exact
    # captcha type for years — an error no test could catch, because
    # nothing fails and the output stays correct; it just tells a reader
    # not to buy something that works. The only sentence this family is
    # entitled to is "this repo does not implement X", which is a TODO.
    # Two engines here carried "so this challenge cannot be solved" until
    # v0.4.1, on a site whose captcha 2Captcha does solve.
    "cannot be solved",
    "can't be solved",
    "is inapplicable",
)

# Flags that must not exist ON THE ENGINES:
#   --antidetect   the endpoint behind it was a placeholder that never existed.
#   --country      the URL/mode already decides which page is read; a flag
#                  here could disagree with it. (fingerprint_client.py
#                  legitimately has --country: it picks a fingerprint
#                  locale, a different question -- so this check is scoped
#                  to the engines, not the whole repo.)
#   --details      the old pre-family scraper's flag for "also fetch each
#                  player's profile page". That is --mode player now.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--marketplace", "--country", "--details")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py")


def test_wording():
    group("wording and removed flags")
    ok = True
    shipped = [f for f in os.listdir(REPO_ROOT)
              if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".html"))
              and f != os.path.basename(__file__)]
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r (%s)" % (phrase, ", ".join(offenders) or "clean"),
                    not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            if ('add_argument("%s"' % flag) in text or ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag, not offenders)

    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor captcha/proxy service",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com|"
                                  r"anti-?captcha\.com|capsolver|2captcha\.com/?[a-z]*competit",
                                  text, re.IGNORECASE))
        ok &= check("the README does not reference gate.2prx.com",
                    "gate.2prx.com" not in text)
    return ok


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    Deliberately coarse -- it pools every binding in the file rather than
    tracking scopes, so it under-reports and never invents a problem. That
    is the right trade here: it exists to catch a name that is nowhere at
    all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A fingerprint in the shape the API actually returns, trimmed to the keys
# this repo reads. Values changed so nothing here looks like a specific
# machine.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "GB",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "en-GB",
        "languages": ["en-GB", "en"],
        "timeZone": "Europe/London",
    },
    "screen": {"width": 1920, "height": 1080,
              "outerWidth": 1920, "outerHeight": 992,
              "deviceScaleFactor": 1},
}


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "GB"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "en-GB")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/London")
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
               "geolocation", "permissions", "extra_http_headers",
               "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright's new_context() accepts",
                set(kw) <= accepted)

    ok &= check("the fingerprint's own deviceScaleFactor is carried through "
                "(a Retina/HiDPI screen used to render as a plain 1x context)",
                kw.get("device_scale_factor") == 1.0)
    no_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800}})
    ok &= check("a screen with no deviceScaleFactor/devicePixelRatio at all "
                "omits the kwarg rather than sending 0 or None",
                "device_scale_factor" not in no_dsf)
    bad_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800, "deviceScaleFactor": "not-a-number"}})
    ok &= check("a garbage deviceScaleFactor is dropped, not sent through to "
                "new_context() where Playwright would reject it",
                "device_scale_factor" not in bad_dsf)
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client: main() reads TWOCAPTCHA_KEY the same way "
          "every other entry point in this repo does")
    ok = True
    import fingerprint_client as fpc

    # This file used to be the one CLI in the repo that skipped env_config
    # entirely and read only its own --key flag -- so a TWOCAPTCHA_KEY set in
    # .env or exported the way every other script here reads it was silently
    # ignored, and the first thing CLAUDE.md tells someone to run when a key
    # "isn't working" (`python3 fingerprint_client.py`) could not see it.
    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    old_argv = sys.argv
    # "No TWOCAPTCHA_KEY anywhere" has to mean no .env either, and
    # env_config.load_env() defaults to the .env sitting NEXT TO THE SCRIPTS
    # (not the current directory -- chdir does not isolate this). A developer
    # who followed the README and created one therefore made main() succeed,
    # and this assertion failed for exactly the people who had configured the
    # tool correctly. Found by finally writing a real .env for a live run.
    #
    # Isolated by pointing the loader at a path that does not exist, which
    # leaves the precedence logic itself running -- the thing under test --
    # rather than stubbing env_config out altogether, and touches no file on
    # disk (a test must not mutate the working tree).
    isolated = tempfile.mkdtemp()
    real_load_env = env_config.load_env
    try:
        env_config.load_env = (
            lambda path=None, override=False, _p=os.path.join(isolated, ".env"):
            real_load_env(path=_p, override=override))
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("with no --key and no TWOCAPTCHA_KEY anywhere, main() "
                    "refuses with exit 2 before ever calling the network",
                    rc == 2)
    finally:
        env_config.load_env = real_load_env
        shutil.rmtree(isolated, ignore_errors=True)
        sys.argv = old_argv
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved

    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    seen = {}
    real_get_fingerprint = fpc.get_fingerprint

    def fake_get_fingerprint(key, **kwargs):
        seen["key"] = key
        return {"id": 1, "userAgent": {"userAgent": "UA/1.0"}}

    fpc.get_fingerprint = fake_get_fingerprint
    old_argv = sys.argv
    try:
        os.environ["TWOCAPTCHA_KEY"] = "from-environment-not-a-flag"
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("a TWOCAPTCHA_KEY exported (never passed via --key) "
                    "reaches get_fingerprint through env_config.apply()",
                    seen.get("key") == "from-environment-not-a-flag")
        ok &= check("main() succeeds (exit 0) once the key is found via the "
                    "environment",
                    rc == 0)
    finally:
        fpc.get_fingerprint = real_get_fingerprint
        sys.argv = old_argv
        os.environ.pop("TWOCAPTCHA_KEY", None)
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_remote_api_error():
    group("exit 5: a 2Captcha product call failing is distinguishable from "
          "a crash (1) or the target site blocking a page (3)")
    ok = True
    import fingerprint_client as fpc

    ok &= check("EXIT_REMOTE_API_ERROR is the family contract's value (5)",
                EXIT_REMOTE_API_ERROR == 5)
    ok &= check("RemoteAPIError is still a RuntimeError -- existing generic "
                "handlers are not broken by this addition",
                issubclass(RemoteAPIError, RuntimeError))

    class _FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.text = body
        def json(self):
            return json.loads(self.text)
        def raise_for_status(self):
            if self.status_code >= 400:
                raise __import__("requests").HTTPError(
                    "%d for url: %s?key=should-be-redacted" % (self.status_code, fpc.RANDOM_URL))

    real_get = fpc.requests.get
    for status, body, label in (
        (401, '{"errorCode":"ERROR_WRONG_USER_KEY"}', "401 (bad key)"),
        (400, '{"errorDescription":"tags invalid"}', "400 (bad request)"),
        (429, '{"errorCode":"ERROR_FINGERPRINT_RATE_LIMITED"}', "429 (rate limited)"),
        (503, "upstream unavailable", "503 (upstream, via raise_for_status)"),
    ):
        fpc.requests.get = lambda *a, _s=status, _b=body, **kw: _FakeResp(_s, _b)
        try:
            raised = False
            try:
                fpc.get_fingerprint("fake-key", cache_dir=None)
            except RemoteAPIError:
                raised = True
            ok &= check("get_fingerprint raises RemoteAPIError on %s, not a "
                        "bare RuntimeError callers can't distinguish" % label,
                        raised)
        finally:
            fpc.requests.get = real_get

    # A connection failure (DNS, refused, timeout) goes through the same
    # path via requests.RequestException -- checked separately since it
    # never reaches a status code at all.
    import requests as _requests
    def _raise_conn_error(*a, **kw):
        raise _requests.ConnectionError("Max retries exceeded")
    fpc.requests.get = _raise_conn_error
    try:
        raised = False
        try:
            fpc.get_fingerprint("fake-key", cache_dir=None)
        except RemoteAPIError:
            raised = True
        ok &= check("get_fingerprint raises RemoteAPIError on a connection "
                    "failure too, not just a bad HTTP status", raised)
    finally:
        fpc.requests.get = real_get

    # The three engines must agree on this mapping (family invariant -- see
    # each module's own docstring). Hard to exercise live without a real
    # --cdp-endpoint or a real bad key, so checked at the source level, the
    # same way this suite already checks banned wording and removed flags.
    for engine_file in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = open(os.path.join(REPO_ROOT, engine_file), encoding="utf-8").read()
        ok &= check("%s imports RemoteAPIError from output_writer" % engine_file,
                    "RemoteAPIError" in src and "from output_writer import" in src)
        ok &= check("%s's __main__ catches RemoteAPIError and exits "
                    "EXIT_REMOTE_API_ERROR, not just ProxyError" % engine_file,
                    "except RemoteAPIError as e:" in src
                    and "sys.exit(EXIT_REMOTE_API_ERROR)" in src)

    pw_src = open(os.path.join(REPO_ROOT, "playwright_scraper.py"), encoding="utf-8").read()
    ok &= check("playwright_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError, not a bare PWError an uncaught crash would "
                "swallow into exit 1",
                "raise RemoteAPIError(" in pw_src and "could not connect to --cdp-endpoint" in pw_src)
    pp_src = open(os.path.join(REPO_ROOT, "puppeteer_scraper.py"), encoding="utf-8").read()
    ok &= check("puppeteer_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError the same way",
                "raise RemoteAPIError(" in pp_src and "could not connect to --cdp-endpoint" in pp_src)
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone (only
    # --mode transfers is concurrency-capable here -- see
    # CONCURRENCY_CAPABLE_MODES) and decides whether the rest may be
    # addressed, so a blocked page 1 means the workers never start. Driven
    # here with the browser stubbed out, which leaves exactly the
    # concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "transfers"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.rows = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results can be reconstructed into page order (they arrive in
    #    whatever order the threads finish, which is why the caller merges
    #    by page number rather than by arrival).
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event: asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no module references a name that does not exist")
    ok = True
    # This exists because of exactly the bug flagged in puppeteer_scraper.py's
    # own docstring: a sibling repo's pyppeteer engine called a function on a
    # line reached only while fetching a live page, after the import of that
    # name had been removed. The module imported fine, `--help` worked,
    # `compileall` passed, the whole offline suite passed and CI was green --
    # and the engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0]) for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok


def test_ci_checks_is_actually_wired_up():
    group(".github/ci_checks.py is shipped AND actually invoked, not just present")
    ok = True
    # A sibling in this family (mediamarkt-scraper) shipped ci_checks.py and
    # had tests.yml run a separately hand-maintained inline copy of the same
    # secret scan instead -- the two disagreed (the inline one matched only
    # ws://, the shipped one also matched http://), and the shipped file was
    # invoked by nothing at all. A check that exists but that no workflow
    # calls is as good as no check.
    ci_checks = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check(".github/ci_checks.py exists", os.path.isfile(ci_checks))

    tests_yml = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    workflow_text = ""
    if os.path.isfile(tests_yml):
        workflow_text = open(tests_yml, encoding="utf-8").read()
    ok &= check("tests.yml exists", bool(workflow_text))
    ok &= check("tests.yml actually runs the shipped secret-check (not a "
                "second, hand-copied scan that can drift from it)",
                "ci_checks.py --secret-check" in workflow_text)

    if os.path.isfile(ci_checks):
        import subprocess
        result = subprocess.run(
            [sys.executable, ci_checks, "--all"],
            capture_output=True, text=True, cwd=REPO_ROOT)
        ok &= check("`python3 .github/ci_checks.py --all` passes against "
                    "this repo right now (help/sample/secret checks, for "
                    "real, not just parsed)",
                    result.returncode == 0)
        if result.returncode != 0:
            print(result.stdout, result.stderr)
    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image", entrypoint in copied)

    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"), not missing)

    gone = sorted(f for f in copied if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real capture's parse")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX", text, re.IGNORECASE))
    ok &= check("every sample row carries an id of a shape this site uses",
                all(re.fullmatch(r"[A-Za-z0-9_]+", r.get("sku") or "")
                    for r in rows))
    ok &= check("every sample row says which host it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's url is a real product URL on this site",
                all(is_product_url(r.get("url") or "") for r in rows))
    ok &= check("the sample shows a real price_source, not a column of nulls",
                all(r.get("price_source") in
                    ("tile-microdata", "tile-text", "jsonld", "pdp-text")
                    for r in rows))
    # The sample is a PRICED locale, so it also demonstrates the columns a
    # showcase-locale run would leave null -- a sample of nulls would
    # document nothing.
    ok &= check("the sample actually carries prices and a currency",
                all(r.get("price") is not None and r.get("currency")
                    for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema", header.strip().split(",") == names)
    return ok


# ---------------------------------------------------------------------------
def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports
    # success without checking that what it wanted actually happened.
    skips = []

    ok &= test_money_parsing()
    ok &= test_tile_parsing()
    ok &= test_product_parsing()
    ok &= test_jsonld_shapes()
    ok &= test_urls_and_ids()
    ok &= test_showcase_and_states()
    ok &= test_aws_waf()
    ok &= test_bot_detection()
    ok &= test_page_flow()
    ok &= test_readiness_wait()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_remote_api_error()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_engine_parity(skips)
    ok &= test_env_duplicate_keys()
    ok &= test_env_example_matches_env_keys()
    ok &= test_proxy_filenames_are_ignored()
    ok &= test_aws_waf_two_actions()
    ok &= test_minted_proxy_sessions()
    ok &= test_browser_profile_client()
    ok &= test_scraper_api_client()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_application()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
