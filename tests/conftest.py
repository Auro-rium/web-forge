import functools
import http.server
import threading
from pathlib import Path

import pytest

from webforge.browser.session import Browser
from webforge.config import Config
from webforge.safety import Secrets

SITE = Path(__file__).resolve().parent.parent / "fixtures" / "site"


@pytest.fixture(scope="session")
def base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def br():
    b = Browser(Config().chromium_path, secrets=Secrets({"site_pw": "hunter2-secret"}, env=False))
    await b.start()
    yield b
    await b.close()


@pytest.fixture
def cfg(tmp_path):
    c = Config(data_dir=tmp_path, runs_dir=tmp_path / "runs", ledger_file=tmp_path / "spend.json")
    c.screenshots = False
    return c
