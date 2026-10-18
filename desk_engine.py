#!/usr/bin/env python3
# desk_engine.py - ABET SPORTS DESK data engine (v12-spec restore, cloud-only, stdlib only).
# Sources (all keyless, per 20261010 log): ESPN scoreboard/news, MLB statsapi (highlights pool +
# season profiles), Reddit new.json (breaking). Anti-repeat via token Jaccard vs history.
# Output: desk_state.json (ticker, breaking, games+win%, highlights, season_profile, script_hint)
#         desk_history.json (seen ids, recent topics) - both committed by the workflow.
import json, os, re, sys, time, urllib.request, urllib.error, urllib.parse
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
    reddit_ok = False
    for sub in ("nba", "nfl", "sportsbook", "baseball", "hockey"):
        try:
            d = get_json(f"https://www.reddit.com/r/{sub}/new.json?limit=15")
            reddit_ok = True
            for c in d.get("data", {}).get("children", []):
                p = c.get("data", {})
                if p.get("stickied"): continue
                ts = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
                cands.append({"id": "rd:" + str(p.get("id", "")), "title": p.get("title", ""),
                              "source": f"r/{sub}", "age_min": (datetime.now(timezone.utc) - ts).total_seconds() / 60,
                              "score": p.get("score", 0)})
        except Exception as e:
            log("reddit fail", sub, e)
    # Google News RSS (free, no auth, fallback for Reddit 403)
    try:
        import xml.etree.ElementTree as ET
        url = "https://news.google.com/rss/search?q=sports+NBA+NFL+MLB&hl=en-US&gl=US&ceid=US:en"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            tree = ET.fromstring(r.read())
        for item in tree.findall(".//item")[:10]:
            title = item.findtext("title", "")
            if title:
                cands.append({"id": "gnews:" + title[:40], "title": title,
                              "source": "Google News", "age_min": 30, "score": 0})
        reddit_ok = True
        log("google news rss: ok")
    except Exception as e:
        log("google news rss fail:", e)
    fresh = [c for c in cands if c["age_min"] < BREAKING_MIN and c["id"] not in set(history["seen_news_ids"])]
    fresh.sort(key=lambda c: (c["age_min"] - 0.001 * c.get("score", 0)))
    for c in cands:
        if c["id"] not in history["seen_news_ids"]:
            history["seen_news_ids"].append(c["id"])
    history["seen_news_ids"] = history["seen_news_ids"][-400:]
    return (fresh[0] if fresh else None, reddit_ok)

# ---------------- MLB highlights pool (v6/v12) ----------------
def fetch_highlights_pool(history):
    """Up to 3 games' latest highlight MP4s (darkroom-clips.mlb.com), round-robin cursor."""
    out = []
    try:
        today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-4)))  # ET-ish
        for day_off in (0, -1):
            date = (today + timedelta(days=day_off)).strftime("%Y-%m-%d")
            d = get_json(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}")
            pk_final, pk_preview = [], []
            for dt in d.get("dates", []):
                for g in dt.get("games", []):
                    st = g.get("status", {}).get("abstractGameState")
                    if st in ("Final", "Live"): pk_final.append(g.get("gamePk"))
                    elif st == "Preview": pk_preview.append(g.get("gamePk"))
            pks = pk_final or pk_preview
            if pks:
                log("mlb games", date, len(pks), "final/live" if pk_final else "preview"); break
        # round-robin: rotate order by history cursor
        used = history.get("used_highlight_pks", [])
        ordered = [p for p in pks if p not in used] + [p for p in pks if p in used]
        NOT_A_CLIP = re.compile(r"bench|availability|lineup|probable|pregame|injur|weather|roster|transaction|interview|press conference|alignment|warmup|first pitch|national anthem|lineup card|postponed|discusses|talks|reacts|previews", re.I)
        for pk in ordered[:3]:
            try:
                c = get_json(f"https://statsapi.mlb.com/api/v1/game/{pk}/content")
                items = (c.get("highlights", {}) or {}).get("highlights", {}).get("items", [])
                it = None
                for cand_it in items[:8]:
                    title_c = cand_it.get("headline", cand_it.get("title", ""))
                    if title_c and not NOT_A_CLIP.search(title_c):
                        it = cand_it; break
                if not it: continue
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

