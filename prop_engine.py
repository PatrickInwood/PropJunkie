"""
prop_engine.py
==============
Projection Engine → Prop Bet Analyzer with Claude Integration

Data sources:
  - The Odds API (free tier)  →  DraftKings, FanDuel, BetMGM, Bovada, etc.
  - OpticOdds (paid)          →  Westgate SuperBook + 100 other books

Pipeline:
  1. Pull live player prop lines from sportsbook API
  2. Accept your Projection Engine's stat forecast
  3. Calculate % probability of hitting Over/Under via normal distribution
  4. Strip vig to get true implied market probability
  5. Calculate edge (your prob vs market prob)
  6. Send everything to Claude for natural-language analysis

Usage:
  python prop_engine.py
  # or import analyze_prop() and claude_explain() into your existing engine
"""

import os
import json
import math
import requests
from scipy import stats
import anthropic


# ─────────────────────────────────────────
# CONFIGURATION — set via environment vars
# or replace the strings directly
# ─────────────────────────────────────────

ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "YOUR_ODDS_API_KEY")
OPTICODDS_KEY   = os.getenv("OPTICODDS_KEY", "YOUR_OPTICODDS_KEY")   # for Westgate
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")

ODDS_API_BASE   = "https://api.the-odds-api.com/v4"
OPTICODDS_BASE  = "https://api.opticodds.com/v1"   # see opticodds.com/docs


# ─────────────────────────────────────────
# SUPPORTED STAT MARKETS
# (The Odds API market keys)
# ─────────────────────────────────────────

MARKETS = {
    # NBA
    "nba_points":           "player_points",
    "nba_rebounds":         "player_rebounds",
    "nba_assists":          "player_assists",
    "nba_threes":           "player_threes",
    "nba_blocks":           "player_blocks",
    "nba_steals":           "player_steals",
    "nba_pts_reb_ast":      "player_points_rebounds_assists",

    # NFL
    "nfl_pass_yards":       "player_pass_yds",
    "nfl_pass_tds":         "player_pass_tds",
    "nfl_rush_yards":       "player_rush_yds",
    "nfl_reception_yards":  "player_reception_yds",
    "nfl_receptions":       "player_receptions",
    "nfl_anytime_td":       "player_anytime_td",

    # MLB — pitchers
    "mlb_strikeouts":           "player_pitcher_strikeouts",
    "mlb_pitcher_outs":         "player_pitcher_outs",
    "mlb_hits_allowed":         "player_pitcher_hits_allowed",
    # MLB — batters
    "mlb_home_runs":            "player_batter_home_runs",
    "mlb_hits":                 "player_batter_hits",
    "mlb_total_bases":          "player_batter_total_bases",
    "mlb_rbis":                 "player_batter_rbis",
    "mlb_runs_scored":          "player_batter_runs_scored",
    "mlb_stolen_bases":         "player_batter_stolen_bases",

    # NHL
    "nhl_shots":            "player_shots_on_goal",
    "nhl_points":           "player_points",
    "nhl_goals":            "player_goals",
    "nhl_assists":          "player_assists",
}

# Free-tier bookmakers on The Odds API
# Only include books confirmed to offer player props
# (Westgate/SuperBook requires OpticOdds — see get_westgate_props())
FREE_BOOKMAKERS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "betrivers",
    "bovada",
]


# ─────────────────────────────────────────
# DATA LAYER — The Odds API
# ─────────────────────────────────────────

def get_events(sport_key: str) -> list:
    """
    Return upcoming events for a sport.
    sport_key examples: basketball_nba, americanfootball_nfl, baseball_mlb, icehockey_nhl
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events"
    resp = requests.get(url, params={"apiKey": ODDS_API_KEY}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_event_props(sport_key: str, event_id: str, markets: list, bookmakers: list = None) -> dict:
    """Pull player prop odds for a specific game.

    Uses regions=us instead of specifying exact bookmakers — this casts a
    wider net and avoids 422 errors when individual books don't carry a market.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey":      ODDS_API_KEY,
        "markets":     ",".join(markets),
        "regions":     "us",          # let API return any US book that has the market
        "oddsFormat":  "american",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 422:
            raise ValueError(
                f"No odds available for this market on this game. "
                f"The sportsbooks may not have posted lines yet, or this market "
                f"({', '.join(markets)}) isn't offered for this event."
            )
        resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────
