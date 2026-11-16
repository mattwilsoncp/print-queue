#!/usr/bin/env python3
"""3D Printer Queue — a tiny local web app for lining up 3D prints.

Drop STL files or jot down ideas while the printer is busy, reorder them,
and mark them printing / done.  Standard library only: no installs needed.

    python3 app.py              # http://localhost:8765
    python3 app.py --port 9000  # pick another port

It listens on all interfaces, so a phone on the same Wi-Fi can open
http://<your-mac's-ip>:8765 and add things too.
"""
import argparse
import json
import mimetypes
import os
import re
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
QUEUE_FILE = DATA_DIR / "queue.json"
STATIC_DIR = ROOT / "static"
MAX_UPLOAD = 300 * 1024 * 1024  # 300 MB
ALLOWED_EXT = {".stl", ".3mf", ".obj", ".gcode", ".step", ".stp"}
STATUSES = ("queued", "printing", "done")

_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def load_queue():
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except json.JSONDecodeError:
            QUEUE_FILE.rename(QUEUE_FILE.with_suffix(".corrupt.json"))
    return []


def save_queue(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    os.replace(tmp, QUEUE_FILE)


def find(items, item_id):
    for it in items:
        if it["id"] == item_id:
            return it
    return None


# ---------------------------------------------------------------- STL info
def stl_info(path):
    """Return bounding-box size (mm) and triangle count, or None."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            header = f.read(84)
            if len(header) == 84:
                n = struct.unpack("<I", header[80:84])[0]
                if 84 + n * 50 == size:  # binary STL
                    mins = [float("inf")] * 3
                    maxs = [float("-inf")] * 3
                    rec = struct.Struct("<12fH")
                    for _ in range(n):
                        v = rec.unpack(f.read(50))
                        for k in range(3, 12):
                            a = k % 3
                            if v[k] < mins[a]:
                                mins[a] = v[k]
                            if v[k] > maxs[a]:
                                maxs[a] = v[k]
                    return _dims(mins, maxs, n)
        # ASCII STL
        text = path.read_text(errors="ignore")
        pts = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
        if not pts:
            return None
        mins = [min(float(p[a]) for p in pts) for a in range(3)]
        maxs = [max(float(p[a]) for p in pts) for a in range(3)]
        return _dims(mins, maxs, len(pts) // 3)
    except Exception:
        return None


def _dims(mins, maxs, tris):
    if tris == 0:
        return None
    return {
        "x": round(maxs[0] - mins[0], 1),
        "y": round(maxs[1] - mins[1], 1),
        "z": round(maxs[2] - mins[2], 1),
        "triangles": tris,
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PrintQueue/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, msg):
        self.send_json({"error": msg}, code)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def send_file(self, path, download_name=None):
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download_name:
            safe = download_name.replace('"', "")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 16):
                self.wfile.write(chunk)

    # -- routes
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send_file(STATIC_DIR / "index.html")
        if path == "/api/queue":
            with _lock:
                return self.send_json(load_queue())
        m = re.fullmatch(r"/files/([0-9a-f]{32})", path)
        if m:
            with _lock:
                it = find(load_queue(), m.group(1))
            if not it or not it.get("file"):
                return self.error(404, "no file")
            fp = UPLOAD_DIR / it["file"]["stored"]
            if not fp.exists():
                return self.error(404, "file missing on disk")
            return self.send_file(fp, it["file"]["name"])
        self.error(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/upload":
            return self.upload()
        if path == "/api/idea":
            data = self.read_json()
            title = (data.get("title") or "").strip()
            if not title:
                return self.error(400, "title required")
            return self.add_item({"kind": "idea", "title": title,
                                  "notes": (data.get("notes") or "").strip()})
        if path == "/api/reorder":
            ids = self.read_json().get("ids", [])
            with _lock:
                items = load_queue()
                order = {i: n for n, i in enumerate(ids)}
                items.sort(key=lambda it: order.get(it["id"], len(order)))
                save_queue(items)
            return self.send_json({"ok": True})
        self.error(404, "not found")

    def do_PATCH(self):
        m = re.fullmatch(r"/api/items/([0-9a-f]{32})", urlparse(self.path).path)
        if not m:
            return self.error(404, "not found")
        data = self.read_json()
        with _lock:
            items = load_queue()
            it = find(items, m.group(1))
            if not it:
                return self.error(404, "no such item")
            for key in ("title", "notes", "material", "color"):
                if key in data:
                    it[key] = str(data[key]).strip()
            if "status" in data:
                if data["status"] not in STATUSES:
                    return self.error(400, "bad status")
                if data["status"] == "printing":
                    for other in items:  # one printer, one job
                        if other["status"] == "printing" and other is not it:
                            other["status"] = "queued"
                it["status"] = data["status"]
                it[f"{data['status']}_at"] = time.time()
            save_queue(items)
        self.send_json(it)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/items/([0-9a-f]{32})", urlparse(self.path).path)
        if not m:
            return self.error(404, "not found")
        with _lock:
            items = load_queue()
            it = find(items, m.group(1))
            if not it:
                return self.error(404, "no such item")
            items.remove(it)
            save_queue(items)
            if it.get("file"):  # keep the file, just move it aside
                fp = UPLOAD_DIR / it["file"]["stored"]
                if fp.exists():
                    trash = DATA_DIR / "removed"
                    trash.mkdir(exist_ok=True)
                    os.replace(fp, trash / fp.name)
        self.send_json({"ok": True})

    # -- upload: raw body, filename in X-Filename header (no multipart parsing)
    def upload(self):
        name = os.path.basename(unquote(self.headers.get("X-Filename", ""))).strip()
        ext = Path(name).suffix.lower()
        if not name or ext not in ALLOWED_EXT:
            return self.error(400, f"allowed types: {', '.join(sorted(ALLOWED_EXT))}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            return self.error(400, "empty or too large")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        item_id = uuid.uuid4().hex
        stored = f"{item_id}{ext}"
        dest = UPLOAD_DIR / stored
        remaining = length
        with open(dest, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(1 << 16, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            dest.unlink(missing_ok=True)
            return self.error(400, "upload interrupted")
        info = stl_info(dest) if ext == ".stl" else None
        title = unquote(self.headers.get("X-Title", "")).strip() or Path(name).stem
        notes = unquote(self.headers.get("X-Notes", "")).strip()
        return self.add_item({
            "kind": "file", "title": title, "notes": notes,
            "file": {"name": name, "stored": stored, "bytes": length, "info": info},
        }, item_id)

    def add_item(self, fields, item_id=None):
        item = {"id": item_id or uuid.uuid4().hex, "status": "queued",
                "created_at": time.time(), "material": "", "color": "", "notes": ""}
        item.update(fields)
        with _lock:
            items = load_queue()
            items.append(item)
            save_queue(items)
        self.send_json(item, 201)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0",
                    help="use 127.0.0.1 to keep it on this computer only")
    args = ap.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"3D Printer Queue running:  http://localhost:{args.port}")
    ip = lan_ip()
    if ip and args.host == "0.0.0.0":
        print(f"From your phone:           http://{ip}:{args.port}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