def fetch_season_profile(prefer_pk=None):
    """v12 radar, correctness round: TRUE season stats via people endpoint, sanity-gated.
    Player = most at-bats in the game (not dict order). Prefers the highlighted game."""
    try:
        pk = prefer_pk
        if not pk:
            d = get_json("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=" +
                         (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
            for dt in d.get("dates", []):
                for g in dt.get("games", []):
                    if g.get("status", {}).get("abstractGameState") == "Final":
                        pk = g.get("gamePk"); break
                if pk: break
        if not pk: return None
        b = get_json(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        cand = None
        for side in ("home", "away"):
            team = b["teams"][side]
            for pid, p in (team.get("players", {}) or {}).items():
                bat = ((p.get("stats", {}) or {}).get("batting", {}) or {})
                ab = int(bat.get("atBats", 0) or 0)
                pid_num = (p.get("person", {}) or {}).get("id")
                if ab and pid_num and (cand is None or ab > cand[1]):
                    cand = (pid_num, ab, p.get("person", {}).get("fullName", "?"),
                            (team.get("team", {}) or {}).get("abbreviation", "?"))
        if not cand: return None
        pid_num, ab, name, team = cand
        season = datetime.now(timezone.utc).year
        s = get_json(f"https://statsapi.mlb.com/api/v1/people/{pid_num}/stats?stats=season&group=hitting&season={season}")
        st = None
        for blk in s.get("stats", []):
            for sp in blk.get("splits", []):
                st = sp.get("stat", {}) or None
                if st: break
            if st: break
        if not st: return None
        avg = st.get("avg")
        try: avg_f = float(avg)
        except Exception: return None
        if not (0.150 <= avg_f <= 0.400):        # sanity gate: junk never reaches air
            log("season profile REJECTED by sanity gate:", name, avg); return None
        return {"player": name, "team": team, "avg": avg, "hr": st.get("homeRuns"),
                "rbi": st.get("rbi"), "hits": st.get("hits"), "ops": st.get("ops"),
                "gamePk": pk, "source": "statsapi /people/season"}
    except Exception as e:
        log("season profile fail", e)
        return None

# ---------------- ABET line moves (documented board-data publish + receiver-proxy fallback) ----------------
def fetch_abet_lines():
    """Line moves: board-data steam_events (production publish) -> receiver-proxy board (dispersion)."""
    out = []
    try:
        d = get_json("https://abet-board-data.abetco.workers.dev/data/steam_events.json", timeout=25)
        gen = d.get("generated_utc", "")
        age = None
        try:
            ts = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            age = round((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        except Exception:
            pass
        evs = sorted(d.get("events", []), key=lambda e: -(e.get("avg_pp") or 0))
        for e in evs[:6]:
            mv = (e.get("movers") or [{}])[0]
            try:
                f, t = int(mv.get("from")), int(mv.get("to"))
                if abs(t - f) > 250:      # junk/merged rows guard
                    continue
            except Exception:
                pass
            note = (f"{e.get('selection')} {e.get('bet_type','')}: {mv.get('from')} to {mv.get('to')} "
                    f"({e.get('n_books')} books, {e.get('avg_pp')}pp, {e.get('direction','')})").replace(" :", ":")
            if age is not None and age > 120:
                note += f" [events {round(age/60)}h old]"
            out.append({"event": e.get("event", "?"), "note": note.strip(),
                        "pp": e.get("avg_pp"), "books": e.get("n_books"), "data_age_min": age})
            if len(out) >= 3:
                break
    except Exception as ex:
        log("abet steam fetch fail", ex)
    if out:
        log("abet steam lines:", len(out), "top pp:", out[0]["pp"])
        return out[:3]
    # fallback: receiver-proxy board cross-book dispersion
    try:
        d = get_json("https://abet-receiver-proxy.abetco.workers.dev/board?sport=NFL", timeout=25)
    except Exception as e:
        log("abet board fail (no line watch this run)", e); return []
    rows = d.get("rows", []) or []
    games = {}
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.get("american_odds") is None: continue
        if r.get("is_live"): continue
        key = r.get("canonical_id") or r.get("id")
        sel = (r.get("selection") or "").upper()
        g = games.setdefault(key, {"event": r.get("participants") or key, "sel": {}, "newest": None})
        book = (r.get("bookmaker") or "?").lower()
        try: od = int(r["american_odds"])
        except Exception: continue
        g["sel"].setdefault(sel, {})[book] = od
        try:
            ts = datetime.fromisoformat((r.get("updated_at") or "").replace("Z", "+00:00"))
            if g["newest"] is None or ts > g["newest"]: g["newest"] = ts
        except Exception:
            pass
    def imp(ml):
        return (100 / (ml + 100)) if ml > 0 else ((-ml) / (-ml + 100))
    for key, g in games.items():
        best_sel = None
        for sel, books in g["sel"].items():
            if len(books) < 2: continue
            bk_best = min(books.items(), key=lambda kv: imp(kv[1]))
            bk_worst = max(books.items(), key=lambda kv: imp(kv[1]))
            pp = round((imp(bk_worst[1]) - imp(bk_best[1])) * 100, 1)
            if pp >= 0.5 and (best_sel is None or pp > best_sel[0]):
                best_sel = (pp, sel, bk_best, bk_worst, len(books))
        if best_sel:
            pp, sel, best, worst, nb = best_sel
            age = round((now - g["newest"]).total_seconds() / 60) if g["newest"] else None
            note = f"{sel}: {best[1]:+d} @ {best[0]} vs {worst[1]:+d} @ {worst[0]} ({pp}pp, {nb} books)"
            if age is not None and age > 1440:
                note += f" [board data {round(age/60)}h old]"
            out.append({"event": g["event"], "note": note, "pp": pp, "books": nb, "data_age_min": age})
    out.sort(key=lambda x: -x["pp"])
    log("abet board lines:", len(out), "top pp:", out[0]["pp"] if out else None)
    return out[:3]

# ---------------- chat monitor (free, no auth; polls public chat) ----------------
def fetch_chat_lines(channel=None, limit=5):
    """Poll public chat. Returns list of recent message strings. Free, no auth."""
    out = []
    if not channel:
        channel = os.environ.get("CHAT_CHANNEL", "")
    if not channel:
        return out
    try:
        d = get_json("https://tmi.twitch.tv/group/user/" + channel + "/chatters", timeout=15)
        v = d.get("chatters", {}).get("viewers", [])
        if v:
            out.append(str(len(v)) + " viewers in chat")
    except Exception as e:
        log("chat fetch fail", e)
    try:
        d = get_json("https://kick.com/api/v2/channels/" + channel, timeout=15)
        if d.get("chatroom", {}).get("id"):
            out.append("kick chat active")
    except Exception as e:
        log("kick chat fail", e)
    return out[:limit]

# ---------------- brain (NIM nemotron primary; pollinations fallback; template last resort) ----------------
def brain_take(facts):
    key = os.environ.get("NVIDIA_API_KEY", "")
    if key:
        for attempt in (1, 2):
            try:
                payload = {
                    "model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
                    "messages": [{"role": "user", "content": (
                        "You are the sharp, witty anchor of the ABET Sports Desk on live TV. "
                        "Write ONE punchy spoken sentence, max 20 words, reacting to this update like a real "
                        "SportsCenter anchor. Be specific, opinionated, never repeat facts verbatim, no lists, "
                        "no emojis, no hashtags. Update: " + facts
                    )}],
                    "max_tokens": 2000,
                    "chat_template_kwargs": {"enable_thinking": True},
                    "temperature": 0.7,
                }
                req = urllib.request.Request(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    j = json.loads(r.read())
                    msg = j.get("choices", [{}])[0].get("message", {})
                    txt = (msg.get("content") or "").strip()
                txt = re.sub(r"\s+", " ", txt).strip("\"'")
                if not (8 < len(txt) < 200) or "http" in txt or "{" in txt or "#" in txt:
                    log("brain: nim rejected output:", txt[:80]); continue
                if jaccard(tokens(txt), tokens(facts)) > 0.7:
                    log("brain: nim fact-echo rejected:", txt[:80]); continue
                log("brain: nim ok (attempt", attempt, ")")
                return txt
            except Exception as e:
                log(f"brain nim attempt {attempt} fail:", e)
    # Fallback to pollinations (keyless, $0)
    for attempt in (1, 2):
        try:
            q = ("You are the sharp, witty anchor of the ABET Sports Desk on live TV. "
                 "Write ONE punchy spoken sentence, max 20 words, reacting to this update like a real "
                 "SportsCenter anchor. Be specific, opinionated, never repeat facts verbatim, no lists, "
                 "no emojis. Update: " + facts)
            url = "https://text.pollinations.ai/" + urllib.parse.quote(q, safe="")
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=35) as r:
                txt = r.read().decode("utf-8", "replace").strip()
            txt = re.sub(r"\s+", " ", txt)
            txt = txt.strip("\"'")
            if not (8 < len(txt) < 200) or "http" in txt or "{" in txt or "#" in txt:
                log("brain: rejected output:", txt[:80]); continue
            if jaccard(tokens(txt), tokens(facts)) > 0.7:
                log("brain: fact-echo rejected:", txt[:80]); continue
            log("brain: pollinations ok (attempt", attempt, ")")
            return txt
        except Exception as e:
            log(f"brain attempt {attempt} fail:", e)
    return None

# ---------------- rundown ----------------
def pick_script_hint(games, breaking, highlights, profile, abet, history):
    """Rotating desk line (template fallback when the brain is unavailable)."""
    pool = []
    if breaking:
        pool.append(f"BREAKING - {breaking['title']} - per {breaking['source']}.")
    for l in abet[:2]:
        pool.append(f"Line watch - {l['event']} - {l['note']}.")
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


def _build_ticker(breaking, games, abet, profile, hint):
    """Build dynamic ticker items from all current content."""
    items = []
    if breaking and breaking.get("title"):
        items.append("BREAKING: " + breaking["title"])
    for g in games[:4]:
        items.append(g["away"] + " " + str(g["away_score"]) + " - " + g["home"] + " " + str(g["home_score"]) + " (" + g.get("status", "") + ")")
    for l in abet[:3]:
        items.append(l["note"])
    if profile and profile.get("player"):
        items.append(profile["player"] + " AVG " + str(profile.get("avg", "")) + " HR " + str(profile.get("hr", "")))
    if hint:
        items.append(hint.replace("  [brain]", ""))
    return items

def main():
    history = load_history()
    games = fetch_scores()
    breaking, reddit_ok = fetch_news_breaking(history)
    highlights = fetch_highlights_pool(history)
    if not highlights:
        highlights = history.get("last_highlights", [])
        if highlights: log("highlights carry-over (last known good):", len(highlights))
    else:
        history["last_highlights"] = highlights
    profile = fetch_season_profile(prefer_pk=highlights[0]["gamePk"] if highlights else None)
    abet = fetch_abet_lines()

    # brain first (keyless; pollinations), template fallback; anti-repeat on both paths
    hint = None
    facts = []
    if breaking: facts.append("BREAKING: " + breaking["title"] + " (per " + breaking["source"] + ")")
    for g in [x for x in games if x["away_score"] != ""][:3]:
        facts.append(f"{g['league']} {g['away']} {g['away_score']} at {g['home']} {g['home_score']} ({g['status']})")
    if abet: facts.append("line move: " + abet[0]["note"] + " (" + abet[0]["event"] + ")")
    if highlights: facts.append("highlight: " + highlights[0]["title"])
    if profile: facts.append(f"{profile['player']} batting {profile['avg']} avg, {profile['hr']} HR, {profile['rbi']} RBI")
    if facts:
        hint = brain_take("; ".join(facts))
    if hint and too_similar(hint, history["recent_topics"]):
        log("brain line too similar to recent; falling back")
        hint = None
    if hint:
        history["recent_topics"].append(hint[:160])
        history["recent_topics"] = history["recent_topics"][-ANTI_REPEAT_WINDOW:]
        hint = hint + "  [brain]"
    else:
        hint = pick_script_hint(games, breaking, highlights, profile, abet, history)

    tick_parts = []
    for g in games[:5]:
        if g["away_score"] != "":
            tick_parts.append(f"{g['league']}: {g['away']} {g['away_score']} - {g['home']} {g['home_score']} {g['status']}")
    ticker = "  //  ".join(tick_parts) if tick_parts else "ABET SPORTS DESK - LIVE"

    state = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "version": 2,
        "ticker": ticker,
        "breaking": breaking,
        "games": games,
        "highlights": highlights,
        "season_profile": profile,
        "abet_lines": abet,
        "sources": {"espn": True, "reddit": reddit_ok, "mlb_statsapi": bool(highlights or profile), "abet_board": bool(abet)},
        "chat": fetch_chat_lines(),
        "ticker_items": _build_ticker(breaking, games, abet, profile, hint),
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
