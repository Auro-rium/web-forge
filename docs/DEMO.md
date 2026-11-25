# Live demo: run of show (about 8 minutes)

Goal: show an agent that takes a plain-language job, works the real web with parallel sub-agents, **asks before anything consequential**, checks its own result and hands back files. Live runs vary; the numbers below come from live runs with Haiku 5.5 via Backboard and are targets, not guarantees.

## Pre-flight checklist (15 minutes before)

- [ ] Pick one path (never show your key on screen):
  - **Local Python:** `.env` with a valid `BACKBOARD_API_KEY`; venv active; `pip install -r requirements.txt`; `python -m playwright install --with-deps chromium`; start with `python -m webforge serve --headed` (the real browser window opens on the projector; the page also shows live thumbnails per sub-agent).
  - **Docker:** `docker compose up --build`, open http://localhost:8000, paste the key in the "Connect Backboard" card. The browser is headless in Docker; the page's live thumbnails show what each sub-agent sees.
- [ ] Network: open https://httpbin.org/forms/post and https://news.ycombinator.com in a normal tab to confirm both are up.
- [ ] Budget check: `curl -s localhost:8000/api/health` shows `"ok": true`, `"browser": true`, `spend_usd` well below `budget_usd` (3.0). A demo run costs $0.004 to $0.04.
- [ ] Run the form task once beforehand, so you know it works on this network.
- [ ] Optional safety net: `python -m webforge serve-fixtures` serves offline test pages (`http://localhost:8765/forms/register.html`, `/forms/login.html`, `/data/specs.html`). This removes the dependency on the live sites, **not** on the LLM or the Backboard key.

## Run of show

### Beat 1: the pitch (30 s)

Say: "WebForge is an autonomous computer-use agent. You give it a job. A planner breaks it down, sends parallel sub-agents into real browser tabs, and every sub-agent has its own context. Anything consequential, like submitting or logging in, waits for me. Before it answers, a verifier checks it against the task. Cost is capped at $3 in total."

Show the **Architecture** tab.

### Beat 2 (headline): class registration with a human in the loop (about 60-90 s)

Start the offline form site (`python -m webforge serve-fixtures`), and for the full effect drive your own Chrome (`python -m webforge chrome`, then `export WEBFORGE_CDP_URL=http://127.0.0.1:9222` before `python -m webforge serve`). Click the **class registration** chip, or paste the text in `fixtures/site/demo/class_list.txt`.

Watch: four agents open four tabs **at once** in your Chrome (badge "WebForge · w2 · typing email…", the field being typed is outlined); one student's email is rejected by the form, so that agent **asks the teacher** and the tab jumps to the front; the teacher answers; then one grouped card "Submit registration x4" appears and a single **Approve all** submits all four. Four confirmation codes and `registrations.csv` appear, the verifier shows a tick, and the **Monitor** section shows live gauges, the per-agent table and the log (download as `.jsonl`).

Measured (live, Haiku 5.5, headless own Chromium, simulated teacher): **3 of 3 runs passed** after the fixes, 62-102 s, 22-34 LLM calls, about $0.008-0.012 each, four distinct codes plus a correct CSV, verifier complete every time. Before the fixes two of two completed runs failed (a sub-agent re-asked the same question 30 times; the verifier judged on truncated evidence), which is why those are now fixed in code. Not run live here: driving your own installed Chrome (tested only against a Chrome started by the same launcher).

### Beat 3 (quick backup): act on a real form, with approval (about 40 s)

Chip **fill a form**, or paste:

> Go to https://httpbin.org/forms/post and order a large pizza with bacon and mushroom for Ada Lovelace (phone 555-0100, email ada@example.org), delivery time 19:00, comment 'ring twice'. Submit it and tell me exactly what the server received.

Watch: the browser thumbnail fills in; the status turns **waiting for you** and the amber card lists every value. Click **approve**. The answer quotes the server's echo (`custname`, `custtel`, `size: large`, `topping: [bacon, mushroom]`, `delivery: 19:00`, `comments: ring twice`).

Measured: 30 s, 8 LLM calls, $0.0036, verifier passed, server echo matched every value.