# DATA LAYER — OpticOdds (Westgate/SuperBook)
# Set OPTICODDS_KEY to enable this path.
# Sign up: opticodds.com/contact
# ─────────────────────────────────────────

def get_westgate_props(sport: str, event_id: str, market: str) -> dict:
    """
    Pull Westgate SuperBook player props via OpticOdds API.
    Returns same normalized format as get_event_props() for drop-in use.

    Requires a paid OpticOdds subscription.
    Docs: https://developer.opticodds.com/reference/getting-started
    """
    if OPTICODDS_KEY == "YOUR_OPTICODDS_KEY":
        raise ValueError(
            "Westgate/SuperBook requires an OpticOdds API key.\n"
            "Sign up at https://opticodds.com/contact\n"
            "Then set OPTICODDS_KEY in your environment."
        )

    url = f"{OPTICODDS_BASE}/odds"
    headers = {"X-Api-Key": OPTICODDS_KEY}
    params = {
        "sport":      sport,
        "eventId":    event_id,
        "market":     market,
        "sportsbook": "superbook",   # Westgate SuperBook key in OpticOdds
        "oddsFormat": "american",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────
# ODDS MATH
# ─────────────────────────────────────────

def american_to_prob(american_odds: float) -> float:
    """Convert American odds to raw implied probability (includes vig)."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def remove_vig(over_odds: float, under_odds: float) -> tuple[float, float]:
    """
    Strip the vig to get the true no-juice implied probabilities.
    Returns (true_over_prob, true_under_prob).
    """
    raw_over  = american_to_prob(over_odds)
    raw_under = american_to_prob(under_odds)
    total     = raw_over + raw_under
    return raw_over / total, raw_under / total


def prob_to_american(prob: float) -> str:
    """Convert probability back to American odds format for display."""
    if prob >= 0.5:
        return f"-{round((prob / (1 - prob)) * 100)}"
    else:
        return f"+{round(((1 - prob) / prob) * 100)}"


# ─────────────────────────────────────────
# PROBABILITY MODEL
# ─────────────────────────────────────────

def calculate_hit_probability(projection: float, line: float, std_dev_pct: float = 0.25) -> float:
    """
    Estimate P(player goes OVER the line) using a normal distribution.

    Args:
        projection:   Your engine's projected stat value (e.g. 26.4 pts)
        line:         The sportsbook's prop line (e.g. 24.5)
        std_dev_pct:  Game-to-game volatility as a fraction of projection.
                      0.25 (25%) is a reasonable default for NBA points.
                      Tune per sport/stat type — see STD_DEV_DEFAULTS below.

    Returns:
        Float 0.0–1.0 representing P(Over)
    """
    std_dev = projection * std_dev_pct
    if std_dev == 0:
        return 1.0 if projection > line else 0.0
    z = (line - projection) / std_dev
    return float(1 - stats.norm.cdf(z))


# Suggested std_dev_pct defaults by stat type
STD_DEV_DEFAULTS = {
    # NBA
    "player_points":                        0.28,
    "player_rebounds":                      0.32,
    "player_assists":                       0.35,
    "player_threes":                        0.55,
    "player_blocks":                        0.60,
    "player_steals":                        0.65,
    "player_points_rebounds_assists":       0.22,
    # NFL
    "player_pass_yds":                      0.26,
    "player_pass_tds":                      0.65,
    "player_rush_yds":                      0.40,
    "player_reception_yds":                 0.45,
    "player_receptions":                    0.40,
    "player_anytime_td":                    0.70,
    # MLB — pitchers
    "player_pitcher_strikeouts":            0.30,
    "player_pitcher_outs":                  0.28,
    "player_pitcher_hits_allowed":          0.35,
    # MLB — batters (rare events = high variance)
    "player_batter_home_runs":              0.90,
    "player_batter_hits":                   0.45,
    "player_batter_total_bases":            0.50,
    "player_batter_rbis":                   0.70,
    "player_batter_runs_scored":            0.65,
    "player_batter_stolen_bases":           0.80,
    # NHL
    "player_shots_on_goal":                 0.35,
    "player_goals":                         0.80,
    "player_assists":                       0.70,
    "player_points":                        0.55,
}


# ─────────────────────────────────────────
# PROP EXTRACTOR
# ─────────────────────────────────────────

def extract_player_prop(event_data: dict, player_name: str, market_key: str) -> list:
    """
    Find a player's Over/Under outcomes across all bookmakers in the event data.
    Returns a list of dicts, one per bookmaker that has the line.
    """
    results = []
    for book in event_data.get("bookmakers", []):
        for market in book.get("markets", []):
            if market["key"] != market_key:
                continue
            outcomes = market.get("outcomes", [])
            over  = next((o for o in outcomes if o["name"] == "Over"  and player_name.lower() in o.get("description", "").lower()), None)
            under = next((o for o in outcomes if o["name"] == "Under" and player_name.lower() in o.get("description", "").lower()), None)
            if over and under and over.get("point") is not None:
                results.append({
                    "bookmaker":   book["title"],
                    "line":        over["point"],
                    "over_odds":   over["price"],
                    "under_odds":  under["price"],
                })
    return results


def best_line(props: list, side: str = "over") -> dict | None:
    """
    Given a list of bookmaker props, return the one with the best odds for your chosen side.
    side = 'over' or 'under'
    """
    if not props:
        return None
    key = "over_odds" if side == "over" else "under_odds"
    return max(props, key=lambda p: p[key] if p[key] < 0 else p[key])


# ─────────────────────────────────────────
# CORE ANALYSIS
# ─────────────────────────────────────────

def analyze_prop(
    player_name:  str,
    projection:   float,
    market_key:   str,
    sport_key:    str,
    event_id:     str,
    std_dev_pct:  float = None,
    use_westgate: bool  = False,
) -> dict:
    """
    Full analysis pipeline for a single player prop.

    Args:
        player_name:  Player's name (partial match OK, e.g. "LeBron")
        projection:   Your engine's projected value for the stat
        market_key:   The Odds API market key (e.g. "player_points")
        sport_key:    Sport identifier (e.g. "basketball_nba")
        event_id:     Event ID from get_events()
        std_dev_pct:  Override default volatility for this stat
        use_westgate: Pull from Westgate SuperBook via OpticOdds (requires paid key)

    Returns:
        Analysis dict with probabilities, edge, and recommendation
    """
    # Use stat-specific std dev if not overridden
    if std_dev_pct is None:
        std_dev_pct = STD_DEV_DEFAULTS.get(market_key, 0.25)

    # Pull lines — fall back gracefully if API has no lines for this market/event
    props = []
    no_lines_msg = None
    try:
        if use_westgate:
            raw_data = get_westgate_props(sport_key, event_id, market_key)
        else:
            raw_data = get_event_props(sport_key, event_id, [market_key])
        props = extract_player_prop(raw_data, player_name, market_key)
        if not props:
            no_lines_msg = (
                f"No '{market_key}' line found for '{player_name}' in the API response. "
                f"The books may not have posted this prop yet."
            )
    except ValueError as e:
        no_lines_msg = str(e)
    except Exception as e:
        return {"error": str(e)}

    # If no live lines, run a projection-only analysis (Claude still gives context)
    if no_lines_msg:
        return {
            "player":          player_name,
            "market":          market_key,
            "sport":           sport_key,
            "projection":      projection,
            "line":            None,
            "hit_probability": None,
            "edge":            None,
            "recommendation":  "No live lines — projection-only",
            "no_lines":        True,
            "no_lines_reason": no_lines_msg,
        }

    # Use the line with the best over odds (you could change to 'under' if fading)
    top = best_line(props, side="over")
    line = top["line"]

    # Probability calculation
    model_prob_over      = calculate_hit_probability(projection, line, std_dev_pct)
    model_prob_under     = 1 - model_prob_over
    implied_over, implied_under = remove_vig(top["over_odds"], top["under_odds"])

    edge_over  = model_prob_over  - implied_over
    edge_under = model_prob_under - implied_under

    # Simple recommendation thresholds (tune as you like)
    if edge_over >= 0.05:
        recommendation = "OVER ✅"
        edge_display   = edge_over
    elif edge_under >= 0.05:
        recommendation = "UNDER ✅"
        edge_display   = edge_under
    elif 0.02 <= edge_over < 0.05:
        recommendation = "LEAN OVER"
        edge_display   = edge_over
    elif 0.02 <= edge_under < 0.05:
        recommendation = "LEAN UNDER"
        edge_display   = edge_under
    else:
        recommendation = "NO EDGE — PASS"
        edge_display   = max(edge_over, edge_under)

    return {
        "player":               player_name,
        "market":               market_key,
        "sport":                sport_key,
        "projection":           projection,
        "line":                 line,
        "best_book":            top["bookmaker"],
        "over_odds":            top["over_odds"],
        "under_odds":           top["under_odds"],
        "all_books":            props,
        "model_prob_over_pct":  round(model_prob_over  * 100, 1),
        "model_prob_under_pct": round(model_prob_under * 100, 1),
        "implied_prob_over_pct":  round(implied_over  * 100, 1),
        "implied_prob_under_pct": round(implied_under * 100, 1),
        "edge_over_pct":        round(edge_over  * 100, 1),
        "edge_under_pct":       round(edge_under * 100, 1),
        "recommendation":       recommendation,
        "std_dev_used_pct":     round(std_dev_pct * 100, 1),
    }


# ─────────────────────────────────────────
# MULTI-PROP BATCH SCAN
# ─────────────────────────────────────────

def scan_props(prop_list: list, sport_key: str, event_id: str, min_edge: float = 0.02) -> list:
    """
    Analyze a batch of player props and return only those with edge.

    prop_list format:
        [
            {"player": "LeBron James", "projection": 26.4, "market": "player_points"},
            {"player": "Anthony Davis", "projection": 11.2, "market": "player_rebounds"},
        ]

    min_edge: minimum edge % to include in results (default 2%)
    """
    results = []
    for prop in prop_list:
        result = analyze_prop(
            player_name = prop["player"],
            projection  = prop["projection"],
            market_key  = prop["market"],
            sport_key   = sport_key,
            event_id    = event_id,
        )
        if "error" not in result:
            max_edge = max(result["edge_over_pct"], result["edge_under_pct"])
            if max_edge >= min_edge * 100:
                results.append(result)

    # Sort by biggest edge first
    results.sort(key=lambda r: max(r["edge_over_pct"], r["edge_under_pct"]), reverse=True)
    return results


# ─────────────────────────────────────────
# CLAUDE INTEGRATION
# ─────────────────────────────────────────

def claude_explain(analysis: dict, style: str = "sharp") -> str:
    """
    Feed analysis result to Claude for a gambler-friendly breakdown.

    style options:
        "sharp"   — concise, data-forward, used by serious bettors
        "casual"  — plain English, good for general users
        "detailed" — full breakdown including all book lines and reasoning
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    style_instructions = {
        "sharp": (
            "You are a sharp sports bettor's assistant. Be terse and data-driven. "
            "State the edge, the line, your model's number, and give a one-line take. "
            "No fluff. Max 4 sentences."
        ),
        "casual": (
            "You are a friendly sports betting guide. Explain what the numbers mean "
            "in plain English — is this a good bet? Why or why not? Keep it under 5 sentences."
        ),
        "detailed": (
            "You are a professional sports betting analyst. Give a thorough breakdown: "
            "what the model projects, what the market implies, where the edge comes from, "
            "which book has the best line, and any caveats. Use 6–8 sentences."
        ),
    }

    system_prompt = style_instructions.get(style, style_instructions["sharp"])

    # Build the prompt depending on whether we have live lines
    if analysis.get("no_lines"):
        user_prompt = f"""No live sportsbook lines are available yet for this prop.
Give your contextual analysis based on what you know about this player and this stat:

Player: {analysis['player']}
Sport: {analysis['sport']}
Market: {analysis['market']}
My Projection: {analysis['projection']}

Since there's no market line to compare against, focus on:
- Whether {analysis['projection']} is a realistic projection for this player in this stat
- Historical context for this player/matchup if you know it
- General advice on whether this type of prop is typically good or bad value

End with: NO LINE AVAILABLE — check back closer to game time."""
    else:
        user_prompt = f"""Analyze this prop bet and give your verdict:

{json.dumps(analysis, indent=2)}

Key things to cover:
- Model projects {analysis['projection']} vs line of {analysis['line']}
- Model says {analysis['model_prob_over_pct']}% chance of Over, market implies {analysis['implied_prob_over_pct']}%
- Edge: {analysis['edge_over_pct']}% on Over, {analysis['edge_under_pct']}% on Under
- Recommendation: {analysis['recommendation']}

End your response with a clear OVER / UNDER / PASS on a new line."""

    message = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 400,
        system     = system_prompt,
        messages   = [{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def claude_batch_summary(results: list) -> str:
    """Ask Claude to summarize a batch of prop analyses into a betting card."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are a sharp sports betting analyst. Below is a batch of player prop analyses from a Projection Engine.
Summarize the best plays into a clean betting card. Rank by edge. Skip anything with no edge.
For each play: player name, stat, line, recommendation, and edge %.
Keep it punchy — this is a pre-game betting card.

Props Analyzed:
{json.dumps(results, indent=2)}"""

    message = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 600,
        messages   = [{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ─────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────

def print_analysis(result: dict):
    """Pretty-print a single prop analysis to console."""
    if "error" in result:
        print(f"⚠️  Error: {result['error']}")
        return

    print(f"\n{'='*55}")
    print(f"  {result['player'].upper()} — {result['market'].replace('player_','').upper()}")
    print(f"{'='*55}")
    print(f"  Line:        {result['line']}  ({result['over_odds']:+d} / {result['under_odds']:+d})  @ {result['best_book']}")
    print(f"  Projection:  {result['projection']}")
    print(f"  Model Over:  {result['model_prob_over_pct']}%  |  Market Over: {result['implied_prob_over_pct']}%")
    print(f"  Edge Over:  {result['edge_over_pct']:+.1f}%   |  Edge Under: {result['edge_under_pct']:+.1f}%")
    print(f"  → {result['recommendation']}")
    if len(result["all_books"]) > 1:
        print(f"\n  All books:")
        for b in result["all_books"]:
            print(f"    {b['bookmaker']:20s}  Line: {b['line']}  Over: {b['over_odds']:+d}  Under: {b['under_odds']:+d}")
    print(f"{'='*55}\n")


# ─────────────────────────────────────────
# EXAMPLE / DEMO
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("Projection Engine — Prop Analyzer")
    print("-----------------------------------")

    SPORT = "basketball_nba"

    # Step 1: Get today's games
    print(f"\nFetching {SPORT} events...")
    events = get_events(SPORT)

    if not events:
        print("No events found. NBA may be off-season or no games today.")
        print("Try: americanfootball_nfl  |  baseball_mlb  |  icehockey_nhl")
        exit()

    # Show available games
    print(f"\nAvailable games ({len(events)} found):")
    for i, e in enumerate(events[:5]):
        print(f"  [{i}] {e.get('home_team')} vs {e.get('away_team')}  —  {e.get('commence_time', '')[:10]}")

    # Step 2: Pick a game (default: first one)
    event = events[0]
    event_id = event["id"]
    print(f"\nAnalyzing: {event.get('home_team')} vs {event.get('away_team')}")

    # ─────────────────────────────────────
    # SINGLE PROP — plug in your projection
    # ─────────────────────────────────────
    # Replace these values with your Projection Engine's output:
    PLAYER     = "LeBron James"
    PROJECTION = 26.4           # Your engine's projected points
    MARKET     = "player_points"

    print(f"\nPulling prop for {PLAYER} ({MARKET})...")
    result = analyze_prop(
        player_name  = PLAYER,
        projection   = PROJECTION,
        market_key   = MARKET,
        sport_key    = SPORT,
        event_id     = event_id,
        # use_westgate = True,  # ← Uncomment after setting OPTICODDS_KEY
    )

    print_analysis(result)

    if "error" not in result:
        print("Claude's take:\n")
        explanation = claude_explain(result, style="sharp")
        print(explanation)

    # ─────────────────────────────────────
    # BATCH SCAN — multiple props at once
    # ─────────────────────────────────────
    # Uncomment to scan a full slate of your engine's projections:

    # prop_slate = [
    #     {"player": "LeBron James",   "projection": 26.4, "market": "player_points"},
    #     {"player": "Anthony Davis",  "projection": 11.2, "market": "player_rebounds"},
    #     {"player": "Stephen Curry",  "projection": 4.8,  "market": "player_threes"},
    # ]
    # print("\nScanning prop slate for edges...")
    # batch_results = scan_props(prop_slate, SPORT, event_id, min_edge=0.02)
    # print(claude_batch_summary(batch_results))
