#!/usr/bin/env python3
"""Jackal League daily brief v2. Read-only: never changes anything in Sleeper.

Data:  Sleeper (league, rosters, matchups, players, weekly game stats, last season's stats, projections)
       ESPN (scoreboard for tip times / live state, injuries feed)
Usage: python3 brief.py [--now 2026-10-29T20:00:00Z] [--out DIR] [--league] [--trades] [--selftest]
"""
import argparse, collections, datetime as dt, json, math, os, random, re, sys, time, unicodedata, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))


def merge(base, over):
    """Overlay over onto base in place; nested dicts merge key by key."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v


if os.path.exists(os.path.join(HERE, "settings.json")):  # tuning dials kept in the Claude Project
    merge(CFG, json.load(open(os.path.join(HERE, "settings.json"))))
ET, NZ, UTC = ZoneInfo("America/New_York"), ZoneInfo("Pacific/Auckland"), dt.timezone.utc
SLEEPER, SLEEPER2 = "https://api.sleeper.app/v1/", "https://api.sleeper.com/"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
TEAMMAP = CFG["espn_to_sleeper_team"]
WARN = []  # data problems to surface in the brief


def http(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def soft(url, default, label):
    try:
        return http(url)
    except Exception as e:  # noqa
        WARN.append(f"{label} unavailable ({e.__class__.__name__}); continuing without it")
        return default


def cached(name, loader, max_age_h):
    d = os.path.join(HERE, CFG["cache_dir"])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) / 3600 < max_age_h:
        return json.load(open(p))
    data = loader()
    json.dump(data, open(p, "w"))
    return data


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


# ---------------- scoring (read from the league) ----------------
def make_fp(scoring):
    def fp(st):
        st = dict(st)
        pts = st.get("pts", 0) or 0
        if "bonus_pt_40p" not in st:
            st["bonus_pt_40p"] = 1 if pts >= 40 else 0
        if "bonus_pt_50p" not in st:
            st["bonus_pt_50p"] = 1 if pts >= 50 else 0
        return sum(w * (st.get(k) or 0) for k, w in scoring.items())
    return fp


# ---------------- lock math ----------------
def _phi(z):
    return math.exp(-z * z / 2) / math.sqrt(2 * math.pi)


def _Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def e_max(c, mu, sd, p_dnp):
    """E[max(X, c)], c >= 0, X = 0 with prob p_dnp else max(0, Normal(mu, sd))."""
    z = (c - mu) / sd
    played = c * _Phi(z) + mu * (1 - _Phi(z)) + sd * _phi(z)
    return p_dnp * c + (1 - p_dnp) * played


def pass_value(mu, sd, dnps):
    """Value of NOT locking now, given the remaining games' DNP chances in order.
    Optimal stopping: an unlocked starter scores 0 at week's end, so the last game is always locked."""
    v = 0.0
    for p in reversed(dnps):
        v = e_max(v, mu, sd, p)
    return v


def win_probability(entries, fixed, opp_total, n=12000, seed=7, min_gain=0.005, max_open=8):
    """Monte Carlo chance of finishing above opp_total, and the lock/pass calls that maximise it.

    entries: open starters, each dict(name, s0, mu, sd, dnps, ev_lock):
      s0       latest lockable score this week (None if there is nothing to lock yet)
      dnps     miss chance for each remaining game, in order
      ev_lock  whether the points rule (lock if s0 >= value of passing) says lock now
    fixed: points already banked (locked slots, plus forced locks with no games left).
    A player who passes follows the points rule for his later games; his last game is always locked
    (an unlocked starter scores 0). Every lock/pass combination is scored on the same simulated weeks,
    so differences between combinations are not noise. Ties count half."""
    rnd = random.Random(seed)

    def pass_path(e):
        d = e["dnps"]
        th = [pass_value(e["mu"], e["sd"], d[i + 1:]) for i in range(len(d))]
        out = []
        for _ in range(n):
            res = 0.0
            for i, p in enumerate(d):
                g = 0.0 if rnd.random() < p else max(0.0, rnd.gauss(e["mu"], e["sd"]))
                if i == len(d) - 1 or g >= th[i]:
                    res = g
                    break
            out.append(res)
        return out

    for e in entries:
        e["path"] = pass_path(e)
        e["pv"] = pass_value(e["mu"], e["sd"], e["dnps"])
    choosable = sorted([e for e in entries if e["s0"] is not None], key=lambda e: abs(e["s0"] - e["pv"]))
    var, rest = choosable[:max_open], [e for e in entries if e not in choosable[:max_open]]
    base = [fixed] * n
    for e in rest:
        if e["s0"] is not None and e["ev_lock"]:
            base = [b + e["s0"] for b in base]
        else:
            base = [b + x for b, x in zip(base, e["path"])]
    for e in var:
        base = [b + x for b, x in zip(base, e["path"])]
    deltas = [[e["s0"] - x for x in e["path"]] for e in var]

    def pwin(arr):
        return sum(1.0 if t > opp_total else (0.5 if t == opp_total else 0.0) for t in arr) / n

    k = len(var)
    score = {}

    def rec(j, arr, mask):
        if j == k:
            score[mask] = pwin(arr)
            return
        rec(j + 1, arr, mask)
        rec(j + 1, [a + d for a, d in zip(arr, deltas[j])], mask | (1 << j))

    rec(0, base, 0)
    ev_mask = sum(1 << j for j, e in enumerate(var) if e["ev_lock"])
    top = max(score.values())
    # among combinations within simulation noise of the best, prefer the one closest to the points rule
    # (so ties never lock more players than needed)
    best_mask = min((m for m in score if score[m] >= top - 0.002), key=lambda m: (bin(m ^ ev_mask).count("1"), -score[m]))
    if score[best_mask] - score[ev_mask] < min_gain:
        best_mask = ev_mask  # not worth departing from the points rule on a gain this small
    return dict(p_ev=score[ev_mask], p_best=score[best_mask], p_all=score[(1 << k) - 1], n=n, k=k,
                calls={e["name"]: bool(best_mask >> j & 1) for j, e in enumerate(var)},
                ev={e["name"]: e["ev_lock"] for e in var}, skipped=len(choosable) - k)


