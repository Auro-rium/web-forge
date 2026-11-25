"""Start a real Chrome with remote debugging so WebForge can drive it (python -m webforge chrome).

A dedicated profile is used on purpose: recent Chrome refuses remote debugging on the default profile,
and it keeps the agent away from the user's everyday logins. Log in to anything the agent should use
once in this window; the session persists in the profile.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 9222
DEFAULT_PROFILE = Path.home() / ".webforge" / "chrome-profile"

_CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "win32": [r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
              r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
              r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
              r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"],
}


def find_chrome(explicit: Optional[str] = None) -> Optional[str]:
    """WEBFORGE_CHROME or an explicit path wins; otherwise the first Chrome/Chromium/Edge found for this OS."""
    for cand in [explicit, os.getenv("WEBFORGE_CHROME")]:
        if cand and Path(cand).exists():
            return cand
    plat = "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux"
    for c in _CANDIDATES[plat]:
        path = os.path.expandvars(c)
        found = shutil.which(path) if plat == "linux" else (path if Path(path).exists() else None)
        if found:
            return found
    return None


def chrome_command(binary: str, port: int, profile: Path, headless: bool = False, url: Optional[str] = None,
                   no_sandbox: Optional[bool] = None) -> list[str]:
    if no_sandbox is None:
        no_sandbox = hasattr(os, "geteuid") and os.geteuid() == 0   # Chrome refuses to run as root otherwise
    cmd = [binary, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
           "--no-default-browser-check", "--disable-features=Translate"]
    if headless:
        cmd.append("--headless=new")
    if no_sandbox:
        cmd.append("--no-sandbox")
    cmd.append(url or "about:blank")
    return cmd


def launch_chrome(port: int = DEFAULT_PORT, profile: Optional[Path] = None, headless: bool = False,
                  url: Optional[str] = None, binary: Optional[str] = None,
                  wait_s: float = 20.0) -> tuple[subprocess.Popen, list[str]]:
    """Start Chrome and wait until its debugging endpoint answers. Returns (process, command)."""
    exe = find_chrome(binary)
    if not exe:
        raise RuntimeError("No Chrome/Chromium/Edge found. Install Chrome or set WEBFORGE_CHROME=/path/to/chrome.")
    profile = Path(profile or DEFAULT_PROFILE)
    profile.mkdir(parents=True, exist_ok=True)
    cmd = chrome_command(exe, port, profile, headless=headless, url=url)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome exited immediately (code {proc.returncode}). Command was: {' '.join(cmd)}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return proc, cmd
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"Chrome did not open its debugging port {port} in {wait_s:.0f}s. Command was: {' '.join(cmd)}")


def stop_chrome(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
