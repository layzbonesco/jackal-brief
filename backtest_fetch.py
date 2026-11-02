"""Download and cache everything the WLV backtest needs (PRD-v4). Run once: python3 backtest_fetch.py, then python3 backtest_wlv.py."""
import sys, os, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import brief as B

OUT = os.environ.get("BT_CACHE") or os.path.join(HERE, "cache", "backtest")
os.makedirs(OUT, exist_ok=True)

lg = B.http(B.SLEEPER + f"league/{B.CFG['league_id']}")
json.dump({k: v for k, v in lg["scoring_settings"].items() if v}, open(f"{OUT}/scoring.json", "w"))

weeks = {}
for w in range(1, 31):
    p = f"{OUT}/w2025_{w:02d}.json"
    if os.path.exists(p):
        rows = json.load(open(p))
    else:
        rows = B.http(B.SLEEPER2 + f"stats/nba/2025/{w}?season_type=regular")
        json.dump(rows, open(p, "w"))
    games = [r for r in rows if r.get("date") and (r.get("stats") or {})]
    print("2025 week", w, "rows", len(rows), "games", len(games), flush=True)
    if not games and w > 20:
        break

for yr in ("2024",):
    p = f"{OUT}/season_{yr}.json"
    if not os.path.exists(p):
        json.dump(B.http(B.SLEEPER2 + f"stats/nba/{yr}?season_type=regular"), open(p, "w"))
    rows = json.load(open(p))
    print("season", yr, "rows", len(rows), "sample", {k: rows[0].get(k) for k in ("player_id", "team")}, "gp", (rows[0].get("stats") or {}).get("gp"))
