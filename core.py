"""Catalogue, price matching, invoice numbering and the sales ledger."""
import csv
import logging
import difflib
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from storage import STORE

PRODUCTS_FILE = os.path.join(config.BASE_DIR, "products.json")   # shipped default list
SALE_TYPES = ("retail", "whole", "supply")
PAY_MODES = ("CASH", "TRANSFER", "POS", "LOAN")


def now():
    return datetime.now(ZoneInfo(config.TIMEZONE))


# ---------------- catalogue ----------------
_cache = {}
log = logging.getLogger("esmo-bot")


# ---------------- who may use the bot ----------------
def admin_ids():
    """Admins = the IDs in the ALLOWED_USER_IDS setting. They can add/remove other users."""
    return set(config.ALLOWED_USER_IDS)


def _stored_users():
    if "u" not in _cache:
        _cache["u"] = [int(x) for x in (STORE.get_kv("allowed", []) or [])]
    return _cache["u"]


def allowed_ids():
    try:
        stored = set(_stored_users())
    except Exception as ex:                     # database hiccup: admins still work, nothing is cached
        log.error("could not load user list: %s", ex)
        stored = set()
    return admin_ids() | stored


def is_allowed(uid):
    return uid in allowed_ids()


def is_admin(uid):
    return uid in admin_ids()


def add_user(uid):
    users = list(_stored_users())
    if uid not in users and uid not in admin_ids():
        users.append(uid)
        STORE.set_kv("allowed", users)
        _cache["u"] = users


def remove_user(uid):
    """True if removed. Admins (set in the ALLOWED_USER_IDS setting) cannot be removed here."""
    if uid in admin_ids():
        raise ValueError("admin")
    users = list(_stored_users())
    if uid not in users:
        return False
    users.remove(uid)
    STORE.set_kv("allowed", users)
    _cache["u"] = users
    return True




