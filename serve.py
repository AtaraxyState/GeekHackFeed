#!/usr/bin/env python3
"""Serve the geekhack feed to the Android app, refreshing on a timer.

Re-scrapes the board every few minutes and exposes the result as JSON plus
cached thumbnails, so the phone only ever talks to this machine.

    python serve.py                        # board 70, refresh every 5 min
    python serve.py --interval 15          # gentler
    python serve.py --port 8765 --pages 6  # more of the board
    python serve.py --tunnel               # also reachable off the home Wi-Fi

Endpoints:
    GET /                 the same scrollable feed, for a phone browser
    GET /api/feed.json    every project as JSON
    GET /api/status       when it last refreshed, and whether it is busy now
    GET /images/<id>.jpg  cached cover thumbnails
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scrape
import tunnel as tunnel_mod
import vendors

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(HERE, "images")

# Served until the first scrape lands, so a phone that connects during startup
# gets something sensible instead of a refused connection.
WARMING_PAGE = b"""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>geekhack feed</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0e0f13;color:#e8eaf2;font:15px/1.6 system-ui,sans-serif;text-align:center}
.dot{width:9px;height:9px;border-radius:50%;background:#7c8cff;display:inline-block;
animation:p 1.2s infinite}@keyframes p{50%{opacity:.25}}
p{color:#949bb3}</style>
<div><h2><span class="dot"></span> Scraping geekhack</h2>
<p>First pass is running. This page reloads itself.</p></div>
"""


class Feed:
    """Holds the current payload and refreshes it in the background."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.payload = {"board": args.board, "generated": None, "projects": []}
        self.html = WARMING_PAGE
        self.last_refresh = None
        self.last_change = None
        self.last_error = None
        self.refreshing = False
        self.refresh_count = 0
        self.skipped_count = 0
        self.tunnel = None  # set in main() when --tunnel is used
        self._fingerprint = None
        self._geekhack = []
        self._fetcher = scrape.Fetcher(delay=args.delay)

    # -- change detection ---------------------------------------------------

    def board_fingerprint(self):
        """Hash the board's RSS feed: 5 KB instead of a 97 KB listing page.

        At a 5 minute cadence this is the difference between a trivial poll and
        hammering a volunteer-run forum for no reason.
        """
        url = f"{scrape.BASE}?action=.xml;type=rss2;board={self.args.board}"
        xml = self._fetcher.get(url)
        if not xml:
            return None
        marks = re.findall(r"<(?:link|pubDate)>([^<]+)</(?:link|pubDate)>", xml)
        if not marks:
            return None
        return hashlib.sha256("".join(marks).encode("utf-8", "replace")).hexdigest()

    # -- refresh ------------------------------------------------------------

    def refresh(self, force=False):
        with self.lock:
            if self.refreshing:
                return "busy"
            self.refreshing = True
        try:
            board_changed = True
            if not force:
                fingerprint = self.board_fingerprint()
                if fingerprint and fingerprint == self._fingerprint:
                    board_changed = False
                else:
                    self._fingerprint = fingerprint

            # Storefront stock moves without the forum changing, so vendors are
            # polled every cycle regardless -- it is one request each.
            if not board_changed and not self.args.vendors:
                self.last_refresh = now_iso()
                self.skipped_count += 1
                return "unchanged"

            if board_changed or not self._geekhack:
                self._geekhack = scrape.scrape(self.args)
            else:
                print("[feed] board unchanged, refreshing vendors only")

            projects = list(self._geekhack)
            if self.args.vendors:
                projects += vendors.collect(
                    only=self.args.vendors, delay=self.args.delay
                )

            payload = {
                "board": self.args.board,
                "generated": now_iso(),
                "count": len(projects),
                "projects": projects,
            }
            html = render_html(projects, self.args.board)

            with self.lock:
                self.payload = payload
                self.html = html
            self.last_refresh = self.last_change = payload["generated"]
            self.refresh_count += 1
            self.last_error = None
            print(f"[feed] refreshed: {len(projects)} projects")
            return "refreshed"
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            self.last_error = f"{type(exc).__name__}: {exc}"
            scrape.warn(f"refresh failed: {self.last_error}")
            return "error"
        finally:
            self.refreshing = False

    def loop(self):
        while True:
            time.sleep(self.args.interval * 60)
            self.refresh()

    def status(self):
        with self.lock:
            count = len(self.payload["projects"])
        status = {
            "board": self.args.board,
            "projects": count,
            "last_refresh": self.last_refresh,
            "last_change": self.last_change,
            "refreshing": self.refreshing,
            "refreshes": self.refresh_count,
            "polls_skipped": self.skipped_count,
            "interval_minutes": self.args.interval,
            "last_error": self.last_error,
        }
        # Quick-tunnel URLs change on every restart, so publish the current one:
        # the phone can read it here instead of the user hunting through logs.
        if self.tunnel is not None:
            status["public_url"] = self.tunnel.url
        return status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def render_html(projects, board):
    out = os.path.join(HERE, f"feed-board{board}.html")
    scrape.render(projects, board, out)
    with open(out, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    feed: Feed = None  # set in main()
    server_version = "geekhack-feed/1.0"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    def _send(self, status, body, ctype, cache="no-cache"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/":
            with self.feed.lock:
                html = self.feed.html
            self._send(200, html, "text/html; charset=utf-8")

        elif path == "/api/feed.json":
            with self.feed.lock:
                payload = self.feed.payload
            self._json(payload)

        elif path == "/api/status":
            self._json(self.feed.status())

        elif path == "/api/refresh":
            result = self.feed.refresh(force=True)
            self._json({"result": result, **self.feed.status()})

        elif path.startswith("/images/"):
            self._serve_image(path)

        else:
            self._json({"error": "not found", "path": path}, status=404)

    def _serve_image(self, path):
        name = os.path.basename(path)
        # Only ever serve the thumbnails we generated ourselves.
        if not re.fullmatch(r"\d+\.jpg", name):
            self._json({"error": "bad image name"}, status=400)
            return
        full = os.path.join(IMAGE_DIR, name)
        if not os.path.isfile(full):
            self._json({"error": "no such image"}, status=404)
            return
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, "image/jpeg", cache="public, max-age=86400")


def local_addresses(port):
    """Best guess at the URL to type into the phone."""
    import socket

    addresses = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))  # no packets sent, just picks the route
        addresses.append(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if ip not in addresses and not ip.startswith("127."):
            addresses.append(ip)
    return [f"http://{ip}:{port}" for ip in addresses]


def main():
    parser = argparse.ArgumentParser(description="Serve the geekhack feed.")
    parser.add_argument("--board", type=int, default=70)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="minutes between refresh polls (default: 5)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between requests to geekhack")
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 so the phone can reach it")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--vendors", nargs="?", const="all", default=None,
        help="also pull vendor storefronts: bare flag for all, or a "
             "comma-separated list of " + ",".join(vendors.VENDORS),
    )
    parser.add_argument(
        "--tunnel", action="store_true",
        help="also publish a Cloudflare quick tunnel (public HTTPS URL, no "
             "account needed) so the phone works off the home Wi-Fi",
    )
    args = parser.parse_args()

    if args.vendors == "all":
        args.vendors = list(vendors.VENDORS)
    elif args.vendors:
        args.vendors = [v.strip().lower() for v in args.vendors.split(",")]

    # scrape.scrape() expects the flags the CLI would have set.
    args.refresh = False
    args.hotlink = False
    args.skip_sticky = True

    if args.delay < 0.5:
        args.delay = 0.5

    feed = Feed(args)
    Handler.feed = feed

    # Listen first, scrape second: the phone should never get a refused
    # connection just because it opened the app while the server was warming up.
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"serving board {args.board}, refreshing every {args.interval:g} min")
    for url in local_addresses(args.port):
        print(f"  {url}")
    print(f"  http://localhost:{args.port}")

    # The tunnel comes up alongside the LAN addresses, not instead of them: on
    # the home Wi-Fi the direct address stays faster and keeps working if
    # Cloudflare is having a bad day.
    if args.tunnel:
        try:
            feed.tunnel = tunnel_mod.start_background(args.port)
            if feed.tunnel.url:
                print(f"  {feed.tunnel.url}   <- public, works anywhere")
            else:
                print("  (tunnel starting; the URL will appear in "
                      "tunnel-url.txt and /api/status)")
        except tunnel_mod.TunnelError as exc:
            # A failed tunnel must not cost you the LAN server.
            scrape.warn(f"tunnel unavailable: {exc}")

    print("\npoint the Android app at one of the addresses above (Ctrl+C to stop)\n")

    try:
        print("first scrape...")
        feed.refresh(force=True)
        feed.loop()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.shutdown()
        if feed.tunnel is not None:
            feed.tunnel.stop()


if __name__ == "__main__":
    main()