def selftest():
    r = random.Random(1)
    mu, sd, p = 25.0, 7.0, 0.07
    g = lambda: 0.0 if r.random() < p else max(0.0, r.gauss(mu, sd))
    N = 200000
    last = sum(g() for _ in range(N)) / N
    sim = sum(max(g(), last) for _ in range(N)) / N
    ana = pass_value(mu, sd, [p, p])
    ok = abs(sim - ana) < 0.15
    print(f"selftest: 2 games left mu 25 sd 7 -> simulated {sim:.2f}, analytic {ana:.2f}: {'OK' if ok else 'FAIL'}")
    # win probability: one forced game vs an opponent sitting at the player's mean -> about 50%
    e = dict(name="x", s0=None, mu=20.0, sd=5.0, dnps=[0.0], ev_lock=False)
    w = win_probability([e], 0.0, 20.0, n=40000)
    ok2 = abs(w["p_best"] - 0.5) < 0.02
    print(f"selftest: win chance with one game vs the mean -> {w['p_best']:.3f} (want about 0.5): {'OK' if ok2 else 'FAIL'}")
    # a lockable 30 against an opponent on 29 with 2 weak games left: locking must beat passing
    e = dict(name="y", s0=30.0, mu=12.0, sd=4.0, dnps=[0.05, 0.05], ev_lock=True)
    w = win_probability([e], 0.0, 29.0, n=20000)
    ok3 = w["calls"]["y"] and w["p_best"] > 0.99
    print(f"selftest: lock a 30 vs 29 with weak games left -> lock={w['calls']['y']}, win {w['p_best']:.3f}: {'OK' if ok3 else 'FAIL'}")
    return ok and ok2 and ok3


# ---------------- lineup assignment (Hungarian, min cost) ----------------
def assign(cost):
    n, m = len(cost), len(cost[0])
    INF = 10 ** 12
    u, v, p, way = [0] * (n + 1), [0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j]}


ELIG = {"PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"}, "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"}, "C": {"C"}}


