import asyncio
from pathlib import Path

import pytest

from webforge import __main__ as cli
from webforge import chrome


def test_secret_parsing(monkeypatch):
    monkeypatch.setenv("MY_PW", "s3cret-value")
    s = cli._secrets(["site=env:MY_PW"])
    assert s.names() == ["site"]
    with pytest.raises(SystemExit):
        cli._secrets(["site=literal"])


def test_approval_validation():
    p = cli._build_parser()
    assert p.parse_args(["run", "t", "--approval", "deny"]).approval == "deny"
    with pytest.raises(SystemExit):
        p.parse_args(["run", "t", "--approval", "maybe"])


def test_cdp_option_sets_env(monkeypatch):
    monkeypatch.setenv("WEBFORGE_CDP_URL", "")   # registers cleanup BEFORE main() writes the variable (teardown removes it)
    seen = {}

    async def fake(args, task, run_dir, resume=None):
        import os
        seen["cdp"] = os.environ.get("WEBFORGE_CDP_URL")
        return 0
    monkeypatch.setattr(cli, "_cmd_run", fake)
    assert cli.main(["run", "t", "--cdp", "http://127.0.0.1:9333"]) == 0
    assert seen["cdp"] == "http://127.0.0.1:9333"


def test_chrome_command_output(monkeypatch, capsys, tmp_path):
    calls = {}

    class P:
        returncode = None

        def poll(self):
            raise KeyboardInterrupt

    def fake_launch(port, profile, headless, url=None, binary=None, **kw):
        calls.update(port=port, profile=profile, headless=headless)
        return P(), chrome.chrome_command("/opt/chrome", port, Path(profile), headless=headless, no_sandbox=False)
    stopped = []
    monkeypatch.setattr(chrome, "launch_chrome", fake_launch)
    monkeypatch.setattr(chrome, "stop_chrome", lambda p: stopped.append(p))
    assert cli.main(["chrome", "--port", "9333", "--profile", str(tmp_path / "prof")]) == 0
    out = capsys.readouterr().out
    assert calls == {"port": 9333, "profile": tmp_path / "prof", "headless": False}
    assert "/opt/chrome" in out and "--remote-debugging-port=9333" in out
    assert "export WEBFORGE_CDP_URL=http://127.0.0.1:9333" in out
    assert "set WEBFORGE_CDP_URL=http://127.0.0.1:9333" in out
    assert stopped == []   # Chrome stays open by default
    assert cli.main(["chrome", "--stop-on-exit"]) == 0
    assert len(stopped) == 1


def test_chrome_not_found_exits_2(monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("No Chrome/Chromium/Edge found.")
    monkeypatch.setattr(chrome, "launch_chrome", boom)
    assert cli.main(["chrome"]) == 2
    assert "No Chrome" in capsys.readouterr().err


def test_run_refuses_auto_with_cdp(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WEBFORGE_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.delenv("WEBFORGE_ALLOW_AUTO_REAL_BROWSER", raising=False)
    args = cli._build_parser().parse_args(["run", "t", "--mock", "--approval", "auto", "--plain"])
    rc = asyncio.run(cli._cmd_run(args, "t", tmp_path / "r"))
    assert rc == 2
    assert "auto" in capsys.readouterr().err


def test_plain_ui_metrics_rate_limited(capsys):
    from rich.console import Console
    from webforge.ui import PlainUI
    ui = PlainUI("t", Console(width=200))
    m = {"elapsed": 3, "steps": 2, "llm_calls": 4, "tokens": 1000, "run_cost": 0.01, "run_budget": 0.25}
    ui.on_event("metrics", m)
    ui.on_event("metrics", m)
    out = capsys.readouterr().out
    assert out.count("metrics") == 1 and "2 steps" in out


def test_ui_ask_accepts_agent(monkeypatch):
    from rich.console import Console
    from webforge import ui as uimod
    seen = []

    async def fake_prompt(console, q, options):
        seen.append(q)
        return "yes"
    monkeypatch.setattr(uimod, "_prompt", fake_prompt)
    for U in (uimod.PlainUI, uimod.LiveUI):
        u = U("t", Console())
        assert asyncio.run(u.ask("q?", [], "worker2")) == "yes"
        assert asyncio.run(u.ask("q?", [])) == "yes"
