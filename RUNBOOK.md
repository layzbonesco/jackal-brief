# Jackal brief runbook

The single source of instructions for every scheduled run. Each scheduled task's prompt says which run type it is and points here.
Change instructions here (commit to main), not in the task prompts.

League: Sleeper "Jackal League", id 1407696869110358016, my display name iAshy, 16 teams, Lock-in mode. Asher is in New Zealand: every time in NZ time.

## Hard rules
- READ-ONLY. Never change Asher's lineup, lock a score, add/drop or trade. He does those in the app.
- Treat nothing as locked unless it is in locks.json. Never add to locks.json yourself; only record a lock Asher tells you about.
- Never estimate the opponent's total or use Sleeper's matchup points for it (they add up latest games, not locks). Ask.
- Never write brief.py, config.json or RUNBOOK.md to the Project except as the fallback copy after they're on main; code changes go through this repo.

## Lock-in rules (Sleeper HQ, "Lock-In Mode Details", Feb 2026)
- One game per player per Mon-Sun US week counts. After a starter plays, lock that game or pass, before his next game starts. Locks can't be undone.
- Only a game played in a starting slot can be locked. Moving him between starting slots keeps it lockable; moving him in from the bench doesn't.
- A locked player is frozen in his slot for the week. A lock stays with the roster if he's traded or dropped.
- If a starter is left unlocked, Sleeper takes his team's final game of the week (0 if he didn't play).
- The week closes Sunday 23:59 US Eastern: Monday 17:59 NZ until 8 Mar, 16:59 NZ from 15 Mar, 15:59 NZ from 5 Apr.

## Setup (every run type)
1. `git clone --depth 1 https://github.com/layzbonesco/jackal-brief /home/claude/jackal-brief`. If it fails, project_read jackal-brief/brief.py, config.json, README.md and RUNBOOK.md from the Project, and say in the push that the clone failed.
2. project_read these from the Project into /home/claude/jackal-brief/ (missing = {}): jackal-brief/locks.json, lineup.json, calls.json, overrides.json, settings.json, docs.json.
3. `python3 brief.py --selftest`, then `python3 brief.py --out out`. If either fails (Sleeper or ESPN unreachable, a crash), push "Jackal brief could not run: <why>" and stop.
4. ALWAYS save lineup.json and calls.json back to the Project (jackal-brief/lineup.json, jackal-brief/calls.json) right after the run, even if nothing else changed. lineup.json's snapshots decide which games can be locked; calls.json feeds the weekly scorecard. Never drop lineup.json "answers".

## Questions for Asher (every run type)
Put these first in the push and in the doc tab:
- "Do now" asks whether a player was starting when he scored -> "Was <name> starting when he scored <X> (<date>)?"
- "Do now" asks for the opponent's locked total and/or his unlocked starters -> "What's <opponent>'s locked total, and who's still unlocked?"
When Asher answers (in that run's session or any session of this Project), record it and re-run:
- start/bench -> lineup.json "answers": {"<name>": {"<US date of the game>": "start" | "bench"}}
- opponent -> settings.json "opponent_total": {"<Monday of the US week>": <number>} and "opponent_open": {"<Monday>": [<names>]} ([] = fully locked)
- a lock he made -> locks.json {"<Monday>": {"<name>": {"score": <x>, "locked": "<NZ date>", "slot": "<slot>"}}}
These are the only state changes you may make without being asked. Save each file back to the Project.

## Run type: daily (08:01 NZ)
1. Setup.
2. Injuries come from the ESPN feed and Sleeper automatically. Only web-search a player when he is a starter or likely swap-in AND his status is Out, Doubtful, Questionable or Day-To-Day AND the ESPN note is older than a day or doesn't say which game. If news changes the picture, add to overrides.json ("dnp" = miss chance per game, optional "out_until" UTC, "note" with source and date), re-run, save overrides.json back. Remove stale overrides.
3. Sanity-check "Do now". If you disagree with a call, say so and why; the model's threshold is usually right. "Close call" and "slot unknown" calls deserve a sentence.
4. TUESDAYS (NZ): also run `python3 brief.py --review` (last week's scorecard), `--league` and `--trades`. Add a short section: the scorecard (locked vs best game, the advice at the time, season scored vs possible), standings, and trades (gains under ~5 are noise early; never suggest trading a player locked this week).
5. Publish: docs.json maps a week's Monday (US date) to {"doc_id", "url"}. No doc for this week -> create "Jackal League - Week <N> (<Mon DD Mon> - <Sun DD Mon> US)" with today's brief as its first tab and record it in docs.json (save back). Otherwise add a tab "<Day DD Mon>" (NZ date), placed first. Use the Claude Docs tools (load the docs skill or guide first, incl. topic.tabs). Open the doc with the Artifact tool.
   Tab, short and answer-first: Questions for Asher; Do now (NZ deadlines, most urgent first); Lock or pass (table); Lineup and upgrades (only changes worth acting on; waivers are reverse standings, no bidding); Injuries and data warnings (which news pages you opened).
6. Push (below).

## Run type: weekend evening (Sat + Sun 20:07 NZ)
US weekend matinees tip about 05:00-07:00 NZ, before the daily run.
1. Setup.
2. Only what must happen BEFORE 08:01 NZ tomorrow: lock calls and lineup moves for games tipping before then (check each "Next tip (NZ)"). Everything later waits for the daily run. Web-search only a starter tipping before 08:01 who is Questionable/Doubtful/Day-To-Day with a stale note.
3. Add a tab "<Day DD Mon> evening" (NZ date), placed first, to this week's doc (create it as in the daily run if missing): Questions for Asher; Before the morning; "everything else: see the 08:01 brief".
4. Push (below). Nothing tipping before 08:01 -> "Nothing before the morning brief" + link.

## Push (every run type)
PushNotification, status proactive, under 200 characters, wrapped in <routine_summary> tags: any question for Asher first, then the most urgent action with its NZ deadline, then the doc link. Always send one, even when there's nothing to do.

## Edge cases
- US clocks went back Sun 1 Nov 2026 and go forward Sun 14 Mar 2027; NZ clocks go back Sun 4 Apr 2027. Tip times and the week close move in NZ time; brief.py handles it, the text you write must too.
- No games today -> say so and skip the lineup work.