def best_lineup(slots, players):
    """players: dicts with pos and val (val may be a dict slot->value or a float). Returns (total, {slot_i: player_i})."""
    if not slots or not players:
        return 0.0, {}
    BIG = 10 ** 9
    pad = max(0, len(slots) - len(players))
    cost = []
    for s in slots:
        row = []
        for p in players:
            ok = s == "UTIL" or bool(ELIG.get(s, set()) & set(p["pos"]))
            row.append(-int(p["val"] * 100) if ok else BIG)
        row += [0] * pad  # empty slot
        cost.append(row)
    a = assign(cost)
    total, out = 0.0, {}
    for i, j in a.items():
        if j < len(players) and cost[i][j] < BIG:
            total += players[j]["val"]
            out[i] = j
    return total, out


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now")
    ap.add_argument("--league", action="store_true")
    ap.add_argument("--trades", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if selftest() else 1)
    now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else dt.datetime.now(UTC)
    os.makedirs(args.out, exist_ok=True)
    L = CFG["league_id"]

    # ---- Sleeper league
    state = http(SLEEPER + "state/nba")
    week, season = state["week"], str(state.get("season") or CFG["season"])
    league = http(SLEEPER + f"league/{L}")
    rosters = http(SLEEPER + f"league/{L}/rosters")
    users = {u["user_id"]: u["display_name"] for u in http(SLEEPER + f"league/{L}/users")}
    matchups = http(SLEEPER + f"league/{L}/matchups/{week}")
    P = cached("players.json", lambda: http(SLEEPER + "players/nba"), 20)
    scoring = {k: v for k, v in league["scoring_settings"].items() if v}
    fp = make_fp(scoring)
    owner = {r["roster_id"]: users.get(r["owner_id"], "?") for r in rosters}
    mine = next(r for r in rosters if owner[r["roster_id"]] == CFG["my_display_name"])
    rostered = {pid: r["roster_id"] for r in rosters for pid in (r["players"] or []) + (r.get("reserve") or [])}
    slots = [s for s in league["roster_positions"] if s not in ("BN", "IR")]

    # ---- week window (Mon-Sun US Eastern)
    now_et = now.astimezone(ET)
    wk_start = (now_et - dt.timedelta(days=now_et.weekday())).date()
    wk_end = wk_start + dt.timedelta(days=6)
    wk_days = [(wk_start + dt.timedelta(days=i)).isoformat() for i in range(7)]
    week_close = dt.datetime.combine(wk_end, dt.time(23, 59), ET)

    # ---- schedule (ESPN), current week only
    events = []
    for day in wk_days:
        sb = soft(ESPN + "scoreboard?dates=" + day.replace("-", ""), {"events": []}, f"ESPN scoreboard {day}")
        for e in sb.get("events", []):
            comp = e["competitions"][0]
            events.append(dict(day=day, start=e["date"], state=e["status"]["type"]["state"],
                               done=e["status"]["type"]["completed"],
                               teams=[TEAMMAP.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in comp["competitors"]]))

    def tip(iso):
        return dt.datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)

    future = collections.defaultdict(list)  # team -> games not finished (live first), in order
    live = set()
    for e in sorted(events, key=lambda e: e["start"]):
        if e["done"]:
            continue
        for t in e["teams"]:
            if e["state"] == "in" or tip(e["start"]) <= now:
                live.add(t)
            future[t].append(e)

    def nz(iso_or_dt):
        t = tip(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
        return t.astimezone(NZ).strftime("%a %H:%M")

    # ---- game logs (Sleeper weekly stats; one call per week, keyed by Sleeper player id)
    glog = collections.defaultdict(list)
    for w in range(1, week + 1):
        rows = soft(SLEEPER2 + f"stats/nba/{season}/{w}?season_type=regular", [], f"Sleeper stats week {w}")
        for r in rows:
            st = r.get("stats") or {}
            if not st or not r.get("date"):
                continue
            glog[r["player_id"]].append(dict(date=r["date"], min=st.get("min", 0) or 0, fp=round(fp(st), 2), team=r.get("team")))
    for pid in glog:
        glog[pid].sort(key=lambda g: g["date"])

    # ---- prior: last season's per-game FP, else this season's projection, else a flat baseline
    prior = {}
    last = soft(SLEEPER2 + f"stats/nba/{int(season) - 1}?season_type=regular", [], "Sleeper last-season stats")
    for r in last:
        st = r.get("stats") or {}
        gp = st.get("gp") or 0
        if gp >= CFG["prior_min_gp"]:
            prior[r["player_id"]] = fp(st) / gp
    proj = soft(SLEEPER + f"projections/nba/regular/{season}", {}, "Sleeper projections")
    for pid, st in proj.items():
        if pid not in prior and st and st.get("pts"):
            prior[pid] = fp(st) / (st.get("gp") or 1)

    def model(pid):
        xs = [g["fp"] for g in glog.get(pid, []) if g["min"] > 0]
        pm = prior.get(pid, CFG["baseline_fp"])
        k = CFG["prior_weight_games"]
        mu = (k * pm + sum(xs)) / (k + len(xs))
        sd0 = max(CFG["min_sd"], CFG["sd_ratio"] * pm)
        ss = k * sd0 ** 2 + sum((x - mu) ** 2 for x in xs)
        sd = max(CFG["min_sd"], math.sqrt(ss / (k + len(xs))))
        return mu, sd, len(xs), pm

    # ---- injuries: ESPN feed + Sleeper status + manual overrides
    espn_inj = {}
    for t in soft(ESPN + "injuries", {"injuries": []}, "ESPN injuries feed").get("injuries", []):
        for i in t.get("injuries", []):
            nm_ = (i.get("athlete") or {}).get("displayName")
            if nm_:
                espn_inj[norm(nm_)] = dict(status=i.get("status"), date=(i.get("date") or "")[:10], comment=i.get("shortComment") or "")
    OV = json.load(open(os.path.join(HERE, "overrides.json"))) if os.path.exists(os.path.join(HERE, "overrides.json")) else {}
    D = CFG["dnp"]

    def nm(pid):
        p = P.get(pid, {})
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

    def inj_info(pid):
        e = espn_inj.get(norm(nm(pid)))
        return dict(sleeper=P.get(pid, {}).get("injury_status"), body=P.get(pid, {}).get("injury_body_part"),
                    espn=e, override=OV.get(nm(pid)))

    def dnp_list(pid, games):
        o = OV.get(nm(pid)) or {}
        e = espn_inj.get(norm(nm(pid)))
        sl = P.get(pid, {}).get("injury_status")
        out = []
        for idx, g in enumerate(games):
            if o.get("out_until") and g["start"] < o["out_until"]:
                out.append(1.0)
                continue
            if "dnp" in o:
                out.append(o["dnp"])
                continue
            if e:
                st, c = (e["status"] or "").lower(), e["comment"].lower()
                if sl == "IR" or st == "out" and any(w in c for w in D["long_words"]):
                    out.append(D["out_long"])
                elif st == "out":
                    out.append(D["out_next"] if idx == 0 else D["out_later"])
                elif st in D["espn"]:
                    out.append(D["espn"][st] if idx == 0 else D["espn"][st] / 2)
                else:
                    out.append(D["default"])
            elif sl:
                v = D["sleeper"].get(sl, D["default"])
                out.append(v if idx == 0 or v >= 1 else max(D["default"], v / 2))
            else:
                out.append(D["default"])
        return out

    # ---- per-player valuation
    def team_of(pid):
        return P.get(pid, {}).get("team")

    def player(pid, starter=False):
        team = team_of(pid)
        games = [g for g in future.get(team, []) if g["day"] <= wk_end.isoformat()]
        mu, sd, n, pm = model(pid)
        dnps = dnp_list(pid, games)
        pv = pass_value(mu, sd, dnps) if games else 0.0
        wk = [g for g in glog.get(pid, []) if g["date"] in wk_days]
        lockable = wk[-1] if (starter and wk and team not in live) else None
        val = max(lockable["fp"], pv) if lockable else pv
        return dict(pid=pid, name=nm(pid), team=team, pos=P.get(pid, {}).get("fantasy_positions") or [],
                    mu=mu, sd=sd, gp=n, prior=pm, games=games, dnps=dnps, pass_value=pv, lock=lockable, val=val,
                    live=team in live, next_tip=nz(games[0]["start"]) if games else None,
                    next_utc=games[0]["start"] if games else "9999", inj=inj_info(pid))

    # ---- my roster
    reserve = set(mine.get("reserve") or [])
    starters = list(mine["starters"])
    me_ids = [p for p in mine["players"] if p not in reserve and p in P]
    me = {pid: player(pid, starter=pid in starters) for pid in me_ids}
    ir = [player(pid) for pid in reserve if pid in P]
    mm = next((m for m in matchups if m["roster_id"] == mine["roster_id"]), {})
    sl_pts = mm.get("players_points") or {}

    lp = os.path.join(HERE, "locks.json")
    LOCKS = (json.load(open(lp)) if os.path.exists(lp) else {}).get(wk_start.isoformat(), {})
    locked_pids = {pid for pid in starters if pid in me and me[pid]["name"] in LOCKS}

    # lock calls
    lock_rows, actions = [], []
    for i, pid in enumerate(starters):
        if pid not in me:
            continue
        r = me[pid]
        if pid in locked_pids:
            lock_rows.append(dict(name=r["name"], score=LOCKS[r["name"]]["score"], sleeper=None, left=0, next_tip=None,
                                  threshold=None, call="LOCKED", why="slot frozen for the week", avg=r["mu"]))
            continue
        n_left = len(r["games"])
        lk = r["lock"]
        if lk:
            s, pv = lk["fp"], r["pass_value"]
            if n_left == 0:
                call, why = "LOCK", "no games left: unlocked scores 0"
                actions.append((week_close.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
                                f"LOCK {r['name']} ({s:.1f}) now: no games left, unlocked = 0 (week closes {nz(week_close)} NZ)"))
            elif s >= pv:
                call, why = "LOCK", f"beats value of passing ({pv:.1f})"
                actions.append((r["next_utc"], f"LOCK {r['name']} ({s:.1f}) before his next tip ({r['next_tip']} NZ): passing is worth {pv:.1f}"))
            else:
                call, why = "PASS", f"passing is worth {pv:.1f}"
                if pv - s < CFG["close_call_fp"]:
                    why += " (close call)"
            lock_rows.append(dict(name=r["name"], score=s, sleeper=sl_pts.get(pid), left=n_left, next_tip=r["next_tip"],
                                  threshold=pv, call=call, why=why, avg=r["mu"]))
        elif n_left == 1 and not r["live"]:
            actions.append((r["next_utc"], f"{r['name']}: his {r['next_tip']} NZ game is his last this week; lock it once it finishes (unlocked = 0)"))
        elif n_left == 0 and not lk:
            actions.append(("0000", f"{r['name']} has no lockable game and no games left: 0 this week unless swapped (slot {slots[i]})"))

    # lineup: locked and live starters are frozen; live bench players can't come in
    frozen = {i for i, pid in enumerate(starters) if pid in locked_pids or (pid in me and me[pid]["live"])}
    open_slots = [i for i in range(len(slots)) if i not in frozen]
    cand = [pid for pid in me_ids if pid not in {starters[i] for i in frozen} and not (me[pid]["live"] and pid not in starters)]
    pool = [dict(pid=pid, pos=me[pid]["pos"], val=me[pid]["val"] if pid in starters else me[pid]["pass_value"]) for pid in cand]
    cur_total = sum(me[starters[i]]["val"] for i in open_slots if starters[i] in me)
    opt_total, opt = best_lineup([slots[i] for i in open_slots], pool)
    target = {open_slots[k]: pool[j]["pid"] for k, j in opt.items()}
    cur_open = {starters[i] for i in open_slots if starters[i] in me}
    tgt_set = set(target.values())
    ins = sorted(tgt_set - cur_open, key=lambda p: -me[p]["pass_value"])
    outs = sorted(cur_open - tgt_set, key=lambda p: me[p]["val"])
    changes = [dict(put=me[b]["name"], was=me[a]["name"], gain=me[b]["pass_value"] - me[a]["val"], tip=me[b]["next_tip"], utc=me[b]["next_utc"])
               for a, b in zip(outs, ins)]
    moves = [f"{slots[i]} {me[target[i]]['name']}" for i in open_slots if target.get(i) and target[i] != starters[i]]
    if opt_total - cur_total >= CFG["min_lineup_gain"] and changes:
        first = min(c["utc"] for c in changes)
        actions.append((first, "Start " + "; ".join(f"{c['put']} for {c['was']} (+{c['gain']:.1f})" for c in changes)
                        + f" before {nz(first) if first != '9999' else 'next tip'} NZ"))

    # free agents
    today_et = now_et.date().isoformat()
    played_today = {t for e in events if e["day"] == today_et and (e["done"] or e["state"] == "in" or tip(e["start"]) <= now) for t in e["teams"]}
    fa = []
    for pid, p in P.items():
        if pid in rostered or not p.get("team") or not p.get("active"):
            continue
        gl = [g for g in glog.get(pid, []) if g["min"] > 0]
        if not ((len(gl) >= CFG["min_games_fa"] and sum(g["min"] for g in gl) / len(gl) >= CFG["min_avg_minutes_fa"]) or prior.get(pid, 0) >= CFG["fa_prior_floor"]):
            continue
        if p.get("injury_status") in ("Out", "IR"):
            continue
        fa.append(player(pid))
    fa.sort(key=lambda r: -r["pass_value"])
    droppable = sorted([x for x in pool if x["pid"] not in starters], key=lambda x: x["val"])[:4]
    ups = []
    for r in fa[:20]:
        best = None
        for d_ in droppable:
            tp = [x for x in pool if x["pid"] != d_["pid"]] + [dict(pid=r["pid"], pos=r["pos"], val=r["pass_value"])]
            g = best_lineup([slots[i] for i in open_slots], tp)[0] - opt_total
            if best is None or g > best[0]:
                best = (g, d_["pid"])
        if best and best[0] > 0.5:
            ups.append(dict(add=r["name"], team=r["team"], pos="/".join(r["pos"]), val=r["pass_value"], left=len(r["games"]),
                            drop=me[best[1]]["name"], gain=best[0], mu=r["mu"], waiver=r["team"] in played_today))
    ups.sort(key=lambda x: (x["waiver"], -x["gain"]))
    for u in [u for u in ups if not u["waiver"]][:1]:
        if u["gain"] >= CFG["min_fa_gain"]:
            actions.append(("9998", f"Consider adding {u['add']} ({u['pos']}, {u['team']}) for {u['drop']}: +{u['gain']:.1f} this week"))
    # ---- win probability against this week's opponent
    wp = None
    opp_m = next((m for m in matchups if mm and m["matchup_id"] == mm.get("matchup_id") and m["roster_id"] != mine["roster_id"]), None)
    ov_total = (CFG.get("opponent_total") or {}).get(wk_start.isoformat())
    if opp_m and ov_total is None:
        # Sleeper's matchup points are each starter's latest game added up, not what he locked, so they can't stand in for his total
        wp = dict(opp=owner[opp_m["roster_id"]], missing=True, sleeper_pts=float(opp_m.get("points") or 0))
        actions.append(("0000", f"Win mode is off: add \"opponent_total\": {{\"{wk_start}\": <{wp['opp']}'s locked total>}} to settings.json "
                                f"(Sleeper's {wp['sleeper_pts']:.1f} is his latest games, not his locks)"))
    elif opp_m:
        opp_name = owner[opp_m["roster_id"]]
        opp_total = float(ov_total)
        fixed, entries = 0.0, []
        for pid in starters:
            if pid not in me:
                continue
            r = me[pid]
            n_left, lk = len(r["games"]), r["lock"]
            if pid in locked_pids:
                fixed += LOCKS[r["name"]]["score"]
            elif lk and n_left == 0:
                fixed += lk["fp"]
            elif n_left:
                entries.append(dict(name=r["name"], s0=lk["fp"] if lk else None, mu=r["mu"], sd=r["sd"], dnps=r["dnps"],
                                    ev_lock=bool(lk and lk["fp"] >= r["pass_value"]), next_utc=r["next_utc"], next_tip=r["next_tip"]))
        wp = win_probability(entries, fixed, opp_total, n=CFG["winprob_sims"], min_gain=CFG["winprob_min_gain"],
                             max_open=CFG["winprob_max_open"])
        wp.update(opp=opp_name, opp_total=opp_total, source="settings.json", fixed=fixed, mine_now=mm.get("points") or 0.0)
        if CFG.get("objective") == "win":
            gain = wp["p_best"] - wp["p_ev"]
            for row in lock_rows:
                c = wp["calls"].get(row["name"])
                if c is None or c == (row["call"] == "LOCK"):
                    continue
                row["call"] = "LOCK" if c else "PASS"
                row["why"] = f"win mode: {wp['p_ev']:.0%} to {wp['p_best']:.0%} win chance (points rule said {'lock' if c is False else 'pass'})"
                actions[:] = [a for a in actions if not a[1].startswith(f"LOCK {row['name']} (")]
                if c:
                    nxt = next(e for e in entries if e["name"] == row["name"])
                    actions.append((nxt["next_utc"], f"LOCK {row['name']} ({row['score']:.1f}) before his next tip ({nxt['next_tip']} NZ): "
                                                     f"win mode, lifts win chance vs {opp_name} from {wp['p_ev']:.0%} to {wp['p_best']:.0%}"))
    actions.sort(key=lambda x: x[0])

    rec = {r["roster_id"]: (r["settings"]["wins"], r["settings"]["losses"]) for r in rosters}

    # ---- trade search
    if args.trades:
        import itertools
        tvc = {}

        def tv(pid):
            if pid not in tvc:
                mu = model(pid)[0]
                tvc[pid] = mu * (1 - dnp_list(pid, [dict(start="9999")])[0])
            return tvc[pid]

        def total(ids):
            pl = [dict(pid=p, pos=P[p].get("fantasy_positions") or [], val=tv(p)) for p in ids if P.get(p, {}).get("fantasy_positions")]
            return best_lineup(slots, pl)[0]

        mine_ids = [p for p in mine["players"] if p not in reserve and p in P]
        base = total(mine_ids)
        found = []
        for r in rosters:
            if r["roster_id"] == mine["roster_id"]:
                continue
            their = [p for p in r["players"] if p not in (r.get("reserve") or []) and p in P]
            tb = total(their)
            for x in sorted([p for p in their if P[p].get("injury_status") not in ("Out", "IR")], key=lambda p: -tv(p))[:6]:
                for k in (1, 2):
                    for give in itertools.combinations(mine_ids, k):
                        gm = total([p for p in mine_ids if p not in give] + [x]) - base
                        gt = total([p for p in their if p != x] + list(give)) - tb
                        if gm > 0.5 and gt >= 0:
                            found.append(dict(owner=owner[r["roster_id"]], rec="%d-%d" % rec[r["roster_id"]], get=nm(x), give=[nm(p) for p in give], me=gm, them=gt))
        found.sort(key=lambda f: -f["me"])
        seen = set()
        print(f"My lineup per-game talent: {base:.0f}. Gains under ~5 are noise early in the season.")
        print("| Get | From (record) | Give | My gain | Their gain |\n|---|---|---|---|---|")
        n = 0
        for f in found:
            if (f["owner"], f["get"]) in seen or n >= 12:
                continue
            seen.add((f["owner"], f["get"]))
            n += 1
            print(f"| {f['get']} | {f['owner']} ({f['rec']}) | {' + '.join(f['give'])} | +{f['me']:.1f} | +{f['them']:.1f} |")
        return

    # ---- league comparison
    if args.league:
        rows = []
        for r in rosters:
            ids = [p for p in r["players"] if p not in (r.get("reserve") or []) and p in P]
            st = set(r["starters"])
            wp, tp_ = [], []
            for pid in ids:
                i = player(pid, starter=pid in st)
                if i["pos"]:
                    wp.append(dict(pid=pid, pos=i["pos"], val=i["val"] if pid in st else i["pass_value"]))
                    tp_.append(dict(pid=pid, pos=i["pos"], val=i["mu"]))
            s = r["settings"]
            rows.append(dict(owner=owner[r["roster_id"]], rec=f"{s['wins']}-{s['losses']}", pf=s.get("fpts", 0),
                             week=best_lineup(slots, wp)[0], talent=best_lineup(slots, tp_)[0], me=r["roster_id"] == mine["roster_id"]))
        for key in ("week", "talent"):
            for i, x in enumerate(sorted(rows, key=lambda x: -x[key])):
                x[key + "_rank"] = i + 1
        print(f"| Owner | Record | Pts so far | Week {week} expected (rank) | Per-game talent (rank) |\n|---|---|---|---|---|")
        for x in sorted(rows, key=lambda x: x["talent_rank"]):
            o = f"**{x['owner']}**" if x["me"] else x["owner"]
            print(f"| {o} | {x['rec']} | {x['pf']} | {x['week']:.0f} ({x['week_rank']}) | {x['talent']:.0f} ({x['talent_rank']}) |")
        return

    # trade targets (top expected week elsewhere)
    trade = []
    for pid, rid in rostered.items():
        if rid == mine["roster_id"] or not team_of(pid) or P.get(pid, {}).get("injury_status") in ("Out", "IR"):
            continue
        r = player(pid)
        trade.append(dict(name=r["name"], team=r["team"], pos="/".join(r["pos"]), val=r["pass_value"], mu=r["mu"], owner=owner[rid], rec="%d-%d" % rec[rid]))
    trade.sort(key=lambda x: -x["mu"])

    opp = next((m for m in matchups if mm and m["matchup_id"] == mm.get("matchup_id") and m["roster_id"] != mine["roster_id"]), None)

    # injury rows (my roster + IR)
    inj_rows = []
    for r in list(me.values()) + ir:
        i = r["inj"]
        if i["sleeper"] or i["espn"] or i["override"]:
            e = i["espn"]
            inj_rows.append(dict(name=r["name"], team=r["team"], where="starter" if r["pid"] in starters else ("IR" if r in ir else "bench"),
                                 sleeper=f"{i['sleeper'] or '-'}{' (' + i['body'] + ')' if i['body'] else ''}",
                                 espn=f"{e['status']} {e['date']}: {e['comment']}" if e else "not in ESPN feed",
                                 override=(i["override"] or {}).get("note"), next_tip=r["next_tip"], left=len(r["games"]),
                                 dnp_next=r["dnps"][0] if r["dnps"] else None))

    # sanity check: our score vs Sleeper's for starters' latest games
    diffs = [(r["name"], r["score"], r["sleeper"]) for r in lock_rows if r.get("sleeper") is not None and abs(r["score"] - r["sleeper"]) > 0.5]
    for n_, a, b in diffs:
        WARN.append(f"{n_}: our score {a:.1f} vs Sleeper {b:.1f} (stat correction or scoring gap; trust Sleeper)")

    # ---- write outputs
    now_nz = now.astimezone(NZ)
    o = []
    o.append(f"# Jackal League brief: {now_nz.strftime('%a %d %b, %H:%M')} NZ (week {week}, Mon {wk_start} - Sun {wk_end} US; week closes {nz(week_close)} NZ)")
    o += ["", "## Do now"]
    o += [f"1. {t}" for _, t in actions[:8]] or ["Nothing urgent."]
    o += ["", "## Lock or pass (starters; only locks.json counts as locked)"]
    if lock_rows:
        o += ["| Player | Latest game | Sleeper | Per-game avg | Games left | Next tip (NZ) | Lock if at least | Call |", "|---|---|---|---|---|---|---|---|"]
        for r in lock_rows:
            th = f"{r['threshold']:.1f}" if r["threshold"] is not None else "-"
            o.append(f"| {r['name']} | {r['score']:.1f} | {r['sleeper'] if r['sleeper'] is not None else '-'} | {r['avg']:.1f} | {r['left']} | {r['next_tip'] or '-'} | {th} | **{r['call']}** ({r['why']}) |")
    else:
        o.append("No starter has a lockable game right now.")
    o += ["", f"## Lineup (open slots now worth {cur_total:.1f}; best {opt_total:.1f})",
          "| Slot | Player | Status | Games left | Value | Frozen |", "|---|---|---|---|---|---|"]
    for i, (s, pid) in enumerate(zip(slots, starters)):
        r = me.get(pid)
        if not r:
            o.append(f"| {s} | empty | | 0 | 0 | |")
            continue
        v = LOCKS[r["name"]]["score"] if pid in locked_pids else r["val"]
        o.append(f"| {s} | {r['name']} | {r['inj']['sleeper'] or ''} | {'-' if pid in locked_pids else len(r['games'])} | {v:.1f} | {'locked' if pid in locked_pids else ('live' if r['live'] else '')} |")
    if changes:
        tag = "Suggested" if opt_total - cur_total >= CFG["min_lineup_gain"] else f"Marginal (under {CFG['min_lineup_gain']:.0f} pts, optional)"
        o += ["", f"{tag}: " + "; ".join(f"start {c['put']} for {c['was']} (+{c['gain']:.1f})" for c in changes)]
        o.append("Slot layout after: " + ", ".join(moves))
    bench = [me[p] for p in me_ids if p not in starters]
    if bench:
        o += ["", "Bench: " + ", ".join(f"{b['name']} ({len(b['games'])} left, {b['pass_value']:.1f})" for b in sorted(bench, key=lambda b: -b["pass_value"]))]
    o += ["", "## Upgrades: free agents"]
    if ups:
        o += ["| Add | Pos | Team | Games left | Week value | Per game | Drop | Gain | Claim |", "|---|---|---|---|---|---|---|---|---|"]
        for u in ups[:6]:
            o.append(f"| {u['add']} | {u['pos']} | {u['team']} | {u['left']} | {u['val']:.1f} | {u['mu']:.1f} | {u['drop']} | +{u['gain']:.1f} | {'waiver (team played today)' if u['waiver'] else 'free'} |")
    else:
        o.append("No free agent beats a current slot by more than half a point.")
    o += ["", "## Trade targets (best per-game on other rosters)", "| Player | Pos | Team | Owner (record) | Per game | This week |", "|---|---|---|---|---|---|"]
    for t in trade[:8]:
        o.append(f"| {t['name']} | {t['pos']} | {t['team']} | {t['owner']} ({t['rec']}) | {t['mu']:.1f} | {t['val']:.1f} |")
    o += ["", "## Injuries (my roster)"]
    for r in inj_rows or []:
        dn = f"; miss chance next game {r['dnp_next']:.0%}" if r["dnp_next"] is not None else ""
        ov = f"; override: {r['override']}" if r["override"] else ""
        o.append(f"- {r['name']} ({r['team']}, {r['where']}): Sleeper {r['sleeper']}; ESPN {r['espn']}{dn}; next tip {r['next_tip'] or '-'} NZ, {r['left']} left{ov}")
    if not inj_rows:
        o.append("- None flagged.")
    o += ["", "## Matchup"]
    if mm and opp:
        o.append(f"Me {mm.get('points', 0):.1f} vs {owner[opp['roster_id']]} {opp.get('points', 0):.1f}")
    if wp and wp.get("missing"):
        o += ["", f"## Win probability vs {wp['opp']}: off this week",
              f"Needs {wp['opp']}'s locked total. Sleeper's matchup points ({wp['sleeper_pts']:.1f}) add up each of his starters' latest game, "
              "not what he locked, so they can't be used.",
              f"Add `\"opponent_total\": {{\"{wk_start}\": <his total>}}` to settings.json; until then the lock calls use the points rule."]
    elif wp:
        o += ["", f"## Win probability vs {wp['opp']} (to beat {wp['opp_total']:.1f}; source: {wp['source']})",
              f"Banked {wp['fixed']:.1f} in locked slots; {len(wp['calls']) + wp['skipped']} open starters simulated, {wp['n']} weeks.",
              f"- Points rule calls: {wp['p_ev']:.1%}",
              f"- Best calls ({'in use' if CFG.get('objective') == 'win' else 'shown only; objective is points'}): {wp['p_best']:.1%}",
              f"- Lock everything lockable now: {wp['p_all']:.1%}"]
        if wp["calls"]:
            o += ["", "| Player | Points rule | Win-chance call |", "|---|---|---|"]
            for n_, c in wp["calls"].items():
                o.append(f"| {n_} | {'LOCK' if wp['ev'][n_] else 'PASS'} | **{'LOCK' if c else 'PASS'}**{'' if c == wp['ev'][n_] else ' (differs)'} |")
        o.append("- Uses your current starters; lineup swaps above are not simulated.")
    if WARN:
        o += ["", "## Data warnings"] + [f"- {w}" for w in WARN]
    o += ["", f"_Model: per-game mean blends last season's FP/game (weight {CFG['prior_weight_games']} games) with this season's games; "
               "lock threshold = value of passing under optimal stopping, with each game's miss chance; an unlocked starter scores 0 at week's end. "
               "Scoring read from the league._"]
    text = "\n".join(o)
    open(os.path.join(args.out, "brief.md"), "w").write(text)
    json.dump(dict(week=week, now=now.isoformat(), actions=[a for _, a in actions], lock=lock_rows, changes=changes,
                   upgrades=ups[:8], trade=trade[:10], injuries=inj_rows, warnings=WARN),
              open(os.path.join(args.out, "brief.json"), "w"), indent=1, default=str)
    print(text)


if __name__ == "__main__":
    main()
