"""Browser layer against the offline fixture site: observation, actions, forms, secrets, readers, tabs."""
from webforge.browser.session import Browser
from webforge.config import Config


def el(obs, name):
    return next(e for e in obs.elements if e.name == name)


async def test_observe_and_click_records_locator(br, base_url):
    assert (await br.act("goto", url=base_url + "/index.html")).ok
    obs = await br.observe()
    reg = el(obs, "Register")
    assert reg.role == "link" and reg.locator == {"role": "link", "name": "Register"}
    assert f"[{reg.idx}] link 'Register'" in obs.render()
    res = await br.act("click", idx=reg.idx)
    assert res.ok and br.page.url.endswith("/forms/register.html")
    step = br.trace[-1]
    assert step.action == "click" and step.locator == {"role": "link", "name": "Register"} and step.ok


async def test_stale_idx_is_reported(br, base_url):
    await br.act("goto", url=base_url + "/index.html")
    await br.observe()
    res = await br.act("click", idx=999)
    assert not res.ok and "not found" in res.message


async def test_form_fields_are_labelled_with_details(br, base_url):
    await br.act("goto", url=base_url + "/forms/register.html")
    obs = await br.observe()
    assert el(obs, "Full name").info == "required"
    assert "email" in el(obs, "Email").info and "required" in el(obs, "Email").info
    assert "options=" in el(obs, "Ticket type").info and "VIP" in el(obs, "Ticket type").info
    assert el(obs, "Morning").role == "radio" and el(obs, "I accept the terms").role == "checkbox"
    assert "submit" in el(obs, "Submit registration").info


async def test_fill_form_and_submit(br, base_url):
    await br.act("goto", url=base_url + "/forms/register.html")
    await br.observe()
    r = await br.act("fill_form", fields={"Full name": "Ada Lovelace", "email": "ada@example.org",
                                          "Ticket type": "Student", "Afternoon": True, "terms": "yes"})
    assert r.ok, r.message
    obs = await br.observe()
    assert el(obs, "Full name").value == "Ada Lovelace" and el(obs, "Afternoon").value == "checked"
    assert (await br.act("click", idx=el(obs, "Submit registration").idx)).ok
    assert "Registration confirmed: student ticket, session pm" in await br.page_text()


async def test_validation_errors_appear_in_observation(br, base_url):
    await br.act("goto", url=base_url + "/forms/register.html")
    obs = await br.observe()
    await br.act("fill_form", fields={"Full name": "Ada", "Email": "not-an-email"})
    await br.act("click", idx=el(obs, "Submit registration").idx)
    obs = await br.observe()
    assert any("valid email" in e for e in obs.errors) and "FORM ERRORS" in obs.render()


async def test_fill_form_reports_unknown_fields(br, base_url):
    await br.act("goto", url=base_url + "/forms/register.html")
    await br.observe()
    r = await br.act("fill_form", fields={"Full name": "Ada", "Shoe size": "42"})
    assert not r.ok and "Shoe size: NO MATCHING FIELD" in r.message


async def test_secret_is_typed_but_never_shown(br, base_url):
    await br.act("goto", url=base_url + "/forms/login.html")
    obs = await br.observe()
    await br.act("type", idx=el(obs, "Username").idx, text="ada")
    r = await br.act("type", idx=el(obs, "Password").idx, text="{{secret:site_pw}}")
    assert r.ok and "hunter2" not in r.message
    obs = await br.observe()
    assert el(obs, "Password").value == "•••" and "hunter2" not in obs.render()
    await br.act("click", idx=el(obs, "Log in").idx)
    assert "Welcome back, ada." in await br.page_text()
    assert all("hunter2" not in (s.result + str(s.args)) for s in br.trace)


async def test_unknown_secret_is_refused(br, base_url):
    await br.act("goto", url=base_url + "/forms/login.html")
    obs = await br.observe()
    r = await br.act("type", idx=el(obs, "Password").idx, text="{{secret:nope}}")
    assert not r.ok and "unknown secret" in r.message


