#!/usr/bin/env python3
"""
Lumen scraper — Playwright port (drop-in for the Selenium version).

Keeps the same public API:
    Lumen("src/IMAP Details.xlsx").run()

Relies on driver.py's create_browser_context for the persistent profile
and Cloudflare handling. Scrapy.Selector is replaced by lxml.html, which
ships with Scrapy anyway.
"""

from __future__ import annotations

import asyncio
import gc
import json
import random
import re
import signal
import traceback
from datetime import date
from pathlib import Path
from urllib.parse import quote, urljoin

import lxml.html
import pandas as pd
import price_fetch

from src.automation.pw_driver import create_browser_context, safe_goto
from src.logging import logger
from src.commons import (
    append_product, build_master, find_latest, limit_current_products, load_links,
    load_product_urls, out_path, overwrite_links, products_file_name,
    reset_current_products,
)


RESTART_EVERY = 50
PRODUCT_WORKERS = 3
COMPETITOR_WORKERS = 3
QUEUE_SIZE = PRODUCT_WORKERS * 2
MAX_LINKS_PER_BRAND = 96
LINK_SCHEMA = "product-cards-v1"
LINKS_STATUS = "Lumen Links Status"
PRODUCTS_STATUS = "Lumen Product Status"
LINK_COL = "Brands best seller products link."
BRAND_COL = "Brand name"

PROFILE_DIR = Path("./chrome-profile")
CF_TIMEOUT_SEC = 90


