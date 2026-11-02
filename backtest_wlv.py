"""WLV backtest (PRD-v4). Separate from brief.py; reads cached 2025 weekly logs + 2024 totals.

Predictors, each built only from data before week N:
  A  FP/g (brief.py blend of last season and this season so far)
  B  FP/g x team games that week
  C  weekly lock value: brief.py pass_value(mu, sd, [default miss chance] * games)
Target: best achievable locked score = the player's best single game that week (0 if he did not play).
Realized (reported separately): his average score over the games he actually played (0 if none).
Headline = weeks 5+; weeks 1-4 = appendix. Rule fixed in the PRD: C must beat B by >= 0.03 pooled rank
correlation, and beat A.
"""
import sys, os, json, math, random, collections
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import brief as B

CACHE = os.environ.get("BT_CACHE") or os.path.join(HERE, "cache", "backtest")
CFG = B.CFG
fp = B.make_fp(json.load(open(f"{CACHE}/scoring.json")))
K, MIN_SD, SD_RATIO = CFG["prior_weight_games"], CFG["min_sd"], CFG["sd_ratio"]
P_DNP = CFG["dnp"]["default"]
MARGIN = 0.03
POOL_MIN_MU = 10.0
HEAD_FROM = 5

# ---------- load ----------
prior = {}
for r in json.load(open(f"{CACHE}/season_2024.json")):
    st = r.get("stats") or {}
    gp = st.get("gp") or 0
    if gp >= CFG["prior_min_gp"]:
        prior[r["player_id"]] = fp(st) / gp
weeks = {}
for w in range(1, 40):
    p = f"{CACHE}/w2025_{w:02d}.json"
    if not os.path.exists(p):
        break
    games = []
    for r in json.load(open(p)):
        st = r.get("stats") or {}
        # 2025 rows carry seconds played in "sp" (no "min"); team-total rows have neither
        if r.get("date") and st and ((st.get("min") or 0) > 0 or (st.get("sp") or 0) > 0) and str(r["player_id"]).isdigit():
            games.append(dict(pid=r["player_id"], team=r.get("team"), date=r["date"], fp=fp(st)))
    if games:
        weeks[w] = games
NAMES = B.cached("players.json", lambda: B.http(B.SLEEPER + "players/nba"), 24 * 30)
nm = lambda pid: (NAMES.get(pid, {}).get("first_name", "") + " " + NAMES.get(pid, {}).get("last_name", "")).strip() or pid


# ---------- model pieces (same formulas as brief.py) ----------
def mean_sd(pid, xs):
    pm = prior.get(pid, CFG["baseline_fp"])
    mu = (K * pm + sum(xs)) / (K + len(xs))
    sd0 = max(MIN_SD, SD_RATIO * pm)
    ss = K * sd0 ** 2 + sum((x - mu) ** 2 for x in xs)
    return mu, max(MIN_SD, math.sqrt(ss / (K + len(xs))))


