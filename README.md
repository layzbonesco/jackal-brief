# Jackal League daily brief (v2)

Read-only tool for Asher's (iAshy) Sleeper NBA league "Jackal League" (id 1407696869110358016).
It reads Sleeper and ESPN, scores players under the league's own scoring, and writes a lock / lineup / upgrade brief.
It never changes anything in Sleeper: locks, lineups, adds and trades are done by hand in the app.

## Where things live
- **Code** (this repo, `layzbonesco/jackal-brief`): `brief.py`, `config.json`, `README.md`.
- **State** (claude.ai Project "Sleeper", folder `jackal-brief/`): `locks.json` (scores Asher has locked, keyed by the Monday of the US week),
  `overrides.json` (manual injury calls), `docs.json` (the link to each week's brief doc).

## Usage
```
python3 brief.py [--now 2026-10-29T20:20:00Z] [--out DIR]   # daily brief -> DIR/brief.md + brief.json
python3 brief.py --league                                     # every roster compared (Tuesday edition)
python3 brief.py --trades                                     # 1-for-1 and 2-for-1 trades that help both sides
python3 brief.py --selftest                                   # checks the lock math against a simulation
```
Python 3.9+, standard library only. Copy `locks.json` and `overrides.json` from the Project next to `brief.py` before running.

## League facts
- 16 teams, points scoring, Lock-in mode, matchup week Mon-Sun US Eastern (closes Mon ~18:00 NZ, ~17:00 NZ before NZ daylight saving starts).
- Lock-in: one game per player counts per week. After a starter plays, lock or pass before his next game.
  **An unlocked starter scores 0 at week's end**, so his last game of the week must be locked.
  Bench scores are assumed not lockable (unverified). Locked players' slots are frozen.
- Lineup: PG SG G SF PF F C UTIL UTIL + 5 bench + IR. Waivers clear in 2 days. Trade deadline week 18, playoffs from week 22.
- Scoring is read from the league each run (pts 0.5, reb 1, ast 1, stl 2, blk 2, to -1, 3PM 0.5, DD 1, TD 2, 40/50-pt bonus 2 each, flagrant/technical -2).

## Model
- **Per-game mean**: last season's FP/game (Sleeper season stats, players with 10+ games; else this season's Sleeper projection; else 12),
  blended with this season's games as if last season were 5 games (`prior_weight_games`). Spread blends 28% of the mean with observed spread, minimum 5.
- **Miss chance per game**: overrides.json first, then the ESPN injuries feed (Out / Day-To-Day / Questionable...; "expected to miss time" means every game this week), then Sleeper's status, else 5%.
- **Lock threshold** = value of passing, by optimal stopping over the remaining games: work back from the last game (which you'd always lock, since unlocked = 0).
  Lock if the score is at least that value. Calls within 2 points are marked close.
- **Lineup**: locked starters and players whose game is live are frozen; the best layout of the open slots is found by assignment. Changes under 2 points are shown as optional.
- **Free agents**: active, not rostered, 2+ games at 18+ minutes or last season 22+ FP/game. Gain = lineup value with him in place of one of the 4 weakest bench players.
  "waiver" = his team already played today, so the add is probably a waiver claim.
- Data warnings (a failed feed, our score differing from Sleeper's by more than 0.5) are listed at the end of the brief.

## Known limits
- Sleeper's stats endpoints (`api.sleeper.com/stats/...`) and ESPN's feeds are unofficial and could change.
- Flagrant and technical fouls aren't in the game stats, so those rare -2s are missed (a warning shows if Sleeper's score differs).
- US clocks go back Sun 1 Nov: tips move one hour later in NZ. Weekend games can tip before 10am NZ; the previous day's brief covers them.
