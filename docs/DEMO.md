# Live demo: run of show (about 8 minutes)

Goal: show that WebForge solves a web task with an LLM, turns what worked into a skill, and then solves a *similar* task faster and cheaper by reusing it. Live runs vary; the numbers below are from one recorded pair of runs (Haiku 5.5 via Backboard) and are targets, not guarantees.

## Pre-flight checklist (15 minutes before)

- [ ] Pick one path (never show your key on screen):
  - **Local Python:** `.env` exists with a valid `BACKBOARD_API_KEY` (copy from `.env.example`); `pip install -r requirements.txt` and `python -m playwright install chromium` done; start with `python -m webforge serve` (add `--headed` to show the browser window).
  - **Docker:** `docker compose up --build`, open http://localhost:8000 and paste the key into the "Connect Backboard" card (or set `BACKBOARD_API_KEY` beforehand). The browser is headless in Docker, so `--headed` is only available on the host.
- [ ] Network works; open https://books.toscrape.com in a normal browser tab to confirm the site is up.
- [ ] Start the server: `python -m webforge serve`, open http://localhost:8000.
  - Start the server with `python -m webforge serve --headed` so the real Chromium window opens on the projector while the audience also sees the todo list, actions timeline and metrics in the page. Without `--headed` the browser runs invisibly.
- [ ] Budget check: `curl -s localhost:8000/api/health` shows `"ok": true`, `"browser": true`, `spend_usd` well below `budget_usd` (default 3.0). A run costs about $0.001-0.01.
- [ ] Decide the starting state:
  - **Clean cold start (recommended for the story):** `python -m webforge skills list` should not show a books-category skill. If one exists and you want beat 2 to be genuinely cold, delete it: `python -m webforge skills rm extract_category_books_sorted_by_price` (name as shown by `skills list`).
  - **Pre-seeded (safer):** run the Poetry task once beforehand so a skill exists, and say so honestly; then beat 2 will already show reuse and you can skip straight to beat 3 for a second reuse.
