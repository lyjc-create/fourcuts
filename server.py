#!/usr/bin/env python3
"""Serve the photobooth and keep a local log of each downloaded composite."""
import json
import re
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PNGS = DATA / "pngs"
INDEX = DATA / "records.json"
LOCK = threading.Lock()
MAX_PNG = 12_000_000


def load_records():
    if not INDEX.exists():
        return []
    return json.loads(INDEX.read_text(encoding="utf-8"))


def store_records(records):
    DATA.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/admin":
            self.send_admin()
            return
        if path.startswith("/records/"):
            self.send_record(path[len("/records/"):])
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/records/delete":
            self.delete_record()
            return
        if path != "/api/saves":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_PNG:
            self.send_error(400, "bad size")
            return
        body = self.rfile.read(length)
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            self.send_error(400, "not a png")
            return
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        name = stamp + ".png"
        with LOCK:
            PNGS.mkdir(parents=True, exist_ok=True)
            (PNGS / name).write_bytes(body)
            records = load_records()
            records.append({
                "id": stamp,
                "time": now.isoformat(timespec="seconds"),
                "ip": self.client_address[0],
                "file": name,
                "bytes": len(body),
            })
            store_records(records)
        payload = json.dumps({"ok": True, "id": stamp}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def delete_record(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2000:
            self.send_error(400, "bad size")
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        ident = parse_qs(body).get("id", [""])[0]
        if not re.fullmatch(r"\d{8}-\d{6}-\d{6}", ident):
            self.send_error(400, "bad id")
            return
        with LOCK:
            records = load_records()
            kept = [rec for rec in records if rec.get("id") != ident]
            if len(kept) == len(records):
                self.send_error(404)
                return
            store_records(kept)
            png = PNGS / (ident + ".png")
            if png.is_file():
                png.unlink()
        self.send_response(303)
        self.send_header("Location", "/admin")
        self.end_headers()

    def send_record(self, name):
        if "/" in name or ".." in name or not name.endswith(".png"):
            self.send_error(404)
            return
        path = PNGS / name
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_admin(self):
        records = list(reversed(load_records()))
        rows = []
        for rec in records:
            file_name = escape(rec.get("file", ""))
            rows.append(
                "<tr><td>{time}</td><td>{ip}</td>"
                "<td><a href=\"/records/{file}\"><img src=\"/records/{file}\" alt=\"合成图\"></a></td>"
                "<td><form method=\"post\" action=\"/api/records/delete\" onsubmit=\"return confirm('删除这条记录？')\">"
                "<input type=\"hidden\" name=\"id\" value=\"{id}\">"
                "<button type=\"submit\">删除</button></form></td></tr>".format(
                    time=escape(rec.get("time", "")),
                    ip=escape(rec.get("ip", "")),
                    file=file_name,
                    id=escape(rec.get("id", "")),
                )
            )
        if not rows:
            rows.append("<tr><td colspan=\"4\">还没有记录。</td></tr>")
        html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>四格拍贴记录</title>
<style>
body{{margin:24px;background:#f7f4ef;color:#252723;font-family:sans-serif}}
table{{border-collapse:collapse;width:100%;background:#fff}}
td,th{{border-bottom:1px solid #e4e0d8;padding:10px;text-align:left;vertical-align:top}}
img{{width:180px;height:auto;background:#f3f1ec}}
button{{border:0;border-radius:8px;padding:8px 12px;background:#344b3e;color:#fff;cursor:pointer}}
a{{color:#344b3e}}
</style>
</head>
<body>
<h1>保存记录</h1>
<p>共 {n} 条。这里只留下载时的合成图、生成时间和访问 IP，原图不单独保存。这些文件只在这台电脑，不会提交到 GitHub。</p>
<table>
<tr><th>生成时间</th><th>IP</th><th>合成图</th><th></th></tr>
{rows}
</table>
</body>
</html>""".format(n=len(records), rows="".join(rows))
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("http://127.0.0.1:8765/  记录后台 http://127.0.0.1:8765/admin")
    server.serve_forever()
