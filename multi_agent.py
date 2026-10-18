# multi_agent_orchestrator.py — ABET Sports Desk multi-agent coordinator
# Agents: CHAT, STATS, NEWS, SCRIPT/PRODUCER, RENDER, PLAYER
# Free resources only. No secrets touched.
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desk_engine import (fetch_scores, fetch_news_breaking, fetch_highlights_pool,
                         fetch_season_profile, fetch_abet_lines, brain_take,
                         fetch_chat_lines, pick_script_hint, load_history,
                         OUT, log, HISTORY)

AGENTS = {
    "CHAT":      {"role": "poll public chat, filter questions", "model": "none (polling)", "status": "active"},
    "STATS":     {"role": "ESPN scoreboard + MLB statsapi + ABET line moves", "model": "none (API)", "status": "active"},
    "NEWS":      {"role": "ESPN news + breaking detection (<45min)", "model": "none (API)", "status": "active"},
    "PRODUCER":  {"role": "nemotron generates script hint from all inputs", "model": "nemotron-3.5-super-vl", "status": "active"},
    "RENDER":    {"role": "Flux portrait + SadTalker lip-sync + FFmpeg composite", "model": "Flux.1-dev (DOWN)", "status": "degraded"},
    "PLAYER":    {"role": "client polls desk_state.json, crossfades clips", "model": "none (JS)", "status": "active"},
}

def run_chat_agent():
    log("[CHAT] polling...")
    lines = fetch_chat_lines()
    log("[CHAT] lines:", len(lines))
    return lines

def run_stats_agent():
    log("[STATS] fetching games + lines...")
    games = fetch_scores()
    abet = fetch_abet_lines()
    log("[STATS] games:", len(games), "lines:", len(abet))
    return games, abet

def run_news_agent(history):
    log("[NEWS] fetching breaking...")
    breaking, reddit_ok = fetch_news_breaking(history)
    log("[NEWS] breaking:", breaking["title"] if breaking else None, "reddit_ok:", reddit_ok)
    return breaking, reddit_ok

def run_producer_agent(games, breaking, highlights, profile, abet, history, chat):
    log("[PRODUCER] generating script hint...")
    facts = []
    if breaking: facts.append("BREAKING: " + breaking["title"])
    for l in abet[:2]: facts.append("Line watch: " + l["event"] + " - " + l["note"])
    live = [g for g in games if "Final" not in g["status"] and g["status"]]
    for g in live[:2]: facts.append(g["away"] + " at " + g["home"] + " " + str(g["away_score"]) + "-" + str(g["home_score"]))
    if chat: facts.append("Chat: " + chat[0])
    hint = brain_take("; ".join(facts))
    if not hint:
        hint = pick_script_hint(games, breaking, highlights, profile, abet, history)
    log("[PRODUCER] hint:", hint)
    return hint

def run_render_agent(hint):
    log("[RENDER] status: Flux endpoint DOWN - using existing clips")
    # When Flux returns, generate portraits + lip-sync here
    # For now, reuse existing clips from assets/
    return None

def orchestrate():
    log("=== MULTI-AGENT ORCHESTRATION START ===")
    history = load_history()
    # Run agents
    chat = run_chat_agent()
    games, abet = run_stats_agent()
    breaking, reddit_ok = run_news_agent(history)
    highlights = fetch_highlights_pool(history)
    profile = fetch_season_profile(highlights[0]["gamePk"] if highlights else None)
    hint = run_producer_agent(games, breaking, highlights, profile, abet, history, chat)
    render_result = run_render_agent(hint)
    # Save history
    import json
    json.dump(history, open(HISTORY, 'w', encoding='utf-8'), indent=1)
    log("=== ORCHESTRATION COMPLETE ===")
    return {
        "chat": chat,
        "games": len(games),
        "breaking": breaking["title"] if breaking else None,
        "highlights": len(highlights),
        "profile": profile["player"] if profile else None,
        "hint": hint,
        "render": render_result,
    }

if __name__ == "__main__":
    result = orchestrate()
    print(json.dumps(result, indent=2))