def ranks(v):
    idx = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
            j += 1
        for t in range(i, j + 1):
            r[idx[t]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    if len(x) < 3:
        return float("nan")
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")


# ---------- build player-weeks ----------
hist = collections.defaultdict(list)  # pid -> scores before this week
last_team = {}
rows = []  # one per player-week in the pool
for w in sorted(weeks):
    games = weeks[w]
    team_dates = collections.defaultdict(set)
    for g in games:
        team_dates[g["team"]].add(g["date"])
    by_pid = collections.defaultdict(list)
    for g in games:
        by_pid[g["pid"]].append(g)
    known = set(hist) | set(prior)
    for pid in known:
        team = last_team.get(pid)
        if team is None and pid in by_pid:
            team = by_pid[pid][0]["team"]
        ng = len(team_dates.get(team, ()))
        if ng == 0:
            continue
        mu, sd = mean_sd(pid, hist[pid])
        if mu < POOL_MIN_MU:
            continue
        played = [g["fp"] for g in by_pid.get(pid, [])]
        rows.append(dict(w=w, pid=pid, g=ng, mu=mu, sd=sd, n_hist=len(hist[pid]),
                         A=mu, B=mu * ng, C=B.pass_value(mu, sd, [P_DNP] * ng),
                         best=max(played) if played else 0.0,
                         real=sum(played) / len(played) if played else 0.0,
                         gp=len(played)))
    for pid, gs in by_pid.items():  # update history AFTER predicting the week
        hist[pid].extend(g["fp"] for g in sorted(gs, key=lambda x: x["date"]))
        last_team[pid] = sorted(gs, key=lambda x: x["date"])[-1]["team"]

# ---------- selftest ----------
def selftest():
    # 1. scoring: a 20-pt, 8-reb, 5-ast, 2-stl, 1-blk, 3-3PM, 3-TO double-double-less line
    line = dict(pts=20, reb=8, ast=5, stl=2, blk=1, tpm=3, to=3)
    want = 0.5 * 20 + 8 + 5 + 2 * 2 + 2 * 1 + 0.5 * 3 - 3
    assert abs(fp(line) - want) < 1e-9, (fp(line), want)
    # 2. one game, 5% miss chance: value is 0.95 * E[max(X,0)], about 0.95 * mu for mu >> sd
    assert abs(B.pass_value(30, 5, [0.05]) - 0.95 * 30) < 0.05
    # 3. more games never lower the weekly value; C is below the best-game ceiling mu + a few sd
    a, b, c = (B.pass_value(20, 7, [0.05] * k) for k in (1, 2, 3))
    assert a < b < c < 20 + 3 * 7
    # 4. no leakage: week-1 predictions use no 2025 games
    assert all(r["n_hist"] == 0 for r in rows if r["w"] == 1)
    # 5. the history length grows week by week for a regular player
    pid = max((r for r in rows if r["w"] == 10), key=lambda r: r["n_hist"])["pid"]
    assert max(r["n_hist"] for r in rows if r["pid"] == pid and r["w"] == 10) > max(r["n_hist"] for r in rows if r["pid"] == pid and r["w"] == 5)
    print("selftest: OK (scoring, one-game value, monotone in games, no week-1 leakage, history grows)")


selftest()

# ---------- hand-check a few player-weeks ----------
print("\nHand-check (three player-weeks):")
for w, pick in ((12, 0), (18, 1), (22, 2)):
    cand = sorted([r for r in rows if r["w"] == w and r["gp"] >= 2], key=lambda r: -r["mu"])[:6]
    r = cand[pick]
    scores = [round(g["fp"], 1) for g in sorted(weeks[w], key=lambda x: x["date"]) if g["pid"] == r["pid"]]
    print(f"  wk{w} {nm(r['pid']):<22} hist n={r['n_hist']} mu={r['mu']:.1f} sd={r['sd']:.1f} team games={r['g']} "
          f"| A={r['A']:.1f} B={r['B']:.1f} C={r['C']:.1f} | games played {scores} best={r['best']:.1f} realized avg={r['real']:.1f}")

# ---------- evaluation ----------
PRED = ("A", "B", "C")


def evaluate(sel, label, target):
    rs = [r for r in rows if sel(r)]
    out = {"n": len(rs)}
    y = [r[target] for r in rs]
    for p in PRED:
        out["rho_" + p] = spearman([r[p] for r in rs], y)
    # per-week rank correlation
    per_week = {p: [] for p in PRED}
    wk_list = sorted({r["w"] for r in rs})
    for w in wk_list:
        wr = [r for r in rs if r["w"] == w]
        for p in PRED:
            per_week[p].append(spearman([r[p] for r in wr], [r[target] for r in wr]))
    out["wk_mean"] = {p: sum(per_week[p]) / len(per_week[p]) for p in PRED}
    out["per_week"] = per_week
    out["weeks"] = wk_list
    # MAE: raw for A and C (same units as the score), and after a straight-line fit for all three
    for p in PRED:
        xs = [r[p] for r in rs]
        mx, my = sum(xs) / len(xs), sum(y) / len(y)
        b = sum((x - mx) * (t - my) for x, t in zip(xs, y)) / sum((x - mx) ** 2 for x in xs)
        a = my - b * mx
        out["mae_fit_" + p] = sum(abs(a + b * x - t) for x, t in zip(xs, y)) / len(xs)
        if p != "B":
            out["mae_raw_" + p] = sum(abs(x - t) for x, t in zip(xs, y)) / len(xs)
    # top 12 per week, whole pool
    top = {p: {"best": 0.0, "real": 0.0} for p in PRED}
    per_week_top = {p: [] for p in PRED}
    for w in wk_list:
        wr = [r for r in rs if r["w"] == w]
        for p in PRED:
            t12 = sorted(wr, key=lambda r: -r[p])[:12]
            top[p]["best"] += sum(r["best"] for r in t12)
            top[p]["real"] += sum(r["real"] for r in t12)
            per_week_top[p].append(sum(r[target] for r in t12))
    out["top12"] = {p: {k: v / len(wk_list) for k, v in top[p].items()} for p in PRED}
    out["top12_per_week"] = per_week_top
    return out


def bootstrap_diff(a, b, n=4000, seed=11):
    rnd = random.Random(seed)
    d = [x - y for x, y in zip(a, b)]
    ms = sorted(sum(rnd.choice(d) for _ in d) / len(d) for _ in range(n))
    return sum(d) / len(d), ms[int(0.025 * n)], ms[int(0.975 * n)]


head = evaluate(lambda r: r["w"] >= HEAD_FROM, "headline", "best")
early = evaluate(lambda r: r["w"] < HEAD_FROM, "early", "best")
first = evaluate(lambda r: HEAD_FROM <= r["w"] <= 14, "first half", "best")
second = evaluate(lambda r: r["w"] >= 15, "second half", "best")
real = evaluate(lambda r: r["w"] >= HEAD_FROM, "realized", "real")


def show(tag, e):
    print(f"\n== {tag}: {e['n']} player-weeks, weeks {e['weeks'][0]}-{e['weeks'][-1]} ==")
    print("  pooled rank corr  A %.3f  B %.3f  C %.3f | C-B %+.3f  C-A %+.3f" % (e["rho_A"], e["rho_B"], e["rho_C"], e["rho_C"] - e["rho_B"], e["rho_C"] - e["rho_A"]))
    print("  mean weekly corr  A %.3f  B %.3f  C %.3f" % tuple(e["wk_mean"][p] for p in PRED))


show("HEADLINE (target = best achievable locked score)", head)
d, lo, hi = bootstrap_diff(head["per_week"]["C"], head["per_week"]["B"])
print(f"  weekly corr C-B: mean {d:+.3f}, 95% interval over weeks [{lo:+.3f}, {hi:+.3f}]")
d2, lo2, hi2 = bootstrap_diff(head["per_week"]["C"], head["per_week"]["A"])
print(f"  weekly corr C-A: mean {d2:+.3f}, 95% interval over weeks [{lo2:+.3f}, {hi2:+.3f}]")
wins = sum(1 for c, b in zip(head["per_week"]["C"], head["per_week"]["B"]) if c > b)
winsA = sum(1 for c, a in zip(head["per_week"]["C"], head["per_week"]["A"]) if c > a)
print(f"  weeks where C beats B: {wins}/{len(head['weeks'])}; C beats A: {winsA}/{len(head['weeks'])}")
print("  MAE vs target  raw: A %.2f  C %.2f | after straight-line fit: A %.2f  B %.2f  C %.2f" % (head["mae_raw_A"], head["mae_raw_C"], head["mae_fit_A"], head["mae_fit_B"], head["mae_fit_C"]))
print("  top 12 per week (avg per week), sum of best-achievable / realized:")
for p in PRED:
    print(f"     {p}: best {head['top12'][p]['best']:.1f}   realized {head['top12'][p]['real']:.1f}")
show("first half (wks 5-14)", first)
show("second half (wks 15-25)", second)
show("APPENDIX weeks 1-4", early)
print("\nRealized-score view (target = his average over games played), weeks 5+:")
show("realized", real)
print("  top 12 per week, realized: " + "  ".join(f"{p} {real['top12'][p]['real']:.1f}" for p in PRED))

# ---------- decision against the PRD rule ----------
margin_ok = head["rho_C"] - head["rho_B"] >= MARGIN
beats_a = head["rho_C"] > head["rho_A"]
print("\nPRD rule: C-B >= %.2f? %s | C > A? %s  ->  %s" % (MARGIN, margin_ok, beats_a,
      "C WINS: weekly lock value earns its keep" if margin_ok and beats_a else "C DOES NOT clear the bar: adopt B (FP/g x games)" if not margin_ok else "inconclusive"))

# ---------- biggest misses (headline, after straight-line fit) ----------
def misses(p, k=5):
    rs = [r for r in rows if r["w"] >= HEAD_FROM]
    xs, y = [r[p] for r in rs], [r["best"] for r in rs]
    mx, my = sum(xs) / len(xs), sum(y) / len(y)
    b = sum((x - mx) * (t - my) for x, t in zip(xs, y)) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    res = sorted(((r, a + b * r[p] - r["best"]) for r in rs), key=lambda z: -abs(z[1]))[:k]
    return [f"wk{r['w']} {nm(r['pid'])}: predicted {a + b * r[p]:.0f}, best game {r['best']:.0f} (played {r['gp']} of {r['g']})" for r, e in res]

for p in PRED:
    print(f"\nBiggest misses, predictor {p}:")
    for line in misses(p):
        print("   ", line)

json.dump({"headline": {k: v for k, v in head.items() if k not in ("per_week", "top12_per_week")},
           "early": {k: v for k, v in early.items() if k not in ("per_week", "top12_per_week")},
           "first": {k: v for k, v in first.items() if k not in ("per_week", "top12_per_week")},
           "second": {k: v for k, v in second.items() if k not in ("per_week", "top12_per_week")},
           "real": {k: v for k, v in real.items() if k not in ("per_week", "top12_per_week")}},
          open(CACHE + "/results.json", "w"), indent=1)
