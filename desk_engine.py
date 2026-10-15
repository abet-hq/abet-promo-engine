#!/usr/bin/env python3
# desk_engine.py - ABET SPORTS DESK data engine (v12-spec restore, cloud-only, stdlib only).
# Sources (all keyless, per 20261010 log): ESPN scoreboard/news, MLB statsapi (highlights pool +
# season profiles), Reddit new.json (breaking). Anti-repeat via token Jaccard vs history.
# Output: desk_state.json (ticker, breaking, games+win%, highlights, season_profile, script_hint)
#         desk_history.json (seen ids, recent topics) - both committed by the workflow.
import json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "abet-desk/1.0 (cloud desk engine)"}
HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(HERE, "desk_history.json")
OUT = os.path.join(HERE, "desk_state.json")
BREAKING_MIN = 45          # v3: hot item under 45 min old
ANTI_REPEAT_J = 0.45       # v6 tightened threshold
ANTI_REPEAT_WINDOW = 8     # v6 window

def log(*a): print("[desk]", *a, flush=True)

def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def load_history():
    try:
        h = json.load(open(HISTORY, encoding="utf-8"))
        for k in ("seen_news_ids", "recent_topics", "used_highlight_pks"):
            h.setdefault(k, [])
        return h
    except Exception:
        return {"seen_news_ids": [], "recent_topics": [], "used_highlight_pks": []}

def tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))

def jaccard(a, b):
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)

def too_similar(title, recent_topics):
    t = tokens(title)
    return any(jaccard(t, set(x)) > ANTI_REPEAT_J for x in recent_topics[-ANTI_REPEAT_WINDOW:])

# ---------------- ESPN ----------------
LEAGUES = [("football", "nfl"), ("basketball", "nba"), ("baseball", "mlb")]

def fetch_scores():
    games = []
    for sport, league in LEAGUES:
        try:
            d = get_json(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard")
        except Exception as e:
            log("espn scoreboard fail", league, e); continue
        for ev in d.get("events", [])[:6]:
            try:
                comp = ev["competitions"][0]
                teams = {c["homeAway"]: c for c in comp["competitors"]}
                home, away = teams.get("home"), teams.get("away")
                if not home or not away: continue
                status = comp.get("status", {}).get("type", {}).get("shortDetail", ev.get("status", {}).get("type", {}).get("shortDetail", ""))
                row = {
                    "league": league.upper(),
                    "name": ev.get("shortName", ev.get("name", "")),
                    "away": away["team"].get("abbreviation", "?"), "away_score": away.get("score", ""),
                    "home": home["team"].get("abbreviation", "?"), "home_score": home.get("score", ""),
                    "status": status, "date": ev.get("date", ""),
                }
                # MODEL implied win% from moneylines when ESPN exposes them (v12)
                odds = (comp.get("odds") or [{}])[0]
                ml_a = (odds.get("awayTeamOdds") or {}).get("moneyLine")
                ml_h = (odds.get("homeTeamOdds") or {}).get("moneyLine")
                if isinstance(ml_a, (int, float)) and isinstance(ml_h, (int, float)):
                    pa = (100 / (ml_a + 100)) if ml_a > 0 else ((-ml_a) / (-ml_a + 100))
                    ph = (100 / (ml_h + 100)) if ml_h > 0 else ((-ml_h) / (-ml_h + 100))
                    s = pa + ph
                    if s > 0:
                        row["model_home_pct"] = round(100 * ph / s)
                        row["model_away_pct"] = round(100 * pa / s)
                games.append(row)
            except Exception as e:
                log("espn event parse fail", league, e)
    return games

def fetch_news_breaking(history):
    """ESPN news + Reddit new.json. Breaking = <45min. Returns (breaking|None, seen_ids)."""
    cands = []
    for sport, league in LEAGUES:
        try:
            d = get_json(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/news")
            for a in d.get("articles", [])[:15]:
                pub = a.get("published", "")
                try:
                    ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except Exception:
                    continue
                cands.append({"id": "espn:" + str(a.get("id") or a.get("headline", ""))[:60],
                              "title": a.get("headline", ""), "source": f"ESPN {league.upper()}",
                              "age_min": (datetime.now(timezone.utc) - ts).total_seconds() / 60})
        except Exception as e:
            log("espn news fail", league, e)
    for sub in ("nba", "nfl", "sportsbook", "baseball", "hockey"):
        try:
            d = get_json(f"https://www.reddit.com/r/{sub}/new.json?limit=15")
            for c in d.get("data", {}).get("children", []):
                p = c.get("data", {})
                if p.get("stickied"): continue
                ts = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
                cands.append({"id": "rd:" + str(p.get("id", "")), "title": p.get("title", ""),
                              "source": f"r/{sub}", "age_min": (datetime.now(timezone.utc) - ts).total_seconds() / 60,
                              "score": p.get("score", 0)})
        except Exception as e:
            log("reddit fail", sub, e)
    fresh = [c for c in cands if c["age_min"] < BREAKING_MIN and c["id"] not in set(history["seen_news_ids"])]
    fresh.sort(key=lambda c: (c["age_min"] - 0.001 * c.get("score", 0)))
    for c in cands:
        if c["id"] not in history["seen_news_ids"]:
            history["seen_news_ids"].append(c["id"])
    history["seen_news_ids"] = history["seen_news_ids"][-400:]
    return (fresh[0] if fresh else None)

# ---------------- MLB highlights pool (v6/v12) ----------------
def fetch_highlights_pool(history):
    """Up to 3 games' latest highlight MP4s (darkroom-clips.mlb.com), round-robin cursor."""
    out = []
    try:
        today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-4)))  # ET-ish
        for day_off in (0, -1):
            date = (today + timedelta(days=day_off)).strftime("%Y-%m-%d")
            d = get_json(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}")
            pks = []
            for dt in d.get("dates", []):
                for g in dt.get("games", []):
                    if g.get("status", {}).get("abstractGameState") in ("Final", "Live", "Preview"):
                        pks.append(g.get("gamePk"))
            if pks:
                log("mlb games", date, len(pks)); break
        # round-robin: rotate order by history cursor
        used = history.get("used_highlight_pks", [])
        ordered = [p for p in pks if p not in used] + [p for p in pks if p in used]
        for pk in ordered[:3]:
            try:
                c = get_json(f"https://statsapi.mlb.com/api/v1/game/{pk}/content")
                items = (c.get("highlights", {}) or {}).get("highlights", {}).get("items", [])
                if not items: continue
                it = items[0]
                pb = it.get("playbacks", [])
                mp4 = None
                for p in pb:
                    if p.get("name") in ("mp4Avc", "2500K", "1800K", "1200K") and p.get("url", "").endswith(".mp4"):
                        mp4 = p["url"]; break
                if not mp4:
                    for p in pb:
                        if p.get("url", "").endswith(".mp4"): mp4 = p["url"]; break
                if mp4:
                    out.append({"gamePk": pk, "title": it.get("headline", it.get("title", "MLB highlight")), "url": mp4})
                    if pk in used: used.remove(pk)
                    used.append(pk)
            except Exception as e:
                log("mlb content fail", pk, e)
        history["used_highlight_pks"] = used[-10:]
    except Exception as e:
        log("mlb pool fail", e)
    return out