- [ ] Optional safety net: in another terminal, `python -m webforge serve-fixtures` (offline demo site at http://localhost:8765). Note this removes the dependency on the live website, **not** on the LLM or the Backboard key.
- [ ] Keep one terminal visible for `skills list` / `memory list`.

## Run of show

### Beat 1: explain (30 s)

Say: "Most web agents rediscover the same clicks every time. WebForge is a planner that delegates browsing to a worker. When a worker succeeds, we compile its trace into a small JSON *skill*, check it by replaying it, and save it. Next time a similar task comes in, the planner runs the skill directly with no browsing LLM. It also writes down lessons for next time. Cost is capped at $3 total."

Show: the **Architecture** tab (or one slide of the diagram in `docs/ARCHITECTURE.md`).

### Beat 2: first task, cold (about 25 s)

Run tab, example chip or paste:

> List the titles and prices of the 3 cheapest books in the 'Poetry' category on https://books.toscrape.com

Narrate while it runs: the planner writes a todo list; delegates one browsing subtask; the worker clicks the Poetry link and extracts the rows; the answer appears; then the "skill built" chip and the lesson appear.

Expected (recorded run): 3 steps, 8 LLM calls, about 2,900 tokens, about $0.003, about 25 s.

Say: "That is the expensive path: the model explored. Note it also built a skill and replayed it to check it works."

### Beat 3: similar task, skill reuse (about 12 s)

Same page, new category:

> List the titles and prices of the 3 cheapest books in the 'Travel' category on https://books.toscrape.com

Point at the "skill_run" entry and the skill chip: no worker was delegated.

Expected (recorded run): 2 steps, 4 LLM calls, about 1,300 tokens, about $0.0006, about 12 s. That is roughly half the time and under half the tokens. The Travel answer in the recorded run was checked by hand against the live site; you can open the Travel page to confirm.

Say: "Same site, different category: the skill takes the category as a parameter."

### Beat 4: Skills and Memory tab (1 min)

Open **Skills & Memory**. Show the skill card (parameters, steps, uses/successes), then the memory list (lessons). Explain demotion: a skill with at least 3 uses and under 50% success is hidden from the planner; breaking skills get one LLM repair first.

### Beat 5: Learning tab (1-2 min)

Show benchmark results. Be precise about what they are:

- 9 tasks, one run each: correct 8/9 cold vs 9/9 learned; mean steps 3.4 vs 1.8.
- The gain is concentrated in the Books family (Quotes and Wikipedia tasks take about 1 step either way, so there is nothing to learn). With n=1 per task, the single cold failure may be noise.
- Repeat benchmark (3 runs per task, 27 per mode): correct 24/27 cold vs 27/27 learned; mean steps 2.85 vs 2.04; mean seconds 15.5 vs 12.0. Again the gain is in Books (all 3 cold failures were `books_mystery`); Quotes and Wikipedia show no real gain. Full table in the README.

### Beat 6 (optional): long-horizon finale (about 50 s)

> On https://news.ycombinator.com find the top 3 stories on the front page. For each story: open its comments page, then record its title, points, number of comments, and the first sentence of the top comment. Save your findings for each story as you go using write_file, then write a final report.md that summarizes all three stories and compares them, and give me the full report as your final answer

This is the wording of the recorded run (it is under the 500-character task limit). Expected: about 47 s, 7 steps, 14 LLM calls, about $0.006, reusing 2 skills built earlier. Numbers in the recorded report matched live HN (two exact, one within normal live drift). Only do this beat if the earlier ones went smoothly and you have the time; HN changes constantly so the answer will differ from the recording.

Close with the cost line: "The whole project, including all benchmarks, has spent well under the $3 cap" (read the exact figure from `curl -s localhost:8000/api/spend`).

## Honest caveats to say out loud

- Haiku sometimes answers in prose instead of calling a tool; the planner nudges it (up to 3 times in a row). You may see a "nudge" warning in the timeline. That is expected behaviour, not a crash.
- Live runs vary in steps and seconds. If a number differs from the slide, that is normal.
- Learning only helps repeated *kinds* of task; one-off tasks cost the same as before, plus the small cost of building a skill.

## Failure recovery

| Symptom | Likely cause | Action |
|---|---|---|
| Run ends "incomplete" or the planner keeps describing plans | Model stalled in prose (3 consecutive nudges used) | Say "small models do this"; click Run again. A rerun usually completes. |
| Worker loops or hits "step budget exhausted" | Page changed or model confused | Rerun; if it repeats, switch to the offline site (next row). |
| Live site down or its layout changed | books.toscrape.com / HN unavailable | Start `python -m webforge serve-fixtures` and use `http://localhost:8765` in the task, e.g. "List the titles and prices of the 3 cheapest books in the 'Poetry' category on http://localhost:8765" (fixture categories: Poetry, Travel, Science). The skill is then learned for the `localhost` domain. |
| Wi-Fi down | No access to Backboard or the sites | Live runs are impossible. In the web UI use **Replay a recorded run** (Run tab, works without a backend). Say clearly that it replays a *recorded* run for illustration; it is not live. |
| HTTP 402 "Budget exhausted" / `BudgetExceeded` | Ledger reached `WEBFORGE_BUDGET_USD` | Check `curl -s localhost:8000/api/spend`. Raise `WEBFORGE_BUDGET_USD` in `.env` and restart the server, only if you are sure. |
| HTTP 409 "Another run is in progress" | A previous run is still active | Wait for it to finish (one run at a time). |
| HTTP 401 | `WEBFORGE_ACCESS_CODE` set on the server | Enter the access code in the Connection card. |
| `health.browser` is false | Chromium not installed | `python -m playwright install chromium`, restart. |
| Beat 3 does not show skill reuse | Skill was not saved in beat 2 (replay validation failed) or planner chose to browse | Check the timeline for "skill not built"; run `python -m webforge skills list`. Rerun beat 2, then beat 3. |
