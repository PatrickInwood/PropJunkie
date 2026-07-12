"""
prop_engine.py
==============
Projection Engine → Prop Bet Analyzer with Claude Integration

Data sources:
  - The Odds API (free tier)  →  DraftKings, FanDuel, BetMGM, BetRivers, Bovada

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
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")

ODDS_API_BASE   = "https://api.the-odds-api.com/v4"


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


def get_game_scores(sport_key: str, days_from: int = 1) -> list:
    """
    Fetch live and recently completed scores from The Odds API.

    Args:
        sport_key: e.g. 'baseball_mlb'
        days_from: include completed games from the last N days (0–3)

    Returns:
        List of game objects, each with:
          - id, home_team, away_team, commence_time
          - completed (bool)
          - scores: [{"name": "Team Name", "score": "5"}, ...]
          - last_update (ISO string, or null if not started)
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores"
    params = {
        "apiKey":   ODDS_API_KEY,
        "daysFrom": min(int(days_from), 3),
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code in (422, 404):
            return []
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[PropJunkie] Scores API error for {sport_key}: {e}")
        return []


def get_game_lines(sport_key: str) -> list:
    """
    Pull moneyline (h2h), spreads, and totals for upcoming games.
    Returns a clean list of games with odds from the best available book.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey":     ODDS_API_KEY,
        "markets":    "h2h,spreads,totals",
        "regions":    "us",
        "oddsFormat": "american",
    }
    resp = requests.get(url, params=params, timeout=10)
    if resp.status_code in (422, 404):
        return []
    resp.raise_for_status()

    PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "betrivers", "bovada"]
    results = []

    for event in resp.json():
        game = {
            "id":            event["id"],
            "sport":         sport_key,
            "home_team":     event["home_team"],
            "away_team":     event["away_team"],
            "commence_time": event["commence_time"],
            "h2h":           None,
            "spreads":       None,
            "totals":        None,
            "source_book":   None,
        }

        # Pick best available book
        books = {b["key"]: b for b in event.get("bookmakers", [])}
        book = None
        for p in PREFERRED_BOOKS:
            if p in books:
                book = books[p]
                game["source_book"] = p.replace("draftkings", "DraftKings")  \
                                        .replace("fanduel", "FanDuel")        \
                                        .replace("betmgm", "BetMGM")          \
                                        .replace("betrivers", "BetRivers")    \
                                        .replace("bovada", "Bovada")
                break
        if not book and books:
            first_key = list(books.keys())[0]
            book = books[first_key]
            game["source_book"] = first_key

        if book:
            for market in book.get("markets", []):
                key = market["key"]
                outcomes = market.get("outcomes", [])

                if key == "h2h":
                    by_name = {o["name"]: o["price"] for o in outcomes}
                    game["h2h"] = {
                        "home": by_name.get(event["home_team"]),
                        "away": by_name.get(event["away_team"]),
                    }

                elif key == "spreads":
                    home_o = next((o for o in outcomes if o["name"] == event["home_team"]), None)
                    away_o = next((o for o in outcomes if o["name"] == event["away_team"]), None)
                    if home_o and away_o:
                        game["spreads"] = {
                            "home_line": home_o.get("point"),
                            "home_odds": home_o["price"],
                            "away_line": away_o.get("point"),
                            "away_odds": away_o["price"],
                        }

                elif key == "totals":
                    over  = next((o for o in outcomes if o["name"] == "Over"),  None)
                    under = next((o for o in outcomes if o["name"] == "Under"), None)
                    if over and under:
                        game["totals"] = {
                            "line":       over.get("point"),
                            "over_odds":  over["price"],
                            "under_odds": under["price"],
                        }

        results.append(game)

    return results


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
# ESPN PLAYER STATS (free, unofficial API)
# ─────────────────────────────────────────

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_HDR  = {'User-Agent': 'Mozilla/5.0 (compatible; PropJunkie/1.0)'}

ESPN_SPORT_MAP = {
    'basketball_nba':       ('basketball', 'nba'),
    'americanfootball_nfl': ('football',   'nfl'),
    'baseball_mlb':         ('baseball',   'mlb'),
    'icehockey_nhl':        ('hockey',     'nhl'),
}

MARKET_ESPN_MAP = {
    'player_pass_yds':                ('passing',   'passingYards',             'pass yards'),
    'player_pass_tds':                ('passing',   'passingTouchdowns',        'pass TDs'),
    'player_rush_yds':                ('rushing',   'rushingYards',             'rush yards'),
    'player_reception_yds':           ('receiving', 'receivingYards',           'receiving yards'),
    'player_receptions':              ('receiving', 'receptions',               'receptions'),
    'player_anytime_td':              ('rushing',   'touchdowns',               'total TDs'),
    'player_points':                  ('scoring',   'points',                   'points'),
    'player_rebounds':                ('rebounds',  'rebounds',                 'rebounds'),
    'player_assists':                 ('general',   'assists',                  'assists'),
    'player_threes':                  ('general',   'threePointFieldGoalsMade', '3-pointers'),
    'player_blocks':                  ('general',   'blocks',                   'blocks'),
    'player_steals':                  ('general',   'steals',                   'steals'),
    'player_points_rebounds_assists': ('general',   'points',                   'pts+reb+ast'),
    'player_pitcher_strikeouts':      ('pitching',  'strikeouts',               'strikeouts'),
    'player_pitcher_outs':            ('pitching',  'outs',                     'outs'),
    'player_batter_hits':             ('batting',   'hits',                     'hits'),
    'player_batter_home_runs':        ('batting',   'homeRuns',                 'home runs'),
    'player_batter_total_bases':      ('batting',   'totalBases',               'total bases'),
    'player_shots_on_goal':           ('skating',   'shots',                    'shots'),
    'player_goals':                   ('skating',   'goals',                    'goals'),
}


def fetch_espn_player_context(player_name: str, market_key: str, sport_key: str) -> str:
    """
    Fetch player's recent stats from ESPN's unofficial API (free, no key needed).
    Returns '' on any error — never blocks the main analysis.
    """
    try:
        sport_info = ESPN_SPORT_MAP.get(sport_key)
        if not sport_info:
            return ''
        sport, league = sport_info
        stat_info = MARKET_ESPN_MAP.get(market_key)
        if not stat_info:
            return ''
        cat_frag, stat_frag, human_label = stat_info
        r = requests.get(
            f"{ESPN_BASE}/{sport}/{league}/athletes",
            params={'search': player_name, 'limit': 5},
            headers=ESPN_HDR, timeout=5
        )
        if r.status_code != 200:
            return ''
        items = r.json().get('items', [])
        if not items:
            return ''
        athlete_id   = str(items[0]['id'])
        display_name = items[0].get('displayName', player_name)
        try:
            r = requests.get(
                f"{ESPN_BASE}/{sport}/{league}/athletes/{athlete_id}/gamelog",
                headers=ESPN_HDR, timeout=6
            )
            if r.status_code == 200:
                data       = r.json()
                categories = data.get('splits', {}).get('categories', [])
                entries    = data.get('splits', {}).get('entries', [])
                stat_idx, offset = None, 0
                for cat in categories:
                    names = cat.get('names', cat.get('labels', []))
                    if cat_frag.lower() in cat.get('name', '').lower():
                        for i, n in enumerate(names):
                            if stat_frag.lower() in n.lower():
                                stat_idx = offset + i
                                break
                    if stat_idx is not None:
                        break
                    offset += len(names)
                if stat_idx is not None and entries:
                    vals = []
                    for entry in entries[-5:]:
                        raw = entry.get('stats', [])
                        if stat_idx < len(raw):
                            v = str(raw[stat_idx])
                            try:
                                float(v.replace(',', ''))
                                vals.append(v)
                            except ValueError:
                                pass
                    if len(vals) >= 2:
                        nums = [float(v.replace(',', '')) for v in vals]
                        avg  = sum(nums) / len(nums)
                        return (
                            f"📊 REAL PLAYER DATA (ESPN — cite these, not generic league avgs): "
                            f"{display_name} last {len(vals)} games — {human_label}: "
                            f"{', '.join(vals)}  |  recent avg: {avg:.1f}"
                        )
        except Exception:
            pass
        r = requests.get(
            f"{ESPN_BASE}/{sport}/{league}/athletes/{athlete_id}/stats",
            headers=ESPN_HDR, timeout=5
        )
        if r.status_code != 200:
            return ''
        cats = r.json().get('splits', {}).get('categories', [])
        for cat in cats:
            if cat_frag.lower() not in cat.get('name', '').lower():
                continue
            stats = cat.get('stats', [])
            for stat in stats:
                n = stat.get('name', '')
                if 'avg' in n.lower() and stat_frag.lower() in n.lower():
                    val = stat.get('displayValue') or str(stat.get('value', ''))
                    if val:
                        return (f"📊 REAL PLAYER DATA (ESPN — season avg): "
                                f"{display_name} — {human_label}: {val}/game")
            for stat in stats:
                if stat_frag.lower() in stat.get('name', '').lower():
                    val = stat.get('displayValue') or str(stat.get('value', ''))
                    if val:
                        return (f"📊 REAL PLAYER DATA (ESPN — current season): "
                                f"{display_name} — {human_label}: {val}")
        return ''
    except Exception:
        return ''


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

def claude_explain(
    analysis: dict,
    style: str = "sharp",
    home_team: str = None,
    away_team: str = None,
    player_context: str = "",
) -> str:
    """
    Feed analysis result to Claude for a gambler-friendly breakdown.

    style options:
        "sharp"   — concise, data-forward, used by serious bettors
        "casual"  — plain English, good for general users
        "detailed" — full breakdown including all book lines and reasoning

    home_team / away_team: pass the actual teams from the selected game so
        Claude uses verified matchup context instead of potentially stale
        training-data roster knowledge.

    player_context: real ESPN game-log stats + game lines injected before the prompt
        so Claude cites actual numbers rather than generic league averages.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    style_instructions = {
        "sharp": (
            "You are a sharp sports bettor's assistant. Be terse and data-driven. "
            "ALWAYS cite the player's real recent stats provided in the context block — "
            "never substitute generic phrases like 'league average' or 'typical output'. "
            "State the edge, the line, your model's number, and give a one-line take. "
            "No fluff. Max 4 sentences."
        ),
        "casual": (
            "You are a friendly sports betting guide. Explain what the numbers mean "
            "in plain English — is this a good bet? Why or why not? "
            "Reference the player's actual recent stats from the context block when available. "
            "Keep it under 5 sentences."
        ),
        "detailed": (
            "You are a professional sports betting analyst. Give a thorough breakdown: "
            "what the model projects vs the player's real recent stats (cite them from the context block), "
            "what the market implies, where the edge comes from, "
            "which book has the best line, and any caveats. Use 6–8 sentences."
        ),
    }

    system_prompt = style_instructions.get(style, style_instructions["sharp"])

    # Build verified matchup context from the live game selection — do NOT guess rosters
    matchup_line = ""
    if home_team and away_team:
        matchup_line = f"Game (live from sportsbook API): {away_team} @ {home_team}\n"

    # Inject real player stats + game lines above the prompt
    context_block = f"{player_context}\n\n" if player_context else ""

    # Build the prompt depending on whether we have live lines
    if analysis.get("no_lines"):
        user_prompt = f"""{context_block}No live sportsbook lines are available yet for this prop.
Provide a concise contextual analysis focused on the numbers — avoid speculating about the player's current team, recent injuries, or roster situation since that information may be outdated.

{matchup_line}Player: {analysis['player']}
Sport: {analysis['sport']}
Market: {analysis['market']}
My Projection: {analysis['projection']}

Focus on:
- Whether {analysis['projection']} is a historically reasonable projection for this stat type, referencing the real stats above if available
- General advice on whether this type of prop tends to be good or bad value (market efficiency for this stat)
- What line level would make the Over vs Under attractive given this projection

Do NOT make claims about the player's current team, recent game logs, or injury status beyond what is provided in the context block above.

End with: NO LINE AVAILABLE — check back closer to game time."""
    else:
        user_prompt = f"""{context_block}Analyze this prop bet and give your verdict:

{matchup_line}{json.dumps(analysis, indent=2)}

Key things to cover:
- Model projects {analysis['projection']} vs line of {analysis['line']}
- Model says {analysis['model_prob_over_pct']}% chance of Over, market implies {analysis['implied_prob_over_pct']}%
- Edge: {analysis['edge_over_pct']}% on Over, {analysis['edge_under_pct']}% on Under
- Recommendation: {analysis['recommendation']}
- Reference the player's real recent stats from the context block above — do NOT use generic league average language

The game matchup is provided above from live API data — use it. Do NOT speculate about roster moves or injuries not confirmed in this data.

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