def fetch_season_profile():
    """v12 radar: real season batting line from a recent boxscore."""
    try:
        d = get_json("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=" +
                     (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
        pk = None
        for dt in d.get("dates", []):
            for g in dt.get("games", []):
                if g.get("status", {}).get("abstractGameState") == "Final":
                    pk = g.get("gamePk"); break
            if pk: break
        if not pk: return None
        b = get_json(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        for side in ("home", "away"):
            team = b["teams"][side]
            for pid, p in team.get("players", {}).items():
                bat = (p.get("seasonStats", {}) or {}).get("batting", {})
                if bat.get("atBats", 0) and bat.get("avg"):
                    return {"player": p.get("person", {}).get("fullName", "?"),
                            "team": team.get("team", {}).get("abbreviation", team.get("team", {}).get("name", "?")),
                            "avg": bat.get("avg"), "hr": bat.get("homeRuns"), "rbi": bat.get("rbi"),
                            "hits": bat.get("hits"), "ops": bat.get("ops"), "gamePk": pk}
    except Exception as e:
        log("season profile fail", e)
    return None

# ---------------- rundown ----------------
def pick_script_hint(games, breaking, highlights, profile, history):
    """Rotating desk line (the anti-repeat brain's output slot)."""
    pool = []
    if breaking:
        pool.append(f"BREAKING - {breaking['title']} - per {breaking['source']}.")
    live = [g for g in games if "Final" not in g["status"] and g["status"]]
    final = [g for g in games if "Final" in g["status"]]
    for g in live[:2]:
        ml = ""
        if g.get("model_home_pct"):
            ml = f" The model prices {g['home']} at {g['model_home_pct']} percent."
        pool.append(f"{g['league']} - {g['away']} {g['away_score']}, {g['home']} {g['home_score']} - {g['status']}.{ml}")
    for g in final[:2]:
        pool.append(f"Final - {g['league']} - {g['away']} {g['away_score']}, {g['home']} {g['home_score']}.")
    if highlights:
        pool.append(f"Highlight coming - {highlights[0]['title']}.")
    if profile:
        pool.append(f"{profile['player']} is hitting {profile['avg']} with {profile['hr']} homers and {profile['rbi']} RBI.")
    if not pool:
        pool.append("Board is quiet. The desk is watching the wire.")
    for line in pool:
        if not too_similar(line, history["recent_topics"]):
            history["recent_topics"].append((line or "")[:160])
            history["recent_topics"] = history["recent_topics"][-ANTI_REPEAT_WINDOW:]
            return line
    return pool[0]

def main():
    history = load_history()
    games = fetch_scores()
    breaking = fetch_news_breaking(history)
    highlights = fetch_highlights_pool(history)
    profile = fetch_season_profile()
    hint = pick_script_hint(games, breaking, highlights, profile, history)

    tick_parts = []
    for g in games[:5]:
        if g["away_score"] != "":
            tick_parts.append(f"{g['league']}: {g['away']} {g['away_score']} - {g['home']} {g['home_score']} {g['status']}")
    ticker = "  //  ".join(tick_parts) if tick_parts else "ABET SPORTS DESK - LIVE"

    state = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "version": 1,
        "ticker": ticker,
        "breaking": breaking,
        "games": games,
        "highlights": highlights,
        "season_profile": profile,
        "script_hint": hint,
    }
    json.dump(state, open(OUT, "w", encoding="utf-8"), indent=1)
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), indent=1)
    log("wrote", OUT)
    log("breaking:", breaking["title"] if breaking else None)
    log("games:", len(games), "highlights:", len(highlights), "profile:", bool(profile))
    log("script_hint:", hint)

if __name__ == "__main__":
    main()
