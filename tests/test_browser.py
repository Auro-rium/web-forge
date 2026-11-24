import functools
import http.server
import threading
from pathlib import Path

import pytest

from webforge.browser.session import Browser
from webforge.config import Config

SITE = Path(__file__).resolve().parent.parent / "fixtures" / "site"


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def br(base_url):
    b = Browser(Config().chromium_path)
    await b.start()
    r = await b.act("goto", url=base_url + "/index.html")
    assert r.ok, r.message
    b.trace.clear()
    yield b
    await b.close()


def find(obs, role, name):
    return next(e for e in obs.elements if e.role == role and e.name == name)


async def test_observe_index(br):
    obs = await br.observe()
    assert "Fixture Books" in obs.title
    poetry = find(obs, "link", "Poetry")
    assert poetry.locator == {"role": "link", "name": "Poetry"}
    assert poetry.href.endswith("/catalogue/category/poetry/index.html")
    box = next(e for e in obs.elements if e.role == "textbox")
    assert box.locator == {"role": "textbox", "name": "Search books"} or box.locator == {"placeholder": "Search books"}
    assert [e.idx for e in obs.elements] == list(range(len(obs.elements)))
    assert "[%d] link 'Poetry'" % poetry.idx in obs.render()


async def test_click_traces_locator(br):
    obs = await br.observe()
    poetry = find(obs, "link", "Poetry")
    res = await br.act("click", idx=poetry.idx)
    assert res.ok and br.page.url.endswith("/catalogue/category/poetry/index.html")
    step = br.trace[-1]
    assert step.action == "click" and step.locator == {"role": "link", "name": "Poetry"}
    assert step.url.endswith("/index.html") and step.ok


async def test_stale_idx(br):
    await br.observe()
    res = await br.act("click", idx=999)
    assert not res.ok and "observe again" in res.message


async def test_type_into_search(br):
    obs = await br.observe()
    box = next(e for e in obs.elements if e.role == "textbox")
    res = await br.act("type", idx=box.idx, text="moon", submit=True)
    assert res.ok
    assert "q=moon" in br.page.url
    assert br.trace[-1].locator


async def test_extract_rows(br, base_url):
    await br.act("goto", url=base_url + "/catalogue/category/poetry/index.html")
    res = await br.act("extract", css="article.product_pod",
                       fields={"title": "h3 a@title", "price": ".price_color"})
    assert res.ok and len(res.data) == 4
    assert res.data[0]["title"] and res.data[0]["price"].startswith("£")
    texts = await br.act("extract", css="h1")
    assert texts.data == ["Poetry"]


async def test_act_locator_replay(br):
    n = len(br.trace)
    res = await br.act_locator("click", {"role": "link", "name": "Travel"})
    assert res.ok and br.page.url.endswith("/travel/index.html")
    assert len(br.trace) == n
    bad = await br.act_locator("click", {"role": "link", "name": "Nonexistent"})
    assert not bad.ok and "not found" in bad.message
    ex = await br.act_locator("extract", None, extract={"type": "css", "selector": "h1"})
    assert ex.ok and ex.data == ["Travel"]


async def test_domain_blocked(base_url):
    b = Browser(Config().chromium_path, allowed_domains=["books.toscrape.com"])
    await b.start()
    try:
        r = await b.act("goto", url="https://evil.example.com/")
        assert not r.ok and r.message == "domain not allowed"
        assert not (await b.act_locator("goto", None, "https://evil.example.com/")).ok
        assert (await b.act("goto", url=base_url + "/index.html")).ok  # localhost always allowed
    finally:
        await b.close()
