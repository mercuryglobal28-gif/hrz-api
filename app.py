#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlencode
from itertools import product
import hashlib, base64, json, re, time, os, threading

app = Flask(__name__)

MIRRORS = [
    os.environ.get("BASE_URL", "https://rezka-ua.tv"),
    "https://rezka.ag",
    "https://hdrezka.ag",
]
# إزالة التكرار مع الحفاظ على الترتيب
_seen = set()
MIRRORS = [m for m in MIRRORS if not (m in _seen or _seen.add(m))]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome120"

COOKIES = {}
COOKIE_TS = 0.0
BASE_URL = MIRRORS[0]
COOKIE_TTL = int(os.environ.get("COOKIE_TTL", "1800"))
_lock = threading.Lock()


def is_blocked(text: str) -> bool:
    if not text:
        return True
    t = text.lower()
    markers = [
        "проверяем",
        "anubis_challenge",
        "что вы не бот",
        "techaro",
        "ошибка доступа",
        "access denied",
        "cf-browser-verification",
    ]
    if any(x in t for x in markers):
        return True
    # صفحة قصيرة بلا نتائج بحث معتادة
    if "b-content__inline_item" not in text and "b-search__title" not in text:
        if "doctype" in t and len(text) < 8000 and ("бот" in t or "challenge" in t):
            return True
    return False


def clear_trash(data: str) -> str:
    if not data:
        return ""
    if data.startswith("[") or data.startswith("http"):
        return data
    trash_list = ["@", "#", "!", "^", "$"]
    trash_codes = [
        base64.b64encode("".join(chars).encode()).decode()
        for i in range(2, 4)
        for chars in product(trash_list, repeat=i)
    ]
    s = "".join(data.replace("#h", "").split("//_//"))
    for code in trash_codes:
        s = s.replace(code, "")
    try:
        pad = "=" * ((4 - len(s) % 4) % 4)
        return base64.b64decode(s + pad).decode("utf-8", errors="ignore")
    except Exception:
        try:
            return base64.b64decode(s + "==").decode("utf-8", errors="ignore")
        except Exception:
            return s


def parse_streams(raw_url: str) -> dict:
    streams = {}
    for part in clear_trash(raw_url).split(","):
        part = part.strip()
        if "[" not in part or "]" not in part:
            continue
        try:
            after = part.split("[", 1)[1]
            quality, urls_part = after.split("]", 1)
            quality = re.sub(r"<[^>]*>", "", quality).strip()
            urls = [
                u.strip().replace("\\/", "/")
                for u in urls_part.split(" or ")
                if u.strip().startswith("http")
            ]
            if quality and urls:
                streams[quality] = urls
        except Exception:
            continue
    return streams


def solve_anubis_pow(random_data: str, difficulty: int = 5):
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if h.startswith(prefix):
            return h, nonce
        nonce += 1
        if nonce > 50_000_000:
            raise RuntimeError("PoW too hard")


