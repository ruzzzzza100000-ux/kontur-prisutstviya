#!/usr/bin/env python3
"""Local MVP backend for Kontur Presence. Python standard library only."""
import csv
import io
import json
import re
import html
import ipaddress
import os
import socket
import sqlite3
import threading
import time
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


# ---------------------------------------------------------------------------
# Site scanner: reads the company's home page and extracts public profile links
# and analytics counters. No API keys required.
# ---------------------------------------------------------------------------
SCAN_MAX_BYTES = 1_500_000
SCAN_TIMEOUT = 10
SCAN_LIMIT_PER_MIN = 30
_scan_hits = []
_scan_lock = threading.Lock()

def scan_allowed():
    """Global (not per-IP) limit: the app has no auth, so keep the endpoint from being used as a free crawler."""
    now = time.time()
    with _scan_lock:
        _scan_hits[:] = [t for t in _scan_hits if now - t < 60]
        if len(_scan_hits) >= SCAN_LIMIT_PER_MIN:
            return False
        _scan_hits.append(now)
        return True

def normalize_site(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Введите адрес сайта")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    if not urllib.parse.urlparse(raw).hostname:
        raise ValueError("Некорректный адрес сайта")
    return raw

def assert_public_host(url):
    """SSRF guard: only public http(s) hosts on standard ports. Checked again on every redirect."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Допустимы только адреса http:// и https://")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Некорректный порт в адресе")
    if port not in (80, 443):
        raise ValueError("Сайты с нестандартным портом не сканируются")
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise ValueError("Не удалось найти сайт по этому адресу. Проверьте, что домен написан без ошибок.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError("Адрес ведёт во внутреннюю сеть, такие сайты не сканируются")

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch_site(url):
    assert_public_host(url)
    opener = urllib.request.build_opener(SafeRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; KonturPresenceMVP/0.3)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru,en;q=0.8",
    })
    with opener.open(req, timeout=SCAN_TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            raise ValueError("По этому адресу открывается не веб-страница")
        raw = resp.read(SCAN_MAX_BYTES + 1)[:SCAN_MAX_BYTES]
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return resp.geturl(), text

SKIP_PATH_PARTS = ("/share", "/away.php", "/widget", "/intent/", "/sharer", "/dialog/", "/login")

def classify_link(url):
    """Return a platform name for a public company-profile link, or None."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    low = (p.path or "/").lower()
    if any(x in low for x in SKIP_PATH_PARTS):
        return None
    has_path = bool(low.strip("/"))
    def on(*domains):
        return any(host == d or host.endswith("." + d) for d in domains)
    if on("2gis.ru", "2gis.com", "2gis.kz", "2gis.ae"):
        return "2ГИС" if has_path else None
    if on("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru"):
        if low.startswith(("/maps/org/", "/maps/-/")):
            return "Яндекс Карты"
        if low.startswith(("/profile/", "/sprav/")):
            return "Яндекс Бизнес"
        return None
    if on("vk.com", "vk.ru"):
        return "VK" if has_path else None
    if on("t.me", "telegram.me"):
        return "Telegram" if has_path else None
    if on("ok.ru"):
        return "Одноклассники" if has_path else None
    if on("youtube.com", "youtu.be"):
        return "YouTube" if low.startswith(("/@", "/channel/", "/c/", "/user/")) else None
    if on("dzen.ru", "zen.yandex.ru"):
        return "Дзен" if has_path else None
    if on("wa.me", "whatsapp.com"):
        return "WhatsApp" if has_path else None
    if on("avito.ru"):
        return "Авито" if low.startswith(("/brands/", "/user/")) else None
    for dom, name in (("flamp.ru", "Flamp"), ("zoon.ru", "Zoon"), ("yell.ru", "Yell"),
                      ("otzovik.com", "Отзовик"), ("irecommend.ru", "IRecommend")):
        if on(dom):
            return name if has_path else None
    return None

def clean_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None

def walk_jsonld(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "sameAs":
                if isinstance(v, str):
                    out["same_as"].append(v)
                elif isinstance(v, list):
                    out["same_as"].extend(x for x in v if isinstance(x, str))
            elif k == "telephone" and isinstance(v, str):
                out["phones"].append(v)
            else:
                walk_jsonld(v, out)
    elif isinstance(node, list):
        for x in node:
            walk_jsonld(x, out)

COUNTER_PATTERNS = [
    ("Яндекс Метрика", [r"\bym\(\s*(\d{5,10})\s*,", r"mc\.yandex\.ru/watch/(\d{5,10})", r"yaCounter(\d{5,10})"]),
    ("Google Analytics", [r"gtag/js\?id=((?:G|UA|AW)-[A-Z0-9-]{4,15})", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]((?:G|UA|AW)-[A-Z0-9-]{4,15})"]),
    ("Google Tag Manager", [r"\b(GTM-[A-Z0-9]{4,8})\b"]),
    ("Top.Mail.Ru / VK-пиксель", [r"top-fwz1\.mail\.ru/counter\?id=(\d{5,10})", r"_tmr[^;]{0,80}?\bid\s*:\s*['\"]?(\d{5,10})", r"VK\.Retargeting\.Init\(\s*['\"]?([\w-]{5,20})"]),
]

def extract_presence(page, base_url):
    """Pure function: HTML text -> profiles, counters, phones. Easy to unit-test."""
    candidates = []
    for m in re.finditer(r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", page, re.I | re.S):
        candidates.append(html.unescape((m.group(1) or m.group(2) or "").strip()))
    out = {"same_as": [], "phones": []}
    for m in re.finditer(r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""", page, re.I | re.S):
        try:
            walk_jsonld(json.loads(m.group(1).strip()), out)
        except Exception:
            continue
    candidates.extend(out["same_as"])

    phones, seen_phone = [], set()
    raw_phones = [h[4:] for h in candidates if h.lower().startswith("tel:")] + out["phones"]
    for raw in raw_phones:
        ph = clean_phone(urllib.parse.unquote(raw))
        if ph and ph not in seen_phone:
            seen_phone.add(ph)
            phones.append(ph)

    profiles, seen = [], set()
    for href in candidates:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        pu = urllib.parse.urlparse(absolute)
        if pu.scheme not in ("http", "https"):
            continue
        platform = classify_link(absolute)
        if not platform:
            continue
        clean = urllib.parse.urlunparse((pu.scheme, pu.netloc, pu.path.rstrip("/") or "/", "", "", ""))
        key = (platform, clean.lower().replace("://www.", "://"))
        if key in seen:
            continue
        seen.add(key)
        profiles.append({"platform": platform, "url": clean})
        if len(profiles) >= 30:
            break

    counters, seen_c = [], set()
    for name, patterns in COUNTER_PATTERNS:
        for pat in patterns:
            for m in re.finditer(pat, page):
                cid = m.group(1)
                if (name, cid) not in seen_c:
                    seen_c.add((name, cid))
                    counters.append({"type": name, "id": cid})

    tm = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", tm.group(1))).strip()[:200] if tm else ""
    return {"title": title, "profiles": profiles, "counters": counters, "phones": phones[:5]}

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
                # Free public web search fallback. Availability and anti-bot rules vary;
                # results are candidates, not verified company ownership.
                try:
                    ddg_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": search_text})
                    req = urllib.request.Request(ddg_url, headers={"User-Agent": "Mozilla/5.0 (compatible; KonturPresenceMVP/0.2)", "Accept-Language": "ru,en;q=0.8"})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        page = resp.read().decode("utf-8", errors="replace")
                    results = []
                    blocks = re.findall(r'<div class="result[^"]*".*?</div>\s*</div>', page, flags=re.S)
                    for block in blocks[:10]:
                        m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
                        if not m:
                            m = re.search(r'<a[^>]+href="([^"]+)"[^>]+class="result__a"[^>]*>(.*?)</a>', block, flags=re.S)
                        if not m: continue
                        link = html.unescape(m.group(1))
                        if "uddg=" in link:
                            try: link = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("uddg", [link])[0]
                            except Exception: pass
                        if not link.startswith(("http://", "https://")): continue
                        title = re.sub(r"<[^>]+>", "", m.group(2))
                        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
                        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>', block, flags=re.S)
                        desc = html.unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip() if sm else ""
                        results.append({"title": title, "url": link, "description": desc, "source": "DuckDuckGo"})
                    return send_json(self, 200, {
                        "results": results,
                        "message": f"Найдено кандидатов: {len(results)} через бесплатный веб-поиск. Совпадения нужно подтвердить вручную.",
                        "search_configured": False,
                        "provider": "duckduckgo"
                    })
                except Exception as e:
                    return send_json(self, 200, {
                        "results": [],
                        "message": "Бесплатный поисковый источник недоступен с сервера. Откройте персональные поисковые ссылки ниже; для стабильного автопоиска подключите Search API.",
                        "search_configured": False,
                        "detail": str(e)
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
        if parsed.path == "/api/scan":
            if not scan_allowed():
                return send_json(self, 429, {"error": "Слишком много проверок подряд. Подождите минуту и повторите."})
            try:
                start_url = normalize_site(str(data.get("url", "")))
                final_url, page = fetch_site(start_url)
                result = extract_presence(page, final_url)
                result["site"] = final_url
                return send_json(self, 200, result)
            except ValueError as e:
                return send_json(self, 400, {"error": str(e)})
            except Exception as e:
                return send_json(self, 502, {"error": "Не удалось открыть сайт. Он может быть недоступен или закрыт от роботов.", "detail": str(e)})
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