class Lumen:

    def __init__(self, file_path: str):
        self.file_path = file_path
        with pd.ExcelFile(file_path) as xls:
            self.df = pd.read_excel(xls, sheet_name="Main")
            self.seller_sites = pd.read_excel(xls, sheet_name="Seller Sites")["Seller Sites"].tolist()

        for col in (LINKS_STATUS, PRODUCTS_STATUS):
            if col not in self.df.columns:
                self.df[col] = ""
            self.df[col] = self.df[col].astype("object").fillna("")

        # Playwright state — populated by _ensure_browser()
        self._pw_cm = None              # the async_playwright() context manager
        self.context = None
        self.page = None
        self.product_pages = []
        self.competitor_pages = []
        self._loads = 0                 # instance counter (was class-level; see notes)
        self.competitor_queue = None
        self.competitor_done = {}
        self.competitor_pending = set()
        self.competitor_pending_lock = None
        self.competitor_checkpoint_lock = None
        self.competitor_checkpoint_path = None
        self.competitor_report_path = None
        self.competitor_sellers = {}
        self.product_names = {}

    # ------------------------------------------------------------------ browser lifecycle
    async def _ensure_browser(self) -> None:
        """Open the persistent context and page if not already open."""
        if self.context is not None:
            try:
                if self.page is None or self.page.is_closed():
                    self.page = await self.context.new_page()
                self.product_pages = [
                    page for page in self.product_pages if not page.is_closed()
                ]
                while len(self.product_pages) < PRODUCT_WORKERS:
                    self.product_pages.append(await self.context.new_page())
                self.competitor_pages = [
                    page for page in self.competitor_pages if not page.is_closed()
                ]
                while len(self.competitor_pages) < COMPETITOR_WORKERS:
                    self.competitor_pages.append(await self.context.new_page())
                return
            except Exception:
                await self._close_browser()
        self._pw_cm = create_browser_context(
            PROFILE_DIR,
            headless=False,
            block_media=True,        # product image URLs come from listing data
        )
        self.context, self.page = await self._pw_cm.__aenter__()
        self.product_pages = [await self.context.new_page() for _ in range(PRODUCT_WORKERS)]
        self.competitor_pages = [
            await self.context.new_page() for _ in range(COMPETITOR_WORKERS)
        ]
        logger.info("[browser] persistent context ready")

    async def _close_browser(self) -> None:
        if self._pw_cm is None:
            return
        try:
            await self._pw_cm.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(f"[browser] close error: {exc!r}")
        finally:
            self._pw_cm = None
            self.context = None
            self.page = None
            self.product_pages = []
            self.competitor_pages = []

    async def _restart_browser(self) -> None:
        logger.info("[browser] restarting...")
        await self._close_browser()
        await self._ensure_browser()

    def _page_alive(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    async def clear_memory(self, page=None) -> None:
        """Release the current document without restarting shared worker tabs."""
        self._loads += 1
        page = page or self.page
        if self._loads >= RESTART_EVERY:
            self._loads = 0
            logger.info("[memory] recycled page documents after %s navigations", RESTART_EVERY)
        if page is not None and not page.is_closed():
            try:
                cdp = await self.context.new_cdp_session(page)
                await cdp.send("Network.clearBrowserCache")
                await cdp.detach()
            except Exception:
                pass
            try:
                await page.goto("about:blank", wait_until="commit")
            except Exception:
                pass
        gc.collect()

    async def safe_get(self, url: str, attempts: int = 3, page=None) -> bool:
        """Navigate with retries + Cloudflare wait. Returns True on success."""
        page = page or self.page
        if page is None or page.is_closed():
            await self._ensure_browser()
            page = self.page
        for i in range(1, attempts + 1):
            try:
                ok = await safe_goto(
                    page, url,
                    timeout=60_000,
                    cf_timeout_sec=CF_TIMEOUT_SEC,
                    log_fn=logger.info,
                )
                if ok:
                    return True
                logger.warning(f"[get] attempt {i}/{attempts}: Cloudflare did not clear")
            except Exception as exc:
                logger.error(f"[get] attempt {i}/{attempts} failed: {exc!r}")
            await asyncio.sleep(random.uniform(1.0, 5.0) * i)
        return False

    # ------------------------------------------------------------------ excel
    def save_excel(self) -> None:
        try:
            with pd.ExcelWriter(
                self.file_path, engine="openpyxl", mode="a", if_sheet_exists="replace",
            ) as w:
                self.df.to_excel(w, sheet_name="Main", index=False)
        except PermissionError:
            logger.error("Excel file is open elsewhere — status not saved this time")

    def set_status(self, index, col, value) -> None:
        self.df.at[index, col] = value
        self.save_excel()

    # ------------------------------------------------------------------ extraction
    @staticmethod
    def extract_details(page_source: str):
        """
        Port of the Selenium version. Scrapy.Selector -> lxml.html.
        XPath is unchanged; the DOM is the same.
        """
        doc = lxml.html.fromstring(page_source)

        mfr_nodes = doc.xpath(
            '//div[@data-testid="pdp-manufacturer-id"]//div[@class="id-number"]/text()'
        )
        mfr_id = mfr_nodes[0].strip() if mfr_nodes else None

        price = None
        for m in re.finditer(r'"price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)"?', page_source):
            val = m.group(1).replace(",", "").strip()
            if val and val not in ("0", "0.0", "0.00"):
                price = m.group(1).strip()
                break

        if not price:
            for c in doc.xpath(
                '//*[contains(concat(" ", normalize-space(@class), " "), " price ")]//text()'
            ):
                c = c.strip()
                if c and re.search(r"\d", c) and ("$" in c or re.search(r"\d+\.\d{2}", c)):
                    price = c
                    break

        logger.info(f"MFR ID : {mfr_id} and Price : {price}")
        return mfr_id, price

    async def parse_product(self, page, product, link, rank, brand_name):
        lumens_url = urljoin("https://www.lumens.com/", product["url"])
        if not await self.safe_get(lumens_url, page=page):
            raise RuntimeError(f"could not load {lumens_url}")

        html = await page.content()
        mfr_id, price = self.extract_details(html)
        return {
            "Lumnes Product Name": product.get("name"),
            "Lumnes Product Url": lumens_url,
            "Lumnes Product Image Url": product.get("image"),
            "Lumens Product Price": price.replace(",", "") if price else None,
            "Lumens BSR rank": rank,
            "MFR ID": mfr_id,
            "Domain": link.split("com/")[-1].split("/")[0],
            "Brand Name": brand_name,
        }

    # ------------------------------------------------------------------ links
    @staticmethod
    def _has_brand(facets, brand_name: str) -> bool:
        expected = str(brand_name).strip().casefold()
        if not isinstance(facets, list):
            return False
        for facet in facets:
            if not isinstance(facet, dict) or str(facet.get("name", "")).strip().casefold() != "brand":
                continue
            values = facet.get("values", [])
            if values is None:
                continue
            if isinstance(values, str):
                values = [values]
            elif not isinstance(values, (list, tuple, set)):
                values = [values]
            if any(str(value).strip().casefold() == expected for value in values):
                return True
        return False

    @classmethod
    def _brand_products(cls, products, brand_name: str):
        """Return one product-card URL per parent API product matching the brand."""
        for product in products:
            if not isinstance(product, dict):
                continue
            variants = product.get("variants")
            if isinstance(variants, list) and variants:
                matching_variant = next((
                    variant for variant in variants
                    if isinstance(variant, dict)
                    and isinstance(variant.get("data"), dict)
                    and cls._has_brand(variant["data"].get("facets"), brand_name)
                ), None)
                if not matching_variant:
                    continue
                # The category card link is the parent product URL; variant
                # URLs in data.url represent color/size/SKU options and are skipped.
                url = product.get("url")
                if not url:
                    continue
                facets = (matching_variant.get("data") or {}).get("facets")
                yield {
                    "id": product.get("id"),
                    "name": product.get("name") or matching_variant.get("value"),
                    "url": urljoin("https://www.lumens.com/", str(url)),
                    "image": product.get("image") or (matching_variant.get("data") or {}).get("image_url"),
                    "facets": facets,
                    "_link_schema": LINK_SCHEMA,
                    "Status": product.get("Status", "NA"),
                    "_page": product.get("_page"),
                }
                continue

            # Resume the compact parent-card records written by this schema.
            if cls._has_brand(product.get("facets"), brand_name) and product.get("url"):
                normalized = dict(product)
                normalized["url"] = urljoin(
                    "https://www.lumens.com/", str(product["url"]),
                )
                normalized["name"] = product.get("name") or product.get("value")
                normalized["image"] = product.get("image") or product.get("image_url")
                normalized["_link_schema"] = LINK_SCHEMA
                normalized.setdefault("Status", "NA")
                yield normalized

    async def _fetch_page_products(
        self, link: str, page_number: int, brand_name: str, page=None,
    ):
        """Return (matching variants, has API products) or None on fetch failure."""
        sep = "&" if "?" in link else "?"
        url = (
            link + sep
            + "c_composable=true&siteId=Lumens"
              f"&c_page={page_number}"
              "&c_sortOption=revenue%2Bdescending"
              "&c_queryType=category"
              "&c_constructorClientId=5d888221-bf57-45e3-add7-3dc217039ee8"
              "&c_constructorSessionId=8"
              f"&c_pageUrl={quote(link, safe='')}"
        )
        if not await self.safe_get(url, page=page):
            return None

        html = await (page or self.page).content()
        try:
            doc = lxml.html.fromstring(html)
        except Exception:
            return None
        pre_nodes = doc.xpath("//pre/text()")
        if not pre_nodes:
            return None
        try:
            data = json.loads(pre_nodes[0])
        except json.JSONDecodeError:
            return None
        products = (data.get("object") or {}).get("products") or []
        return list(self._brand_products(products, brand_name)), bool(products)

    def _initialize_competitor_pipeline(self) -> None:
        """Prepare price_fetch checkpointing and seller domains from this workbook."""
        self.competitor_checkpoint_path = out_path(
            "JSON", "Competitor Price Progress.jsonl",
        )
        self.competitor_report_path = out_path(
            "XLSX", "Competitor Price Comparison.xlsx",
        )
        price_fetch.CHECKPOINT = self.competitor_checkpoint_path
        price_fetch.LEGACY_CHECKPOINT = None
        price_fetch._blocked_hosts.clear()
        self.competitor_done = price_fetch.load_checkpoint()
        self.competitor_pending = set(self.competitor_done)
        self.competitor_pending_lock = asyncio.Lock()
        self.competitor_checkpoint_lock = asyncio.Lock()
        self.competitor_queue = asyncio.Queue(maxsize=COMPETITOR_WORKERS * 2)
        self.competitor_sellers = {}
        for seller_url in self.seller_sites:
            if not isinstance(seller_url, str) or not seller_url.strip():
                continue
            seller_url = seller_url.strip()
            host = price_fetch.norm_host(seller_url)
            if host:
                self.competitor_sellers[host] = seller_url

        logger.info(
            "[competitors] ready: %s seller domains, %s checkpointed products",
            len(self.competitor_sellers), len(self.competitor_done),
        )

    def _prepare_product_card_schema(self) -> None:
        """Discard today's old variant-URL state once; never touch prior dates."""
        legacy_brands = set()
        for brand_name in self.df[BRAND_COL].dropna().astype(str).unique():
            records = load_links(brand_name)
            if any(
                isinstance(product, dict)
                and product.get("_link_schema") != LINK_SCHEMA
                and (product.get("variation_id") or "variationValues" in product)
                for product in records
            ):
                legacy_brands.add(brand_name)

        if not legacy_brands:
            return

        for brand_name in legacy_brands:
            overwrite_links(brand_name, [])
            removed = reset_current_products(brand_name)
            logger.info(
                "[%s] reset today's old variant output (%s CSV rows)",
                brand_name, removed,
            )
            mask = self.df[BRAND_COL].astype(str).str.strip() == brand_name.strip()
            self.df.loc[mask, [LINKS_STATUS, PRODUCTS_STATUS]] = ""

        # The old competitor checkpoint is keyed by variant URLs. Start a clean
        # product-card checkpoint/report for today; earlier dates are untouched.
        for file_type, file_name in (
            ("JSON", "Competitor Price Progress.jsonl"),
            ("XLSX", "Competitor Price Comparison.xlsx"),
        ):
            path = out_path(file_type, file_name)
            if path.exists():
                path.unlink()
        self.save_excel()

    async def _schedule_competitor(self, record, product, link_products, brand_name, write_lock):
        """Queue one extracted Lumens record once; checkpointed URLs are never repeated."""
        if self.competitor_queue is None:
            return
        product_url = record.get("Lumnes Product Url")
        if not product_url:
            return

        async with self.competitor_pending_lock:
            if product_url in self.competitor_done:
                async with write_lock:
                    product["Competitor Status"] = "Completed"
                    product["Competitor Result Count"] = len(
                        self.competitor_done[product_url],
                    )
                    overwrite_links(brand_name, link_products)
                return
            if product_url in self.competitor_pending:
                return
            self.competitor_pending.add(product_url)
            async with write_lock:
                product["Competitor Status"] = "In Progress"
                overwrite_links(brand_name, link_products)

        try:
            await self.competitor_queue.put(
                (record, product, link_products, brand_name, write_lock),
            )
        except BaseException:
            async with self.competitor_pending_lock:
                self.competitor_pending.discard(product_url)
            raise

    async def _competitor_worker(self, worker_id, page) -> None:
        while True:
            job = await self.competitor_queue.get()
            try:
                if job is None:
                    return
                record, product, link_products, brand_name, write_lock = job
                product_url = record["Lumnes Product Url"]
                competitor_product = {
                    "name": record.get("Lumnes Product Name") or "",
                    "url": product_url,
                    "image": record.get("Lumnes Product Image Url"),
                    "price": price_fetch.parse_price(record.get("Lumens Product Price")),
                    "rank": record.get("Lumens BSR rank"),
                    "mfr": str(record.get("MFR ID") or "").strip(),
                }
                try:
                    logger.info("[competitor-%s] Comparing %s", worker_id, product_url)
                    rows = await price_fetch.process_product(
                        self.context, page, competitor_product,
                        self.competitor_sellers, debug_dir=None,
                    )
                    async with self.competitor_checkpoint_lock:
                        with open(self.competitor_checkpoint_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"key": product_url, "rows": rows}, default=str) + "\n")
                        self.competitor_done[product_url] = rows
                    async with write_lock:
                        product["Competitor Status"] = "Completed"
                        product["Competitor Result Count"] = len(rows)
                        overwrite_links(brand_name, link_products)
                    try:
                        await page.goto("about:blank", wait_until="commit")
                    except Exception:
                        pass
                except Exception as exc:
                    logger.error(
                        "[competitor-%s] Failed %s: %s: %s",
                        worker_id, product_url, type(exc).__name__, exc,
                    )
                    async with write_lock:
                        product["Competitor Status"] = f"Failed: {type(exc).__name__}: {exc}"[:240]
                        overwrite_links(brand_name, link_products)
                        if str(product.get("Status", "")).startswith("Completed"):
                            product["Status"] = "Completed"
                    try:
                        await page.goto("about:blank", wait_until="commit")
                    except Exception:
                        pass
            finally:
                if job is not None:
                    async with self.competitor_pending_lock:
                        self.competitor_pending.discard(job[0].get("Lumnes Product Url"))
                self.competitor_queue.task_done()

    async def _write_competitor_report(self) -> None:
        if not self.competitor_done or self.competitor_report_path is None:
            return
        rows = [row for product_rows in self.competitor_done.values() for row in product_rows]
        for row in rows:
            product_name = self.product_names.get(row.get("Url"))
            if product_name:
                row["Product Name"] = product_name
        await asyncio.to_thread(price_fetch.write_excel, rows, self.competitor_report_path)
        logger.info("[competitors] saved %s rows to %s", len(rows), self.competitor_report_path)

    async def _process_brand(self, index, row) -> None:
        """Extract links and scrape products concurrently using a bounded queue."""
        await self._ensure_browser()
        brand_name = row[BRAND_COL]
        link = row[LINK_COL]
        raw_links = load_links(brand_name)
        filtered_links_status = (
            f"Completed ({LINK_SCHEMA}; brand-filtered: {str(brand_name).strip()}; "
            f"date: {date.today().isoformat()})"
        )
        current_product_urls = limit_current_products(
            brand_name, MAX_LINKS_PER_BRAND,
        )
        link_products = []
        seen_urls = set()
        for product in self._brand_products(raw_links, brand_name):
            product_url = product.get("url")
            if not product_url or product_url in seen_urls:
                continue
            seen_urls.add(product_url)
            if len(link_products) == MAX_LINKS_PER_BRAND:
                break
            link_products.append(product)
        self.product_names.update({
            product["url"]: product["name"]
            for product in link_products
            if product.get("url") and product.get("name")
        })
        done_urls = load_product_urls(brand_name)
        done_urls.update(current_product_urls)
        products_path = find_latest("CSV", products_file_name(brand_name))
        saved_records = {}
        if products_path:
            for saved in price_fetch.load_products(products_path):
                saved_records[saved["url"]] = {
                    "Lumnes Product Name": saved["name"],
                    "Lumnes Product Url": saved["url"],
                    "Lumnes Product Image Url": saved["image"],
                    "Lumens Product Price": saved["price"],
                    "Lumens BSR rank": saved["rank"],
                    "MFR ID": saved["mfr"],
                    "Brand Name": brand_name,
                }
        reserved_urls = set()
        write_lock = asyncio.Lock()
        queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        product_queue_done = asyncio.Event()
        link_status = str(self.df.at[index, LINKS_STATUS]).strip()
        links_done = (
            link_status == filtered_links_status
            or len(link_products) >= MAX_LINKS_PER_BRAND
        )

        if raw_links != link_products:
            overwrite_links(brand_name, link_products)
        self.set_status(
            index, LINKS_STATUS,
            filtered_links_status if links_done else "In Progress",
        )
        self.set_status(index, PRODUCTS_STATUS, "In Progress")

        async def product_worker(worker_id, page):
            while True:
                job = await queue.get()
                try:
                    if job is None:
                        return
                    rank, product = job
                    lumens_url = urljoin("https://www.lumens.com/", product["url"])

                    if product_queue_done.is_set():
                        await self._schedule_competitor(
                            saved_records.get(lumens_url, {
                                "Lumnes Product Url": lumens_url,
                                "Lumnes Product Name": product.get("name"),
                                "Lumnes Product Image Url": product.get("image"),
                                "Lumens Product Price": None,
                                "Lumens BSR rank": rank,
                                "MFR ID": product.get("id"),
                            }),
                            product, link_products, brand_name, write_lock,
                        )
                        continue

                    already_done = False
                    async with write_lock:
                        if product.get("Status") == "Completed" or lumens_url in done_urls:
                            product["Status"] = "Completed"
                            overwrite_links(brand_name, link_products)
                            already_done = True
                        elif len(current_product_urls) + len(reserved_urls) >= MAX_LINKS_PER_BRAND:
                            product["Status"] = f"Skipped - {MAX_LINKS_PER_BRAND} product limit"
                            overwrite_links(brand_name, link_products)
                        else:
                            reserved_urls.add(lumens_url)
                            product["Status"] = "In Progress"
                            overwrite_links(brand_name, link_products)

                    if already_done:
                        saved_record = saved_records.get(lumens_url)
                        if saved_record:
                            await self._schedule_competitor(
                                saved_record, product, link_products, brand_name, write_lock,
                            )
                        continue
                    if product.get("Status", "").startswith("Skipped - "):
                        continue

                    try:
                        logger.info("[product-%s] Going to %s", worker_id, lumens_url)
                        record = await self.parse_product(page, product, link, rank, brand_name)
                        async with write_lock:
                            saved = append_product(
                                brand_name,
                                record,
                                known_urls=done_urls,
                                max_rows=MAX_LINKS_PER_BRAND,
                                current_urls=current_product_urls,
                            )
                            product["Status"] = (
                                "Completed" if saved or lumens_url in done_urls
                                else f"Skipped - {MAX_LINKS_PER_BRAND} product limit"
                            )
                            reserved_urls.discard(lumens_url)
                            overwrite_links(brand_name, link_products)
                        if product.get("Status") == "Completed":
                            saved_records[lumens_url] = {
                                **record,
                                "Lumens Product Price": price_fetch.parse_price(
                                    record.get("Lumens Product Price"),
                                ),
                            }
                            await self._schedule_competitor(
                                record, product, link_products, brand_name, write_lock,
                            )
                        await self.clear_memory(page)
                    except Exception:
                        product["Status"] = "Failed " + traceback.format_exc()
                        logger.error("Failed %s\n%s", lumens_url, traceback.format_exc())
                        async with write_lock:
                            reserved_urls.discard(lumens_url)
                            overwrite_links(brand_name, link_products)
                        await self.clear_memory(page)
                finally:
                    queue.task_done()

        async def extract_links():
            nonlocal links_done

            async def queue_existing_products():
                changed = False
                for rank, product in enumerate(link_products, start=1):
                    lumens_url = urljoin("https://www.lumens.com/", product["url"])
                    if product.get("Status") == "Completed" or lumens_url in done_urls:
                        if product.get("Status") != "Completed":
                            product["Status"] = "Completed"
                            changed = True
                        saved_record = saved_records.get(lumens_url)
                        if saved_record:
                            await self._schedule_competitor(
                                saved_record, product, link_products, brand_name, write_lock,
                            )
                        continue
                    await queue.put((rank, product))
                if changed:
                    overwrite_links(brand_name, link_products)

            if links_done:
                await queue_existing_products()
                if self.page is not None and not self.page.is_closed():
                    await self.clear_memory(self.page)
                return

            page_number = max([p.get("_page") or 1 for p in link_products] or [1])
            await queue_existing_products()

            while True:
                result = await self._fetch_page_products(
                    link, page_number, brand_name, self.page,
                )
                if result is None:
                    logger.warning(
                        "[%s] page %s fetch failed; extraction will remain resumable",
                        brand_name, page_number,
                    )
                    self.set_status(index, LINKS_STATUS, f"Partial - stopped at page {page_number}")
                    break
                products, page_has_products = result
                if not page_has_products:
                    links_done = True
                    self.set_status(index, LINKS_STATUS, filtered_links_status)
                    break

                new_products = []
                for product in products:
                    product_url = urljoin(
                        "https://www.lumens.com/", str(product.get("url", "")),
                    )
                    if not product_url or product_url in seen_urls:
                        continue
                    if len(link_products) >= MAX_LINKS_PER_BRAND:
                        break
                    seen_urls.add(product_url)
                    product["url"] = product_url
                    product["Status"] = "NA"
                    product["_page"] = page_number
                    new_products.append(product)

                if new_products:
                    link_products.extend(new_products)
                    overwrite_links(brand_name, link_products)
                    first_rank = len(link_products) - len(new_products) + 1
                    for offset, product in enumerate(new_products):
                        await queue.put((first_rank + offset, product))
                logger.info(
                    "[%s] page %s: %s products (%s new)",
                    brand_name, page_number, len(products), len(new_products),
                )
                if len(link_products) >= MAX_LINKS_PER_BRAND:
                    links_done = True
                    self.set_status(index, LINKS_STATUS, filtered_links_status)
                    break
                page_number += 1
                await self.clear_memory(self.page)

            if self.page is not None and not self.page.is_closed():
                await self.clear_memory(self.page)

        workers = [
            asyncio.create_task(product_worker(worker_id, page))
            for worker_id, page in enumerate(self.product_pages, start=1)
        ]
        try:
            await extract_links()
            await queue.join()
        finally:
            product_queue_done.set()
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)

        left = sum(
            1 for product in link_products
            if product.get("Status") != "Completed"
            and not str(product.get("Status", "")).startswith("Skipped - 96 product limit")
        )
        self.set_status(
            index, PRODUCTS_STATUS,
            "Completed (96 product limit)" if left == 0 and len(current_product_urls) >= MAX_LINKS_PER_BRAND
            else "Completed" if left == 0 else f"Partial {left} left",
        )
        logger.info("Lumen Product Status for %s: %s left", brand_name, left)

    # ------------------------------------------------------------------ main loop
    async def parse(self) -> None:
        for index, row in self.df.iterrows():
            await self._process_brand(index, row)

    async def run(self) -> None:
        try:
            await self._ensure_browser()
            self._prepare_product_card_schema()
            self._initialize_competitor_pipeline()
            competitor_workers = [
                asyncio.create_task(self._competitor_worker(worker_id, page))
                for worker_id, page in enumerate(self.competitor_pages, start=1)
            ]
            extra_pages = []
            try:
                await self.parse()

                # The Lumen queue is now drained. Reassign its idle tabs to the
                # competitor queue to finish the remaining price/image work sooner.
                extra_pages = list(self.product_pages)
                if self.page is not None and not self.page.is_closed():
                    extra_pages.append(self.page)
                next_worker_id = len(competitor_workers) + 1
                for page in extra_pages:
                    if not page.is_closed():
                        competitor_workers.append(asyncio.create_task(
                            self._competitor_worker(next_worker_id, page),
                        ))
                        next_worker_id += 1

                await self.competitor_queue.join()
            finally:
                for _ in competitor_workers:
                    await self.competitor_queue.put(None)
                await asyncio.gather(*competitor_workers)
                for page in extra_pages:
                    if not page.is_closed():
                        await page.close()
        finally:
            try:
                await self._write_competitor_report()
            except Exception:
                logger.error(
                    "Could not write competitor report\n%s",
                    traceback.format_exc(),
                )
            try:
                path, n = build_master("Lumen_data.csv")
                logger.info(f"Saved {n} rows to {path}")
            except Exception:
                logger.error(f"Could not build Lumen_data.csv\n{traceback.format_exc()}")
            try:
                self.save_excel()
            finally:
                await self._close_browser()


def _run_sync() -> None:
    """Bridge asyncio.run + SIGTERM so the finally block runs on kill."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sigterm(*_):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (ValueError, OSError):
        pass  # not on main thread; ignore

    try:
        loop.run_until_complete(Lumen("src/IMAP Details.xlsx").run())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    _run_sync()