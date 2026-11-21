"""Where products, the invoice counter and the sales ledger are kept.

* Local computer: plain JSON files (default).
* Hosted online (Render etc.): the disk is wiped on every restart, so data goes to a free Supabase
  database instead. Set SUPABASE_URL + SUPABASE_KEY and the bot switches automatically.
"""
import json
import logging
import os

import httpx

import config

log = logging.getLogger("esmo-bot")


class FileStore:
    name = "local files"
    _paths = {"products": os.path.join(config.BASE_DIR, "products.json"),
              "state": os.path.join(config.DATA_DIR, "state.json"),
              "allowed": os.path.join(config.DATA_DIR, "allowed.json")}
    _ledger = os.path.join(config.DATA_DIR, "ledger.json")

    @staticmethod
    def _read(path, default):
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def get_kv(self, key, default=None):
        return self._read(self._paths[key], default)

    def set_kv(self, key, value):
        self._write(self._paths[key], value)

    def add_sale(self, order):
        ledger = self._read(self._ledger, [])
        ledger.append(order)
        self._write(self._ledger, ledger)

    def void_sale(self, inv):
        ledger = self._read(self._ledger, [])
        for s in ledger:
            if s["inv"] == inv:
                s["status"] = "VOID"
                self._write(self._ledger, ledger)
                return True
        return False

    def sales_on(self, day):
        return [s for s in self._read(self._ledger, []) if s["date"] == day]

    def all_sales(self):
        return self._read(self._ledger, [])


class SupabaseStore:
    """Talks to Supabase's REST API (PostgREST) with plain httpx. Tables: see supabase_setup.sql."""
    name = "Supabase database"

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):                 # legacy JWT-style keys also go in Authorization
            self.headers["Authorization"] = f"Bearer {key}"

    def _req(self, method, table, params=None, body=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            r = httpx.request(method, f"{self.base}/{table}", params=params, json=body, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            raise RuntimeError(f"database unreachable ({type(e).__name__})") from None
        if r.status_code >= 400:
            raise RuntimeError(f"database error HTTP {r.status_code}")   # body not logged
        return r.json() if r.content else None

    def get_kv(self, key, default=None):
        rows = self._req("GET", "kv", params={"key": f"eq.{key}", "select": "value"})
        return rows[0]["value"] if rows else default

    def set_kv(self, key, value):
        self._req("POST", "kv", params={"on_conflict": "key"}, body={"key": key, "value": value},
                  prefer="resolution=merge-duplicates,return=minimal")

    def add_sale(self, order):
        self._req("POST", "sales", body={"inv": order["inv"], "sale_date": order["date"],
                                         "status": order.get("status", "ISSUED"), "data": order},
                  prefer="return=minimal")

    def void_sale(self, inv):
        rows = self._req("PATCH", "sales", params={"inv": f"eq.{inv}"}, body={"status": "VOID"},
                         prefer="return=representation")
        return bool(rows)

    @staticmethod
    def _order(row):
        return {**row["data"], "status": row["status"]}

    def sales_on(self, day):
        rows = self._req("GET", "sales", params={"sale_date": f"eq.{day}", "select": "data,status", "order": "inv.asc"})
        return [self._order(r) for r in rows or []]

    def all_sales(self):
        out, offset = [], 0
        while True:
            rows = self._req("GET", "sales", params={"select": "data,status", "order": "inv.asc",
                                                     "limit": 1000, "offset": offset})
            out += [self._order(r) for r in rows or []]
            if not rows or len(rows) < 1000:
                return out
            offset += 1000


def make_store():
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        return SupabaseStore(config.SUPABASE_URL, config.SUPABASE_KEY)
    return FileStore()


STORE = make_store()
