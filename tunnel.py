#!/usr/bin/env python3
"""Expose the local feed server on a public HTTPS URL via a Cloudflare quick
tunnel, so the phone reaches it off the home Wi-Fi.

No Cloudflare account and no port-forwarding: `cloudflared tunnel --url ...`
registers an outbound connection and Cloudflare hands back a
`https://<random>.trycloudflare.com` address. The binary is downloaded once
into `.tools/` on first use.

The URL is random and it CHANGES on every restart -- that is the price of a
free quick tunnel. serve.py prints it, writes it to tunnel-url.txt and exposes
it at /api/status so the phone can be re-pointed without digging through logs.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(HERE, ".tools")
LOG_PATH = os.path.join(HERE, "tunnel.log")
URL_PATH = os.path.join(HERE, "tunnel-url.txt")

_RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
# (asset name, local filename) per platform.
_ASSETS = {
    ("Windows", "AMD64"): ("cloudflared-windows-amd64.exe", "cloudflared.exe"),
    ("Windows", "ARM64"): ("cloudflared-windows-arm64.exe", "cloudflared.exe"),
    ("Linux", "x86_64"): ("cloudflared-linux-amd64", "cloudflared"),
    ("Linux", "aarch64"): ("cloudflared-linux-arm64", "cloudflared"),
    ("Darwin", "x86_64"): ("cloudflared-darwin-amd64.tgz", "cloudflared"),
    ("Darwin", "arm64"): ("cloudflared-darwin-arm64.tgz", "cloudflared"),
}

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(RuntimeError):
    """cloudflared could not be obtained or did not produce a URL."""


def _asset():
    key = (platform.system(), platform.machine())
    if key not in _ASSETS:
        raise TunnelError(f"no cloudflared build known for {key}; install it "
                          f"yourself and put it in {TOOLS_DIR}")
    return _ASSETS[key]


def ensure_binary() -> str:
    """Path to a usable cloudflared, downloading it on first use. An already
    installed one on PATH wins, so a system package stays authoritative."""
    from shutil import which

    found = which("cloudflared")
    if found:
        return found

    asset, local_name = _asset()
    local = os.path.join(TOOLS_DIR, local_name)
    if os.path.exists(local):
        return local

    if asset.endswith(".tgz"):
        raise TunnelError(
            "cloudflared for macOS ships as a .tgz; install it with "
            "`brew install cloudflared` and re-run.")

    os.makedirs(TOOLS_DIR, exist_ok=True)
    print(f"[tunnel] downloading cloudflared ({asset})...")
    tmp = local + ".part"
    try:
        urllib.request.urlretrieve(_RELEASE + asset, tmp)
        os.replace(tmp, local)
    except Exception as exc:  # noqa: BLE001 - surfaced as TunnelError below
        if os.path.exists(tmp):
            os.remove(tmp)
        raise TunnelError(f"download failed: {exc}") from exc
    if not sys.platform.startswith("win"):
        os.chmod(local, 0o755)
    return local


class Tunnel:
    """A running quick tunnel. `url` is None until Cloudflare assigns one."""

    def __init__(self, port: int):
        self.port = port
        self.url: str | None = None
        self._proc: subprocess.Popen | None = None
        self._log = None

    def start(self, timeout: float = 30.0) -> str | None:
        """Launch cloudflared and wait (up to `timeout`) for the public URL.
        Returns the URL, or None if it never appeared -- the tunnel process is
        left running either way, since it may simply be slow."""
        binary = ensure_binary()
        self._log = open(LOG_PATH, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [binary, "tunnel", "--url", f"http://localhost:{self.port}",
             "--no-autoupdate"],
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise TunnelError(
                    f"cloudflared exited with code {self._proc.returncode}; "
                    f"see {LOG_PATH}")
            url = self._scan_log()
            if url:
                self.url = url
                try:
                    with open(URL_PATH, "w", encoding="utf-8") as fh:
                        fh.write(url + "\n")
                except OSError:
                    pass
                return url
            time.sleep(0.5)
        return None

    def _scan_log(self) -> str | None:
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
                match = URL_RE.search(fh.read())
        except OSError:
            return None
        return match.group(0) if match else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def watch(self, interval: float = 30.0):
        """Restart the tunnel if cloudflared dies (its URL changes when that
        happens, so the new one is printed). Runs until the process exits."""
        while True:
            time.sleep(interval)
            if self.alive():
                continue
            print("[tunnel] cloudflared died; restarting")
            try:
                self.stop()
                url = self.start()
            except TunnelError as exc:
                print(f"[tunnel] restart failed: {exc}")
                continue
            print(f"[tunnel] new public URL: {url or 'pending'} "
                  f"(the old one is dead -- re-point the app)")

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


def start_background(port: int) -> Tunnel:
    """Start a tunnel plus its watchdog thread. Raises TunnelError if
    cloudflared cannot be started at all."""
    tunnel = Tunnel(port)
    tunnel.start()
    threading.Thread(target=tunnel.watch, daemon=True).start()
    return tunnel


if __name__ == "__main__":
    # Standalone: tunnel an already-running server, e.g. `python tunnel.py 8765`
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    t = Tunnel(port)
    print(f"[tunnel] public URL: {t.start() or 'not assigned yet'}")
    print("Ctrl+C to stop.")
    try:
        t.watch(interval=5)
    except KeyboardInterrupt:
        pass
    finally:
        t.stop()