Optional: run it again and click **deny**. The agent reports that it was not done and why; it does not retry. (Without an answer in 90 s the action counts as not approved.)

### Beat 3: parallel research, decision and files (1-4 min)

Chip **compare laptops**, or paste:

> Compare 3 good student laptops under 1000 dollars (price, battery life, weight). Recommend one and save the comparison as outputs/laptops.csv and your recommendation as outputs/recommendation.md

Watch: several searches run at once, the plan fills in, the analyst writes the files, and the **verifier** may send the planner back once for missing prices (it did in the measured run). Download `laptops.csv` and `recommendation.md` from the **Deliverables** links.

Measured: 260 s, 44 LLM calls, $0.04, two deliverables, verifier rejected once, then passed. It is the slow beat: skip it, or start it first and talk over it, if time is short. The answer states plainly what it could not verify (no manufacturer prices were reachable).

### Beat 4: parallel browsing (about 1 minute)

Chip **HN digest**, or paste:

> Summarize the top 3 stories on Hacker News right now. For each give the title, points, comment count and the top comment. Save it as outputs/hn.md

Watch the three coloured lanes `w1 w2 w3` in the timeline and three live thumbnails: three sub-agents, three tabs, at once. HN may rate-limit three simultaneous loads; the agents reload and mention it in the answer.

Measured: 56 s, 22 LLM calls, $0.010, 3 parallel workers, `hn.md` saved.

### Beat 5: stop and resume (about 1 minute)

Start any research task, press **Stop** after about 20 seconds, then **Resume**. The run continues from its checkpoint with the saved workspace instead of starting over (CLI: `python -m webforge resume <run_id>`).

Measured: stopped after round 2 with 3 workspace files; the resumed run finished in 103 s with `outputs/versions.md`.

### Beat 6 (optional): a login with a secret

In the **Secrets** panel add `site_pw`; then run the task "Open http://127.0.0.1:8765/forms/login.html and log in as user ada with the secret site_pw. Tell me the message the page shows" (needs `serve-fixtures`; password for the fixture page is `hunter2-secret`). The agent types `{{secret:site_pw}}`; the browser fills the real value; the page says "Welcome back, ada."; the value appears in no event, log or saved file (checked with `grep`).

Close with the cost line: "Everything today, including development tests, is well under the $3 cap" (read the exact figure from `curl -s localhost:8000/api/spend`).

## Honest caveats to say out loud

- Web search answers are only as good as the sources. The verifier checks completeness and unsupported claims against the evidence, not ground truth.
- Sites with captchas, bot walls or required logins can stop a sub-agent; it reports that and tries other sources. Payments are blocked by design.
- Haiku sometimes replies in prose instead of calling a tool; the planner nudges it (a dim "nudge" line in the timeline). Not a crash.
- Live runs vary in time and steps. The research beat can take over four minutes; the default run budget is 300 s, with a wrap-up note at 70 %.

## Failure recovery

| Symptom | Likely cause | Action |
|---|---|---|
| Status stuck on "waiting for you" | An approval or question is open | Answer the amber card; after 90 s the action is treated as not approved |
| Run ends "incomplete", or the planner keeps describing plans | Model stalled in prose (3 consecutive nudges used) | Run again; or press **Resume** |
| Run stopped, errored or Wi-Fi dropped mid-run | Connection or backend problem | Press **Resume** (or `python -m webforge resume <run_id>`) |
| Live site down or layout changed | httpbin / HN unavailable | Use `serve-fixtures` and the pages under `http://localhost:8765/forms/` and `/data/` |
| HN says "we're not able to serve your requests this quickly" | Three simultaneous loads from one IP | The agents reload on their own; or run the task once more |
| HTTP 402 "Budget exhausted" | Ledger reached `WEBFORGE_BUDGET_USD` | `curl -s localhost:8000/api/spend`; raise the cap in `.env` only if you are sure |
| HTTP 409 "Another run is in progress" | A previous run is still active | Wait, or press **Stop** (one run at a time) |
| HTTP 401 | `WEBFORGE_ACCESS_CODE` set on the server | Enter the access code in the Connection card |
| `health.browser` is false | Chromium not installed | `python -m playwright install --with-deps chromium` (inside the venv), restart |
