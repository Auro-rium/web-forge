"""Driving a real, already-running Chrome over CDP: the user's tabs stay untouched and the agent's tabs are visibly marked."""
import json
import socket

import pytest
from playwright.async_api import async_playwright

from webforge.browser.session import Browser, open_browser
from webforge.chrome import launch_chrome, stop_chrome
from webforge.config import Config
from webforge.safety import Secrets


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def chrome(tmp_path, base_url):
    """A real Chrome with remote debugging and ONE pre-existing user tab."""
    port = free_port()
    proc, cmd = launch_chrome(port=port, profile=tmp_path / "profile", headless=True, url=base_url + "/index.html",
                              binary=Config().chromium_path or None)
    try:
        yield f"http://127.0.0.1:{port}", proc
    finally:
        stop_chrome(proc)


async def tabs(cdp):
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp(cdp)
        try:
            return [p.url for c in b.contexts for p in c.pages]
        finally:
            await b.close()


async def test_agent_tabs_are_marked_and_user_tabs_untouched(chrome, base_url):
    cdp, proc = chrome
    before = await tabs(cdp)
    assert any(u.endswith("/index.html") for u in before)

    b = Browser(secrets=Secrets({"x": "y"}, env=False))
    await b.connect(cdp)
    t = await b.new_tab()
    t.identify("w2")
    assert (await t.act("goto", url=base_url + "/forms/register.html")).ok
    await t.observe()
    # badge + title prefix make the controlled tab recognisable in the real window
    badge = await t.page.evaluate("document.getElementById('wf-badge') && document.getElementById('wf-badge').innerText")
    assert badge and "WebForge" in badge and "w2" in badge
    assert (await t.page.title()).startswith("[w2] ")
    # observation does not leak the prefix to the model
    assert not (await t.observe()).title.startswith("[w2]")
    # while the agent works, the acted-on field is outlined, and it is cleaned up afterwards
    assert (await t.act("fill_form", fields={"Full name": "Ada Lovelace"})).ok
    assert await t.page.evaluate("document.querySelectorAll('[data-wf-hl]').length") == 0

    # waiting for the user: badge turns amber and the tab is brought to the front
    await t.attention(True)
    assert "waiting for you" in await t.page.evaluate("document.getElementById('wf-badge').innerText")
    await t.attention(False)

    await b.close()
    after = await tabs(cdp)
    assert after == before, (before, after)          # only the user's original tab remains, untouched
    assert proc.poll() is None                        # Chrome itself was not quit


async def test_default_tab_is_new_not_the_users(chrome, base_url):
    cdp, _ = chrome
    b = Browser(secrets=Secrets(env=False))
    await b.connect(cdp)
    try:
        assert (await b.act("goto", url=base_url + "/data/specs.html")).ok
        assert "specs" in b.page.url
        urls = await tabs(cdp)
        assert any(u.endswith("/index.html") for u in urls)       # the user's tab still shows what it showed
    finally:
        await b.close()


async def test_open_browser_chooses_real_chrome_and_refuses_auto_approval(chrome, base_url, monkeypatch):
    cdp, _ = chrome
    cfg = Config()
    cfg.cdp_url = cdp
    cfg.approval = "auto"
    monkeypatch.delenv("WEBFORGE_ALLOW_AUTO_REAL_BROWSER", raising=False)
    with pytest.raises(RuntimeError, match="auto"):
        await open_browser(cfg, Secrets(env=False))
    cfg.approval = "ask"
    b = await open_browser(cfg, Secrets(env=False))
    try:
        assert b.external and (await b.act("goto", url=base_url + "/index.html")).ok
    finally:
        await b.close()


def test_chrome_launcher_builds_a_safe_command(tmp_path):
    from webforge.chrome import chrome_command
    cmd = chrome_command("/usr/bin/chromium", 9333, tmp_path / "p", headless=False, url=None, no_sandbox=False)
    assert "--remote-debugging-port=9333" in cmd and f"--user-data-dir={tmp_path / 'p'}" in cmd
    assert "--no-first-run" in cmd and not any(c.startswith("--headless") for c in cmd)
    json.dumps(cmd)