async def test_generic_readers(br, base_url):
    await br.act("goto", url=base_url + "/data/specs.html")
    r = await br.act("read")
    assert r.ok and "# Planet data" in r.message and "footer junk" not in r.message
    t = await br.act("tables")
    assert {"Planet": "Mars", "Moons": "2", "Day length": "24.6 hours"} in t.data[0]["rows"]
    links = await br.act("links", filter="wiki")
    assert links.ok and [x["text"] for x in links.data] == ["Mini Wiki"]
    f = await br.act("find", text="backwards")
    assert f.ok and "Venus rotates backwards" in f.data["context"]
    s = await br.act("structured")
    assert s.ok and s.data["jsonld"][0]["offers"]["price"] == "23.50"
    page = await br.read(offset=0, window=40)
    assert "read(offset=40)" in page.message


async def test_extract_rows_and_bad_selectors(br, base_url):
    await br.act("goto", url=base_url + "/data/specs.html")
    r = await br.act("extract", css="table tr", fields={"cells": "td", "bad": "p[[["})
    assert r.ok and "invalid selector for field(s) bad" in r.message
    assert not (await br.act("extract", css="tr[[[")).ok


async def test_tabs_are_independent(br, base_url):
    await br.act("goto", url=base_url + "/index.html")
    t1, t2 = await br.new_tab(), await br.new_tab()
    assert (await t1.act("goto", url=base_url + "/wiki/index.html")).ok
    assert (await t2.act("goto", url=base_url + "/data/specs.html")).ok
    assert br.page.url.endswith("/index.html") and len(t1.trace) == 1 and len(t2.trace) == 1
    assert len(br.trace) == 1
    await t1.close()
    await t2.close()


async def test_domain_allowlist(base_url):
    b = Browser(Config().chromium_path, allowed_domains=["example.com"])
    await b.start()
    try:
        assert (await b.act("goto", url=base_url + "/index.html")).ok   # localhost always allowed
        r = await b.act("goto", url="https://not-allowed.test/")
        assert not r.ok and "not allowed" in r.message
    finally:
        await b.close()


async def test_screenshot_is_small_jpeg(br, base_url):
    await br.act("goto", url=base_url + "/forms/register.html")
    img = await br.screenshot()
    assert img and img[:2] == b"\xff\xd8" and len(img) < 120_000


async def _register(br, base_url, name, email, ticket):
    await br.act("goto", url=base_url + "/forms/register.html")
    obs = await br.observe()
    await br.act("fill_form", fields={"Full name": name, "Email": email, "Ticket type": ticket, "terms": True})
    await br.act("click", idx=el(obs, "Submit registration").idx)
    return await br.page_text()


async def test_confirmation_codes_are_unique_per_student(br, base_url):
    import re
    codes = []
    for name, email in [("Ada Lovelace", "ada@school.org"), ("Alan Turing", "alan@school.org"),
                        ("Maya Rao", "maya@school.org")]:
        text = await _register(br, base_url, name, email, "Student")
        m = re.search(r"Confirmation code (WF-\d{4})", text)
        assert m and f"Thanks, {name}" in text, text
        codes.append(m.group(1))
    assert len(set(codes)) == 3
    again = await _register(br, base_url, "Ada Lovelace", "ada@school.org", "Student")
    assert codes[0] in again          # deterministic: same student, same code


async def test_domain_without_dot_is_rejected_with_a_visible_error(br, base_url):
    text = await _register(br, base_url, "Maya Rao", "maya@school", "Student")
    assert "Thanks" not in text
    obs = await br.observe()
    assert any("valid email" in e for e in obs.errors)


async def test_email_error_appears_as_soon_as_it_is_typed(br, base_url):
    """Like real sites: a bad email is flagged on entry, so the agent can ask BEFORE it requests approval to submit."""
    await br.act("goto", url=base_url + "/forms/register.html")
    await br.observe()
    await br.act("fill_form", fields={"Full name": "Maya Rao", "Email": "maya@school"})
    obs = await br.observe()
    assert any("valid email" in e for e in obs.errors), obs.errors
    await br.act("fill_form", fields={"Email": "maya@school.org"})
    assert not any("valid email" in e for e in (await br.observe()).errors)
