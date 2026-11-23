import csv
import json
import os
import re
from datetime import date
from pathlib import Path
import pandas as pd
import gc

RESTART_EVERY = 50
BASE_DIR = "Output Data"

# Columns of every product row and of the final Lumen_data.csv
PRODUCT_COLUMNS = [
    "Lumnes Product Name",
    "Lumnes Product Url",
    "Lumnes Product Image Url",
    "Lumens Product Price",
    "Lumens BSR rank",
    "MFR ID",
    "Domain",
    "Brand Name",
]
PRODUCT_KEY = "Lumnes Product Url"   # dedupe key for CSV rows
LINK_KEY = "url"                     # dedupe key for link/JSON rows


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip()

    _loads = 0   # class-level counter, no change to __init__ needed

# def clear_memory():
#     """Free Chrome + Python memory. Call after every page load."""
#     self._loads += 1
#     try:
#         if self._loads >= RESTART_EVERY or not self._driver_alive():
#             self._loads = 0
#             self.restart_driver()          # quits Chrome, which releases all its memory
#         else:
#             try:
#                 self.driver.execute_cdp_cmd("Network.clearBrowserCache", {})
#             except Exception:
#                 pass
#             self.driver.get("about:blank")  # drops the heavy product page from memory
#     except Exception as e:
#         logger.error(f"[memory] cleanup failed: {e}")
#     gc.collect()

def _today():
    return date.today().isoformat()


def out_path(file_type, file_name, base_dir=BASE_DIR):
    """Output Data/<today>/<TYPE>/<file_name>  (folder is created)."""
    folder = Path(base_dir) / _today() / file_type.upper()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / file_name


def find_latest(file_type, file_name, base_dir=BASE_DIR):
    """Return this date's output only; prior-day artifacts are not resume state."""
    path = Path(base_dir) / _today() / file_type.upper() / file_name
    return path if path.exists() else None


# ----------------------------------------------------------------- JSON (links)
def _atomic_json_write(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)          # a crash can never leave a half-written file


def links_file_name(brand):
    return f"{safe_name(brand)} Links data.json"


def load_links(brand):
    p = find_latest("JSON", links_file_name(brand))
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_links(brand, products):
    """Merge `products` into the brand's links JSON (dedupe on url, existing Status wins)."""
    merged = {p[LINK_KEY]: p for p in load_links(brand) if p.get(LINK_KEY)}
    for p in products:
        if p.get(LINK_KEY) and p[LINK_KEY] not in merged:
            merged[p[LINK_KEY]] = p
    result = list(merged.values())
    _atomic_json_write(out_path("JSON", links_file_name(brand)), result)
    return result


def overwrite_links(brand, products):
    """Write the full list as-is (used when product Status values change)."""
    _atomic_json_write(out_path("JSON", links_file_name(brand)), products)


# ----------------------------------------------------------------- CSV (products)
def products_file_name(brand):
    return f"{safe_name(brand)} Products data.csv"


def load_product_urls(brand):
    p = find_latest("CSV", products_file_name(brand))
    if not p:
        return set()
    try:
        return set(pd.read_csv(p, usecols=[PRODUCT_KEY])[PRODUCT_KEY].dropna())
    except Exception:
        return set()


def limit_current_products(brand, max_rows):
    """Deduplicate and cap today's brand CSV, returning its retained product URLs."""
    path = out_path("CSV", products_file_name(brand))
    if not path.exists():
        return set()

    df = pd.read_csv(path)
    if PRODUCT_KEY not in df.columns:
        raise ValueError(f"{path} is missing the required {PRODUCT_KEY!r} column")
    df = df.dropna(subset=[PRODUCT_KEY]).drop_duplicates(subset=[PRODUCT_KEY], keep="first")
    df = df.head(max_rows)

    tmp = Path(str(path) + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    return set(df[PRODUCT_KEY].astype(str))


def reset_current_products(brand):
    """Remove today's product CSV when its URL schema changes; keep prior dates intact."""
    path = out_path("CSV", products_file_name(brand))
    if not path.exists():
        return 0
    try:
        count = len(pd.read_csv(path, usecols=[PRODUCT_KEY]))
    except Exception:
        count = 0
    path.unlink()
    return count


def append_product(brand, record, known_urls=None, *, max_rows=None, current_urls=None):
    """Append one row to today's brand CSV, unless it is already there (today or earlier day)."""
    if known_urls is None:
        known_urls = load_product_urls(brand)
    product_url = record[PRODUCT_KEY]
    if product_url in known_urls:
        return False
    if max_rows is not None and current_urls is not None and len(current_urls) >= max_rows:
        return False
    path = out_path("CSV", products_file_name(brand))
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PRODUCT_COLUMNS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(record)
    known_urls.add(product_url)
    if current_urls is not None:
        current_urls.add(product_url)
    return True


def build_master(file_name="Lumen_data.csv"):
    """Combine every '* Products data.csv' (all dates) into one deduped Lumen_data.csv."""
    base = Path(BASE_DIR)
    frames = []
    if base.exists():
        for p in sorted(base.glob("*/CSV/* Products data.csv")):   # oldest -> newest
            try:
                frames.append(pd.read_csv(p))
            except Exception:
                pass
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PRODUCT_COLUMNS)
    df = df.drop_duplicates(subset=[PRODUCT_KEY], keep="last")
    for c in PRODUCT_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[PRODUCT_COLUMNS]
    path = out_path("CSV", file_name)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path, len(df)