def _default_products():
    try:
        with open(PRODUCTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def products():
    if "p" not in _cache:
        items = STORE.get_kv("products", None)
        if items is None:                      # first run on a fresh database: start from the shipped list
            items = _default_products()
            STORE.set_kv("products", items)
        _cache["p"] = items
    return _cache["p"]


def save_products(items):
    STORE.set_kv("products", items)
    _cache["p"] = items


def upsert_product(name, retail, whole, supply):
    name = name.strip().upper()
    items = products()
    for p in items:
        if p["name"] == name:
            p.update(retail=retail, whole=whole, supply=supply)
            break
    else:
        items.append({"name": name, "retail": retail, "whole": whole, "supply": supply})
    save_products(items)


def delete_product(name):
    name = name.strip().upper()
    items = products()
    kept = [p for p in items if p["name"] != name]
    save_products(kept)
    return len(kept) != len(items)


def match_product(raw):
    """Return (product or None, how) where how is 'exact' | 'fuzzy' | None."""
    items = products()
    key = " ".join(str(raw).upper().split())
    for p in items:
        if p["name"] == key:
            return p, "exact"
    names = [p["name"] for p in items]
    close = difflib.get_close_matches(key, names, n=1, cutoff=0.78)
    if close:
        return next(p for p in items if p["name"] == close[0]), "fuzzy"
    # every word of the typed name appears in exactly one product name
    words = key.split()
    hits = [p for p in items if all(w in p["name"] for w in words)] if words else []
    if len(hits) == 1:
        return hits[0], "fuzzy"
    return None, None


# ---------------- invoice numbers ----------------
def peek_invoice():
    return STORE.get_kv("state", {}).get("next_inv", config.START_INVOICE)


def set_invoice(n):
    st = STORE.get_kv("state", {})
    st["next_inv"] = int(n)
    STORE.set_kv("state", st)


def take_invoice():
    st = STORE.get_kv("state", {})
    n = st.get("next_inv", config.START_INVOICE)
    st["next_inv"] = n + 1
    STORE.set_kv("state", st)
    return n


# ---------------- reading the AI output ----------------
_EMPTY = {"", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "NOT STATED", "-"}


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").replace("\u20a6", "").strip())
    except ValueError:
        return None


def _text(v):
    s = str(v or "").strip()
    return "" if s.upper() in _EMPTY else s


def parse_sale_type(text):
    t = str(text).lower()
    if "retail" in t:
        return "retail"
    if "whole" in t:
        return "whole"
    if "supply" in t:
        return "supply"
    return None


def parse_pay_mode(text):
    t = str(text).lower()
    for words, mode in ((("cash",), "CASH"), (("transfer", "bank"), "TRANSFER"), (("pos", "card"), "POS"),
                        (("loan", "credit", "debt", "later"), "LOAN")):
        if any(w in t.split() or w in t for w in words):
            return mode
    return None


def normalize_parsed(raw):
    """Clean the AI output; anything the AI could not find becomes None/'' so we can ask about it."""
    items = []
    for it in raw.get("items") or []:
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        qty, price = _num(it.get("qty")), _num(it.get("unit_price"))
        items.append({"name": name, "qty": qty if qty and qty > 0 else None,
                      "unit_price": price if price and price > 0 else None})
    pm = _text(raw.get("payment_mode")).upper()
    return {
        "customer": _text(raw.get("customer")),
        "address": _text(raw.get("address")),
        "payment_mode": pm if pm in PAY_MODES else None,
        "sale_type": parse_sale_type(_text(raw.get("sale_type"))),
        "discount": max(0, _num(raw.get("discount")) or 0),
        "discount_done": (_num(raw.get("discount")) or 0) > 0,
        "items": items,
        "problems": [str(p) for p in (raw.get("problems") or []) if str(p).strip()],
    }


def next_question(p):
    """What still needs to be asked? Returns (kind, item_index) or None when the order is complete."""
    if not p["sale_type"]:
        return ("sale", None)
    if not p["payment_mode"]:
        return ("pay", None)
    if not p["customer"]:
        return ("cust", None)
    if not p["address"] and p["customer"] != "WALK-IN":
        return ("addr", None)
    for i, it in enumerate(p["items"]):
        if not it["qty"]:
            return ("qty", i)
    for i, it in enumerate(p["items"]):
        if not it["unit_price"] and match_product(it["name"])[0] is None:
            return ("price", i)
    if not p.get("discount_done"):
        return ("disc", None)
    return None


def subtotal_of(p):
    """Subtotal of the order so far (used to show the boss the amount before asking about discount)."""
    total = 0
    for it in p["items"]:
        prod, _ = match_product(it["name"])
        price = it.get("unit_price") or ((prod.get(p["sale_type"]) or prod["retail"]) if prod else 0)
        total += price * (it.get("qty") or 0)
    return total


def parse_discount(text, subtotal):
    """'5000', '5,000', '5%', '5 percent', 'no' -> naira amount, or None if not understood."""
    t = str(text).lower().replace(",", "").replace("\u20a6", "").replace("ngn", "").strip()
    if t in ("no", "none", "nil", "0", "zero", "no discount", "nope", "n"):
        return 0.0
    pct = "%" in t or "percent" in t or "pct" in t
    num = _num(t.replace("%", "").replace("percent", "").replace("pct", "").strip())
    if num is None or num < 0:
        return None
    return round(subtotal * num / 100, 2) if pct and num <= 100 else (None if pct else float(num))


# ---------------- loading products ----------------
def parse_price_line(line):
    """'NAME | retail | whole | supply'  or  'NAME | price'  -> (name, r, w, s) or None."""
    parts = [x.strip() for x in line.split("|")]
    nums = [_num(x) for x in parts[1:]]
    if not parts[0] or len(parts) not in (2, 4) or any(n is None or n < 0 for n in nums):
        return None
    return (parts[0].upper(), *(nums * 3 if len(nums) == 1 else nums))


def import_backup(d):
    """Load ONLY products (and the next invoice number) from the web app's backup.json.
    Logins, creditors and transactions in that file are ignored and never stored."""
    inv = d.get("inventory") if isinstance(d, dict) else None
    if not isinstance(inv, list):
        raise ValueError("not a backup file")
    items = {}
    for it in inv:
        if not isinstance(it, dict):
            continue
        name, r = " ".join(str(it.get("name", "")).upper().split()), _num(it.get("retail"))
        if not name or r is None:
            continue
        w, s = _num(it.get("whole")), _num(it.get("supply"))
        items[name] = {"name": name, "retail": r, "whole": r if w is None else w, "supply": r if s is None else s}
    if not items:
        raise ValueError("no products found")
    save_products(list(items.values()))
    nxt = _num(d.get("nextInv"))
    if nxt:
        set_invoice(max(int(nxt), peek_invoice()))
    return len(items), peek_invoice()


# ---------------- order building ----------------
class OrderProblem(Exception):
    pass


def build_order(parsed):
    """Turn the AI's reading of a message into a priced order (no invoice number yet).
    Returns (order, warnings). Raises OrderProblem when the order cannot be priced safely."""
    warnings = []
    raw_items = parsed.get("items") or []
    if not raw_items:
        raise OrderProblem("I could not find any products in that message. Please send the order as text, "
                           "or a clearer photo.")

    sale_type = parsed.get("sale_type") if parsed.get("sale_type") in SALE_TYPES else "retail"
    mode = str(parsed.get("payment_mode") or "CASH").upper()
    if mode not in PAY_MODES:
        mode = "CASH"

    items, unmatched = [], []
    for it in raw_items:
        raw_name = str(it.get("name", "")).strip()
        qty = _num(it.get("qty")) or 0
        if qty <= 0 or not raw_name:
            warnings.append(f"Skipped a line I could not read properly: “{raw_name or '?'}”.")
            continue
        qty = int(qty) if qty == int(qty) else qty
        given_price = _num(it.get("unit_price"))

        prod, how = match_product(raw_name)
        if prod:
            name = prod["name"]
            list_price = prod.get(sale_type) or prod["retail"]
            price = given_price if given_price else list_price
            if how == "fuzzy":
                warnings.append(f"“{raw_name}” was read as {name}.")
            if given_price and abs(given_price - list_price) > 0.001:
                warnings.append(f"{name}: used your price {given_price:,.0f} instead of list price {list_price:,.0f}.")
        else:
            if not given_price:
                unmatched.append(raw_name)
                continue
            name, price = raw_name.upper(), given_price
            warnings.append(f"{name} is not in the product list; used your price {given_price:,.0f}.")
        items.append({"name": name, "qty": qty, "price": price})

    if unmatched:
        raise OrderProblem("These items are not in the product list and have no price:\n• " +
                           "\n• ".join(unmatched) +
                           "\n\nResend with a price (e.g. “Item x 5 @ 1200”) or add them with /setprice.")
    if not items:
        raise OrderProblem("I could not read any valid item lines. Please resend more clearly.")

    subtotal = sum(i["price"] * i["qty"] for i in items)
    discount = max(0, _num(parsed.get("discount")) or 0)
    if discount > subtotal:
        warnings.append("Discount was bigger than the order, so I capped it.")
        discount = subtotal

    customer = str(parsed.get("customer") or "").strip() or "WALK-IN"
    address = str(parsed.get("address") or "").strip() or "N/A"
    for p in parsed.get("problems") or []:
        warnings.append(str(p))

    order = {
        "customer": customer, "address": address, "mode": mode, "sale_type": sale_type,
        "discount": discount, "subtotal": subtotal, "total": subtotal - discount, "items": items,
    }
    return order, warnings


# ---------------- ledger ----------------
def record_sale(order):
    STORE.add_sale(order)


def void_sale(inv):
    return STORE.void_sale(inv)


def recent_sales(n=20):
    return STORE.recent_sales(n)


def get_sale(inv):
    return STORE.get_sale(inv)


def day_summary(day):
    sales = STORE.sales_on(day)
    ok = [s for s in sales if s.get("status") != "VOID"]
    by_mode = {}
    for s in ok:
        by_mode[s["mode"]] = by_mode.get(s["mode"], 0) + s["total"]
    return {"count": len(ok), "voided": len(sales) - len(ok), "total": sum(s["total"] for s in ok), "by_mode": by_mode}


def export_csv(path):
    ledger = STORE.all_sales()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["INV", "DATE", "TIME", "CUSTOMER", "ADDRESS", "MODE", "SALE TYPE", "ITEMS",
                    "SUBTOTAL", "DISCOUNT", "TOTAL", "STATUS"])
        for s in ledger:
            items = "; ".join(f"{i['name']} x{i['qty']} @{i['price']:g}" for i in s["items"])
            w.writerow([s["inv"], s["date"], s.get("time", ""), s["customer"], s["address"], s["mode"],
                        s["sale_type"], items, s["subtotal"], s["discount"], s["total"], s.get("status", "ISSUED")])
    return path
