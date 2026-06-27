#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Облачный сервер загрузчика (Render / Docker).
Раздаёт PWA и качает через yt-dlp, отдавая готовый файл прямо в телефон.
Слушает 0.0.0.0:$PORT. ffmpeg ставится в Docker. Для YouTube положите cookies.txt рядом.
"""
import os
import json
import re
import glob
import time
import shutil
import tempfile
from urllib.parse import urlparse, parse_qs, quote, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yt_dlp

DownloadCancelled = getattr(yt_dlp.utils, "DownloadCancelled", None)
if DownloadCancelled is None:
    class DownloadCancelled(Exception):
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
RES = BASE
DLDIR = os.path.join(tempfile.gettempdir(), "vdl")
os.makedirs(DLDIR, exist_ok=True)
INDEX = os.path.join(RES, "index.html")
FFMPEG = shutil.which("ffmpeg")
PORT = int(os.environ.get("PORT", "8000"))

STATIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}

RECENT = {}


def find_cookiefile():
    direct = os.path.join(BASE, "cookies.txt")
    if os.path.isfile(direct):
        return direct
    for pat in ("*cookies*.txt", "*cookie*.txt"):
        m = sorted(glob.glob(os.path.join(BASE, pat)))
        if m:
            return m[0]
    return None


def cleanup_old(max_age=1800):
    now = time.time()
    for p in glob.glob(os.path.join(DLDIR, "*")):
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _emit(self, obj):
        try:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise DownloadCancelled("client disconnected")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/status":
            return self._send_json({"ffmpeg": bool(FFMPEG), "cookiefile": bool(find_cookiefile())})
        if path in STATIC_FILES:
            return self._serve_static(*STATIC_FILES[path])
        if path == "/.well-known/assetlinks.json":
            return self._serve_assetlinks()
        if path == "/api/download":
            return self._download(parse_qs(parsed.query))
        if path.startswith("/files/"):
            return self._serve_file(unquote(path[len("/files/"):]))
        self.send_error(404)

    def _serve_index(self):
        try:
            with open(INDEX, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            data = b"index.html not found"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, name, ctype):
        fp = os.path.join(RES, os.path.basename(name))
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_assetlinks(self):
        fp = os.path.join(BASE, "assetlinks.json")
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, name):
        name = os.path.basename(name)
        fp = RECENT.get(name) or os.path.join(DLDIR, name)
        if not os.path.isfile(fp):
            return self.send_error(404)
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(fp, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _download(self, q):
        url = (q.get("url") or [""])[0].strip()
        mode = (q.get("mode") or ["video"])[0]
        self._sse_start()
        cleanup_old()

        if not url:
            return self._emit({"status": "error", "message": "Пустая ссылка"})
        if not FFMPEG:
            return self._emit({"status": "error", "message": "ffmpeg недоступен на сервере"})

        state = {"id": None, "title": None, "path": None}

        def hook(d):
            st = d.get("status")
            if st == "downloading":
                info = d.get("info_dict") or {}
                state["id"] = info.get("id") or state["id"]
                state["title"] = info.get("title") or state["title"]
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                pct = round(downloaded / total * 100, 1) if total else None
                self._emit({"status": "downloading", "title": state["title"], "percent": pct,
                            "downloaded": downloaded, "total": total,
                            "speed": d.get("speed"), "eta": d.get("eta")})
            elif st == "finished":
                self._emit({"status": "processing", "title": state["title"]})

        def pphook(d):
            if d.get("status") == "finished":
                info = d.get("info_dict") or {}
                fp = info.get("filepath") or info.get("_filename")
                if fp:
                    state["path"] = fp
                self._emit({"status": "processing", "title": state["title"]})

        opts = {
            "outtmpl": os.path.join(DLDIR, "%(title).120s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [hook],
            "postprocessor_hooks": [pphook],
            "quiet": True,
            "no_warnings": True,
            "ffmpeg_location": FFMPEG,
            "restrictfilenames": True,
        }
        if mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
            opts["merge_output_format"] = "mp4"

        cf = find_cookiefile()
        if cf:
            opts["cookiefile"] = cf

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadCancelled:
            return
        except Exception as e:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
            return self._emit({"status": "error", "message": msg})

        final = state["path"]
        if not (final and os.path.isfile(final)) and state["id"]:
            m = [p for p in glob.glob(os.path.join(DLDIR, "*")) if state["id"] in os.path.basename(p)]
            if m:
                final = max(m, key=os.path.getmtime)
        if not (final and os.path.isfile(final)):
            fs = [p for p in glob.glob(os.path.join(DLDIR, "*")) if os.path.isfile(p)]
            final = max(fs, key=os.path.getmtime) if fs else None
        if not final:
            return self._emit({"status": "error", "message": "Файл не найден после загрузки"})

        name = os.path.basename(final)
        RECENT[name] = final
        self._emit({"status": "done", "title": state["title"] or name,
                    "file": name, "url": "/files/" + quote(name)})


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Server on 0.0.0.0:%d  ffmpeg=%s  cookies=%s" % (PORT, bool(FFMPEG), bool(find_cookiefile())))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
