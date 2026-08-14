#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlencode
from itertools import product
import hashlib, base64, json, re, time, os

app = Flask(__name__)

BASE_URL = os.environ.get("BASE_URL", "https://rezka-ua.tv")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome120"
COOKIES = {}
COOKIE_TS = 0
COOKIE_TTL = 1800  # 30 دقيقة

def is_blocked(text: str) -> bool:
    if not text:
        return True
    t = text.lower()
    return any(x in t for x in ["проверяем", "anubis_challenge", "что вы не бот", "techaro"])

def clear_trash(data: str) -> str:
    if not data:
        return ""
    if data.startswith("[") or data.startswith("http"):
        return data
    trash_list = ["@", "#", "!", "^", "$"]
    trash_codes = []
    for i in range(2, 4):
        for chars in product(trash_list, repeat=i):
            trash_codes.append(base64.b64encode("".join(chars).encode()).decode())
    s = data.replace("#h", "")
    trash_string = "".join(s.split("//_//"))
    for code in trash_codes:
        trash_string = trash_string.replace(code, "")
    try:
        pad = "=" * ((4 - len(trash_string) % 4) % 4)
        return base64.b64decode(trash_string + pad).decode("utf-8", errors="ignore")
    except Exception:
        try:
            return base64.b64decode(trash_string + "==").decode("utf-8", errors="ignore")
        except Exception:
            return trash_string

def parse_streams(raw_url: str) -> dict:
    streams = {}
    cleaned = clear_trash(raw_url)
    for part in cleaned.split(","):
        part = part.strip()
        if "[" not in part or "]" not in part:
            continue
        try:
            after = part.split("[", 1)[1]
            quality, urls_part = after.split("]", 1)
            quality = re.sub(r"<[^>]*>", "", quality).strip()
            url_list = [
                u.strip().replace("\\/", "/")
                for u in urls_part.split(" or ")
                if u.strip().startswith("http")
            ]
            if quality and url_list:
                streams[quality] = url_list
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
        cookies=COOKIES,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )

def renew_cookies() -> bool:
    global COOKIES, COOKIE_TS
    s = curl_requests.Session(
        impersonate=IMPERSONATE,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        r = s.get(BASE_URL + "/", timeout=25)
        if not is_blocked(r.text):
            COOKIES = dict(s.cookies)
            COOKIE_TS = time.time()
            return True

        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", id="anubis_challenge")
        if not script or not script.string:
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
            "redir": BASE_URL + "/",
            "elapsedTime": str(elapsed),
        }
        pass_url = f"{BASE_URL}/.within.website/x/cmd/anubis/api/pass-challenge?{urlencode(params)}"
        s.get(pass_url, headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"},
              timeout=20, allow_redirects=True)
        COOKIES = dict(s.cookies)
        COOKIE_TS = time.time()
        return bool(COOKIES)
    except Exception as e:
        print(f"Anubis error: {e}")
        return False

def ensure_cookies(force=False):
    global COOKIES, COOKIE_TS
    if force or not COOKIES or (time.time() - COOKIE_TS > COOKIE_TTL):
        return renew_cookies()
    return True

def http(method, url, **kwargs):
    for attempt in range(3):
        s = make_session()
        r = s.get(url, timeout=20, **kwargs) if method == "GET" else s.post(url, timeout=20, **kwargs)
        text = r.text
        expired = "сессии истекло" in text.lower()
        if is_blocked(text) or expired:
            if not renew_cookies():
                raise RuntimeError("فشل تجاوز الحماية")
            continue
        return r
    raise RuntimeError("فشل الطلب")

def search(query: str):
    url = f"{BASE_URL}/search/?do=search&subaction=search&q={quote_plus(query)}"
    r = http("GET", url)
    soup = BeautifulSoup(r.text, "html.parser")
    item = soup.find("div", class_="b-content__inline_item") or soup.select_one(".b-content__inline_item")
    if not item:
        return None
    movie_url = item.get("data-url")
    if not movie_url:
        a = item.find("a", href=True)
        movie_url = a["href"] if a else None
    if not movie_url:
        return None
    if not movie_url.startswith("http"):
        movie_url = BASE_URL + movie_url
    title = item.get("data-title") or query
    return {"title": title, "url": movie_url}

def get_streams(movie_url: str, season="1", episode="1"):
    r = http("GET", movie_url)
    soup = BeautifulSoup(r.text, "html.parser")
    m = re.search(r"/(\d+)-", movie_url)
    if not m:
        raise RuntimeError("فشل استخراج ID")
    movie_id = m.group(1)

    is_series = any(x in movie_url for x in ["/series/", "/cartoons/", "/animation/"])
    action = "get_stream" if is_series else "get_movie"

    translators = []
    for li in soup.select("li.b-translator__item"):
        tid = li.get("data-translator_id")
        if tid:
            translators.append(tid)

    ordered = []
    node = soup.select_one("li.b-translator__item.active")
    if node and node.get("data-translator_id"):
        ordered.append(node["data-translator_id"])
    for t in translators:
        if t not in ordered:
            ordered.append(t)
    if not ordered:
        ordered = ["238", "1"]

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
    return jsonify({
        "status": "ok",
        "cookies": len(COOKIES),
        "base_url": BASE_URL,
    })

@app.route("/api")
def api():
    query = (request.args.get("q") or "").strip()
    season = request.args.get("season", "1")
    episode = request.args.get("episode", "1")

    if not query:
        return jsonify({"success": False, "error": "q required"}), 400

    try:
        if not ensure_cookies():
            return jsonify({"success": False, "error": "Anubis failed"}), 503

        result = search(query)
        if not result:
            return jsonify({"success": False, "error": "لا توجد نتائج"}), 404

        info = get_streams(result["url"], season, episode)
        if not info or not info.get("streams"):
            return jsonify({"success": False, "error": "لا توجد روابط بث"}), 404

        return jsonify({
            "success": True,
            "title": result["title"],
            "url": result["url"],
            "id": info["id"],
            "translator_id": info["translator_id"],
            "streams": info["streams"],
            "subtitles": info.get("subtitles") or [],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
