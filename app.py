#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بروكسي بسيط — يجلب أي رابط عبر السيرفر ويعيده كما هو
الاستخدام:
  /?url=https://example.com
  /proxy?url=https://example.com/path
"""

from flask import Flask, request, Response, jsonify
import requests
from urllib.parse import urlparse, urljoin
import re

app = Flask(__name__)

# حد أقصى لحجم الرد (حماية)
MAX_BODY = 20 * 1024 * 1024  # 20 MB

# رؤوس لا نمررها من العميل
HOP_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "content-length", "accept-encoding",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_safe_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        if not p.netloc:
            return False
        # منع العناوين الداخلية الشائعة
        host = p.hostname or ""
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False
        if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
            return False
        return True
    except Exception:
        return False


def build_headers():
    headers = {"User-Agent": USER_AGENT}
    # تمرير بعض رؤوس العميل المفيدة
    for key, value in request.headers:
        lk = key.lower()
        if lk in HOP_HEADERS:
            continue
        if lk in ("user-agent", "cookie", "referer", "accept", "accept-language", "range"):
            headers[key] = value
    # Cookie من معامل الاستعلام إن وُجد
    qcookie = request.args.get("cookie")
    if qcookie:
        headers["Cookie"] = qcookie
    return headers


@app.route("/")
@app.route("/proxy")
def proxy():
    target = request.args.get("url", "").strip()
    if not target:
        return jsonify({
            "message": "Simple HTTP Proxy",
            "usage": {
                "browse": "/?url=https://example.com",
                "with_cookie": "/?url=https://example.com&cookie=PHPSESSID=xxx",
            },
            "health": "/health",
        })

    if not target.startswith("http"):
        target = "https://" + target

    if not is_safe_url(target):
        return jsonify({"error": "invalid or blocked url"}), 400

    try:
        method = request.method if request.method in ("GET", "POST", "HEAD") else "GET"
        kwargs = {
            "headers": build_headers(),
            "timeout": 30,
            "allow_redirects": True,
            "stream": True,
        }
        if method == "POST":
            kwargs["data"] = request.get_data()
            if request.content_type:
                kwargs["headers"]["Content-Type"] = request.content_type

        r = requests.request(method, target, **kwargs)

        # جمع الجسم مع حد أقصى
        chunks = []
        size = 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_BODY:
                return jsonify({"error": "response too large"}), 502
            chunks.append(chunk)
        body = b"".join(chunks)

        # رؤوس الرد
        out_headers = {}
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        out_headers["Content-Type"] = content_type

        # للصفحات HTML: إعادة كتابة الروابط النسبية بشكل بسيط (اختياري)
        rewrite = request.args.get("rewrite", "1") == "1"
        if rewrite and "text/html" in content_type.lower():
            try:
                text = body.decode(r.encoding or "utf-8", errors="replace")
                base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
                # استبدال الروابط المطلقة الشائعة لتمر عبر البروكسي
                def repl_abs(m):
                    u = m.group(1)
                    return f'{m.group(0).split("=")[0]}="{request.host_url.rstrip("/")}?url={u}"'

                # روابط href/src المطلقة http
                text = re.sub(
                    r'(?:href|src)=["\'](https?://[^"\']+)["\']',
                    lambda m: f'{m.group(0).split("=")[0]}="{request.url_root.rstrip("/")}?url={m.group(1)}"',
                    text,
                    flags=re.I,
                )
                body = text.encode("utf-8", errors="replace")
                out_headers["Content-Type"] = "text/html; charset=utf-8"
            except Exception:
                pass

        out_headers["Access-Control-Allow-Origin"] = "*"
        # تمرير بعض الرؤوس المفيدة
        for h in ("Content-Disposition", "Accept-Ranges", "Content-Range"):
            if h in r.headers:
                out_headers[h] = r.headers[h]

        return Response(body, status=r.status_code, headers=out_headers)

    except requests.exceptions.Timeout:
        return jsonify({"error": "timeout"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "proxy"})


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/", methods=["OPTIONS"])
@app.route("/proxy", methods=["OPTIONS"])
def options():
    return ("", 204)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
