#!/usr/bin/env python3
"""Local MVP backend for Kontur Presence. Python standard library only."""
import csv
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", "/tmp/kontur.db")).expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", "8765"))
BRAVE_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'Другое',
            name TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'Ручное добавление',
            company TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(url)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            spend REAL NOT NULL DEFAULT 0,
            clicks REAL NOT NULL DEFAULT 0,
            leads REAL NOT NULL DEFAULT 0,
            sales REAL NOT NULL DEFAULT 0,
            period_days INTEGER NOT NULL DEFAULT 30,
            imported_from TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")

def send_json(handler, code, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)

def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 8_000_000:
        raise ValueError("Request too large")
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))

def guess_platform(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "2gis" in host: return "2ГИС"
    if "yandex" in host or host.endswith("ya.ru"): return "Яндекс"
    if "vk.com" in host: return "VK"
    return "Сайт / другое"

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[http]", fmt % args)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            return send_json(self, 200, {"ok": True, "search_configured": bool(BRAVE_KEY), "database": str(DB_PATH.name)})
        if parsed.path == "/api/profiles":
            with db() as c:
                rows = c.execute("SELECT * FROM profiles ORDER BY id DESC").fetchall()
            return send_json(self, 200, {"profiles": [dict(r) for r in rows]})
        if parsed.path == "/api/analytics":
            qs = urllib.parse.parse_qs(parsed.query)
            days = max(1, min(3650, int(qs.get("days", ["30"])[0])))
            with db() as c:
                rows = c.execute("""SELECT channel, SUM(spend) spend, SUM(clicks) clicks,
                    SUM(leads) leads, SUM(sales) sales FROM metrics
                    WHERE period_days = ? GROUP BY channel ORDER BY SUM(spend) DESC""", (days,)).fetchall()
            totals = {"spend": 0, "clicks": 0, "leads": 0, "sales": 0}
            for r in rows:
                for k in ("spend", "clicks", "leads", "sales"):
                    totals[k] += float(r[k] or 0)
            totals["channels"] = [dict(r) for r in rows]
            totals["cpl"] = totals["spend"] / totals["leads"] if totals["leads"] else None
            totals["lead_to_sale_pct"] = totals["sales"] / totals["leads"] * 100 if totals["leads"] else None
            return send_json(self, 200, totals)
        if parsed.path == "/api/export":
            out = io.StringIO(newline="")
            writer = csv.writer(out, delimiter=";")
            writer.writerow(["Тип записи", "Площадка/канал", "Название/источник", "URL", "Расходы ₽", "Клики", "Лиды", "Продажи", "Период дней", "Дата"])
            with db() as c:
                for r in c.execute("SELECT platform,name,url,source,created_at FROM profiles ORDER BY id"):
                    writer.writerow(["Профиль", r["platform"], r["name"] or r["source"], r["url"], "", "", "", "", "", r["created_at"]])
                for r in c.execute("SELECT channel,spend,clicks,leads,sales,period_days,imported_from,created_at FROM metrics ORDER BY id"):
                    writer.writerow(["Метрики", r["channel"], r["imported_from"], "", r["spend"], r["clicks"], r["leads"], r["sales"], r["period_days"], r["created_at"]])
            return send_json(self, 200, {"csv": out.getvalue()})
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = read_json(self)
        except Exception as e:
            return send_json(self, 400, {"error": "Invalid JSON", "detail": str(e)})
        if parsed.path == "/api/search":
            query = str(data.get("query", "")).strip()
            city = str(data.get("city", "")).strip()
            typ = str(data.get("type", "name"))
            if not query:
                return send_json(self, 400, {"error": "Введите сайт, телефон или название"})
            search_text = query if typ == "site" else f'"{query}"'
            if city: search_text += " " + city
            if not BRAVE_KEY:
                return send_json(self, 200, {
                    "results": [],
                    "message": "Автоматический поиск не настроен: добавьте BRAVE_SEARCH_API_KEY. Используйте поисковые ссылки в интерфейсе.",
                    "search_configured": False
                })
            try:
                url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": search_text, "count": 10})
                req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY, "User-Agent": "KonturPresenceMVP/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                results = []
                for r in payload.get("web", {}).get("results", []):
                    link = r.get("url", "")
                    if not link.startswith(("http://", "https://")): continue
                    results.append({"title": r.get("title", ""), "url": link, "description": r.get("description", ""), "source": "Brave Search"})
                return send_json(self, 200, {"results": results, "message": f"Найдено результатов: {len(results)}. Проверьте, что профиль принадлежит нужной компании.", "search_configured": True})
            except Exception as e:
                return send_json(self, 502, {"error": "Search provider error", "detail": str(e), "results": []})
        if parsed.path == "/api/profiles":
            url = str(data.get("url", "")).strip()
            if urllib.parse.urlparse(url).scheme not in ("http", "https") or not urllib.parse.urlparse(url).netloc:
                return send_json(self, 400, {"error": "URL должен начинаться с http:// или https://"})
            platform = str(data.get("platform") or guess_platform(url))[:100]
            name = str(data.get("name", ""))[:300]
            source = str(data.get("source", "Ручное добавление"))[:100]
            company = str(data.get("company", ""))[:300]
            city = str(data.get("city", ""))[:200]
            try:
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO profiles(platform,name,url,source,company,city) VALUES(?,?,?,?,?,?)",
                              (platform, name, url, source, company, city))
                    row = c.execute("SELECT * FROM profiles WHERE url=?", (url,)).fetchone()
                return send_json(self, 201, {"profile": dict(row)})
            except Exception as e:
                return send_json(self, 500, {"error": str(e)})
        if parsed.path == "/api/import":
            filename = str(data.get("filename", "import.csv"))[:200]
            raw = str(data.get("csv", ""))
            if len(raw) > 5_000_000:
                return send_json(self, 413, {"error": "CSV больше 5 МБ"})
            try:
                sample = raw[:4096]
                try: dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                except csv.Error: dialect = csv.excel
                reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
                if not reader.fieldnames:
                    return send_json(self, 400, {"error": "CSV должен содержать заголовки колонок"})
                fields = {str(k).strip().lower(): k for k in reader.fieldnames if k}
                aliases = {
                    "channel": ["channel", "канал", "площадка", "источник"],
                    "spend": ["spend", "расходы", "расход", "затраты", "cost"],
                    "clicks": ["clicks", "клики", "переходы"],
                    "leads": ["leads", "лиды", "заявки", "обращения"],
                    "sales": ["sales", "продажи", "сделки"],
                    "period_days": ["period_days", "период", "дней", "days"]
                }
                mapped = {}
                for target, opts in aliases.items():
                    for opt in opts:
                        if opt in fields:
                            mapped[target] = fields[opt]; break
                if "channel" not in mapped:
                    return send_json(self, 400, {"error": "Не найдена колонка channel/канал/площадка"})
                imported = 0
                with db() as c:
                    for row in reader:
                        def num(key):
                            if key not in mapped: return 0.0
                            val = str(row.get(mapped[key], "") or "0").strip().replace(" ", "").replace(",", ".").replace("₽", "")
                            try: return max(0.0, float(val))
                            except ValueError: return 0.0
                        channel = str(row.get(mapped["channel"], "") or "").strip()
                        if not channel: continue
                        days = int(num("period_days") or 30)
                        c.execute("INSERT INTO metrics(channel,spend,clicks,leads,sales,period_days,imported_from) VALUES(?,?,?,?,?,?,?)",
                                  (channel[:200], num("spend"), num("clicks"), num("leads"), num("sales"), max(1,min(3650,days)), filename))
                        imported += 1
                return send_json(self, 200, {"imported": imported, "filename": filename})
            except Exception as e:
                return send_json(self, 400, {"error": "Не удалось разобрать CSV", "detail": str(e)})
        return send_json(self, 404, {"error": "Unknown endpoint"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        m = __import__("re").fullmatch(r"/api/profiles/(\d+)", parsed.path)
        if not m:
            return send_json(self, 404, {"error": "Unknown endpoint"})
        with db() as c:
            c.execute("DELETE FROM profiles WHERE id=?", (int(m.group(1)),))
        return send_json(self, 200, {"ok": True})

def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Контур присутствия запущен на 0.0.0.0:{PORT}")
    print(f"База данных: {DB_PATH}")
    print("Автопоиск:", "Brave Search API настроен" if BRAVE_KEY else "не настроен (задайте BRAVE_SEARCH_API_KEY)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка сервера…")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