def make_session():
    return curl_requests.Session(
        impersonate=IMPERSONATE,
        cookies=dict(COOKIES),
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


def renew_cookies(base_url: str = None) -> bool:
    """تجديد ناجح فقط إذا حصلنا على كوكيز غير فارغة"""
    global COOKIES, COOKIE_TS, BASE_URL
    base_url = base_url or BASE_URL
    print(f"[*] تجديد الكوكيز على {base_url}")

    s = curl_requests.Session(
        impersonate=IMPERSONATE,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        r = s.get(base_url + "/", timeout=25)
        jar = dict(s.cookies)

        if not is_blocked(r.text) and jar:
            with _lock:
                COOKIES = jar
                COOKIE_TS = time.time()
                BASE_URL = base_url
            print(f"[+] جلسة بدون تحدّي — {len(jar)} كوكي — {base_url}")
            return True

        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", id="anubis_challenge")
        if not script or not script.string:
            print("[-] لا anubis_challenge")
            # حتى لو في كوكيز أولية
            if jar:
                with _lock:
                    COOKIES = jar
                    COOKIE_TS = time.time()
                    BASE_URL = base_url
                return True
            return False

        data = json.loads(script.string.strip())
        random_data = data["challenge"]["randomData"]
        challenge_id = data["challenge"]["id"]
        difficulty = data.get("rules", {}).get("difficulty", 5)

        t0 = time.time()
        hash_hex, nonce = solve_anubis_pow(random_data, difficulty)
        elapsed = int((time.time() - t0) * 1000) + 500

        params = {
            "id": challenge_id,
            "response": hash_hex,
            "nonce": str(nonce),
            "redir": base_url + "/",
            "elapsedTime": str(elapsed),
        }
        pass_url = f"{base_url}/.within.website/x/cmd/anubis/api/pass-challenge?{urlencode(params)}"
        s.get(
            pass_url,
            headers={"User-Agent": USER_AGENT, "Referer": base_url + "/"},
            timeout=20,
            allow_redirects=True,
        )
        jar = dict(s.cookies)

        if not jar:
            print("[-] التحدي تم لكن الكوكيز فارغة")
            return False

        with _lock:
            COOKIES = jar
            COOKIE_TS = time.time()
            BASE_URL = base_url
        print(f"[+] تم التخطي — {len(jar)} كوكي — {base_url}")
        return True
    except Exception as e:
        print(f"[-] خطأ Anubis: {e}")
        return False


def ensure_cookies(force: bool = False) -> bool:
    global COOKIES, COOKIE_TS
    with _lock:
        has = bool(COOKIES)
        age_ok = COOKIE_TS and (time.time() - COOKIE_TS <= COOKIE_TTL)
        need = force or (not has) or (not age_ok)

    if not need:
        print(f"[*] جلسة حالية — cookies={len(COOKIES)}")
        return True

    # جرّب كل المرايا حتى تنجح واحدة
    for mirror in MIRRORS:
        if renew_cookies(mirror):
            return True
    return False


def http(method: str, url: str, **kwargs):
    for attempt in range(3):
        s = make_session()
        r = s.get(url, timeout=20, **kwargs) if method.upper() == "GET" else s.post(url, timeout=20, **kwargs)
        text = r.text
        expired = "сессии истекло" in text.lower() or (
            '"success":false' in text and "обновите" in text.lower()
        )
        if is_blocked(text) or expired:
            print(f"[!] حماية/جلسة — تجديد ({attempt + 1})")
            if not ensure_cookies(force=True):
                raise RuntimeError("فشل تجاوز الحماية")
            continue
        # حدّث الكوكيز من الرد إن وُجدت
        try:
            jar = dict(s.cookies)
            if jar:
                with _lock:
                    COOKIES.update(jar)
        except Exception:
            pass
        return r
    raise RuntimeError("فشل الطلب")


def search(query: str):
    url = f"{BASE_URL}/search/?do=search&subaction=search&q={quote_plus(query)}"
    r = http("GET", url)
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    item = soup.find("div", class_="b-content__inline_item") or soup.select_one(
        ".b-content__inline_item"
    )
    if not item:
        return None, {
            "blocked": is_blocked(html),
            "title": (soup.title.string if soup.title else None),
            "len": len(html),
            "has_item": "b-content__inline_item" in html,
            "base_url": BASE_URL,
            "snippet": html[:400],
        }

    movie_url = item.get("data-url")
    if not movie_url:
        a = item.find("a", href=True)
        movie_url = a["href"] if a else None
    if not movie_url:
        return None, {"error": "no data-url", "base_url": BASE_URL}
    if not movie_url.startswith("http"):
        movie_url = BASE_URL + movie_url
    title = item.get("data-title") or query
    return {"title": title, "url": movie_url}, None


def get_streams(movie_url: str, season="1", episode="1"):
    r = http("GET", movie_url)
    soup = BeautifulSoup(r.text, "html.parser")
    m = re.search(r"/(\d+)-", movie_url)
    if not m:
        raise RuntimeError("فشل استخراج ID")
    movie_id = m.group(1)

    is_series = any(x in movie_url for x in ["/series/", "/cartoons/", "/animation/"])
    action = "get_stream" if is_series else "get_movie"

    ordered = []
    node = soup.select_one("li.b-translator__item.active")
    if node and node.get("data-translator_id"):
        ordered.append(node["data-translator_id"])
    for li in soup.select("li.b-translator__item"):
        tid = li.get("data-translator_id")
        if tid and tid not in ordered:
            ordered.append(tid)
    if not ordered:
        ordered = ["238", "1", "56"]

    for tid in ordered:
        payload = {"id": movie_id, "translator_id": tid, "action": action}
        if is_series:
            payload["season"] = str(season)
            payload["episode"] = str(episode)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": movie_url,
        }
        api = http("POST", f"{BASE_URL}/ajax/get_cdn_series/", data=payload, headers=headers)
        try:
            data = api.json()
        except Exception:
            continue
        if data.get("success") is False:
            if "сессии" in str(data.get("message", "")).lower():
                ensure_cookies(force=True)
            continue
        if data.get("url"):
            streams = parse_streams(data["url"])
            if streams:
                return {
                    "id": movie_id,
                    "translator_id": tid,
                    "streams": streams,
                    "subtitles": [
                        s.strip()
                        for s in (data.get("subtitle") or "").replace("\\/", "/").split(",")
                        if s.strip()
                    ],
                }
    return None


@app.route("/")
def index():
    return jsonify({
        "message": "HDRezka Stream API",
        "usage": "/api?q=NAME&season=1&episode=1",
        "health": "/health",
    })


@app.route("/health")
def health():
    age = int(time.time() - COOKIE_TS) if COOKIE_TS else None
    return jsonify({
        "status": "ok",
        "cookies": len(COOKIES),
        "cookie_keys": list(COOKIES.keys()),
        "cookie_age_seconds": age,
        "cookie_ttl": COOKIE_TTL,
        "base_url": BASE_URL,
        "mirrors": MIRRORS,
    })


@app.route("/api")
def api():
    query = (request.args.get("q") or "").strip()
    season = request.args.get("season", "1")
    episode = request.args.get("episode", "1")
    if not query:
        return jsonify({"success": False, "error": "q required"}), 400

    try:
        if not ensure_cookies(force=False):
            return jsonify({
                "success": False,
                "error": "فشل تجديد الكوكيز (Anubis)",
                "hint": "IP الاستضافة قد يكون محظوراً من Rezka",
            }), 503

        if not COOKIES:
            return jsonify({
                "success": False,
                "error": "جلسة فارغة",
                "hint": "ensure_cookies نجح شكلياً بلا كوكيز — هذا لا يجب أن يحدث",
            }), 503

        result, debug = search(query)
        if not result:
            # محاولة مرآة أخرى
            ensure_cookies(force=True)
            result, debug = search(query)

        if not result:
            return jsonify({
                "success": False,
                "error": "لا توجد نتائج",
                "debug": debug,
            }), 404

        info = get_streams(result["url"], season, episode)
        if not info or not info.get("streams"):
            ensure_cookies(force=True)
            info = get_streams(result["url"], season, episode)

        if not info or not info.get("streams"):
            return jsonify({
                "success": False,
                "error": "لا توجد روابط بث",
                "title": result.get("title"),
                "url": result.get("url"),
            }), 404

        return jsonify({
            "success": True,
            "title": result["title"],
            "url": result["url"],
            "id": info["id"],
            "translator_id": info["translator_id"],
            "mirror": BASE_URL,
            "streams": info["streams"],
            "subtitles": info.get("subtitles") or [],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
