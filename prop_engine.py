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
import re
import json
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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


def _safe_http_raise(resp):
    """Like raise_for_status() but strips API keys from the error URL."""
    if not resp.ok:
        safe_url = re.sub(r'apiKey=[^&\s]+', 'apiKey=***', resp.url)
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} Error: {safe_url}", response=resp
        )


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
    _safe_http_raise(resp)
    return resp.json()


ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"

# US sports run on a US schedule, so anchor "today" to US Eastern — not the
# server's clock (Railway runs in UTC, which rolls to tomorrow late US-evening).
# ESPN's default (no-date) scoreboard also lags to yesterday in the morning, so
# we always pass explicit dates instead of relying on the default.
ESPN_TZ = ZoneInfo("America/New_York")


def _espn_dates(days_forward: int = 2, days_back: int = 0) -> list:
    """Explicit YYYYMMDD date strings around US-Eastern 'today' (oldest → newest)."""
    today = datetime.now(ESPN_TZ).date()
    return [(today + timedelta(days=i)).strftime("%Y%m%d")
            for i in range(-days_back, days_forward + 1)]


def get_game_scores(sport_key: str, days_from: int = 1) -> list:
    """
    Fetch live and recently completed scores from ESPN's free scoreboard API.

    ESPN needs no API key and has no request quota, so — unlike the Odds API —
    polling it for live scores never eats into our paid-data budget. Team names
    match the Odds API's, so the front end pairs scores to lines by name.

    Args:
        sport_key: e.g. 'baseball_mlb'
        days_from: also include finals from the last N days (0–3); today's board
                   already covers live games and today's finals.

    Returns:
        List of game objects, each with:
          - id, home_team, away_team, commence_time
          - completed (bool)
          - scores: [{"name": "Team Name", "score": "5"}, ...]  (empty until start)
          - status_detail: e.g. "Top 7th" / "Final" (for future display)
    """
    sport_info = ESPN_SPORT_MAP.get(sport_key)
    if not sport_info:
        return []
    sport, league = sport_info
    url = ESPN_SCOREBOARD.format(sport=sport, league=league)

    # Explicit US-Eastern dates: today (live + today's finals) plus prior days.
    # (The default no-date board lags to yesterday morning-of, so we don't use it.)
    date_params = _espn_dates(days_forward=0, days_back=min(int(days_from), 3))

    games, seen = [], set()
    try:
        for dp in date_params:
            resp = requests.get(url, params={"dates": dp},
                                headers=ESPN_HDR, timeout=8)
            if resp.status_code != 200:
                continue
            for ev in resp.json().get("events", []):
                eid = ev.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                comp = (ev.get("competitions") or [{}])[0]
                type_info = ev.get("status", {}).get("type", {})
                state = type_info.get("state")   # 'pre' | 'in' | 'post'
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                away = next((c for c in competitors if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue
                home_name = home.get("team", {}).get("displayName")
                away_name = away.get("team", {}).get("displayName")
                scores = []
                if state in ("in", "post"):
                    scores = [
                        {"name": home_name, "score": str(home.get("score", ""))},
                        {"name": away_name, "score": str(away.get("score", ""))},
                    ]
                games.append({
                    "id":            eid,
                    "home_team":     home_name,
                    "away_team":     away_name,
                    "commence_time": ev.get("date"),
                    "completed":     state == "post",
                    "scores":        scores,
                    "status_detail": type_info.get("shortDetail"),
                })
        return games
    except requests.exceptions.RequestException as e:
        print(f"[PropJunkie] ESPN scores error for {sport_key}: {e}")
        return []


def _odds_phase(side: dict, field: str):
    """Read a value from an ESPN odds side (home/away/over/under).

    ESPN nests the number under a phase — 'close' (current pre-game line),
    falling back to 'open'/'current'. Returns None if absent.
    """
    if not isinstance(side, dict):
        return None
    for phase in ("close", "current", "open"):
        block = side.get(phase)
        if isinstance(block, dict) and block.get(field) is not None:
            return block[field]
    return None


def _american(s):
    """'+101' / '-121' → 101 / -121. None on failure."""
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None


def _spread_line(s):
    """'+1.5' / '-1.5' / 'o7.5' / 'u7.5' → signed float. None on failure."""
    if s is None:
        return None
    try:
        return float(str(s).strip().lstrip("ouOU").strip())
    except (ValueError, TypeError):
        return None


def fetch_final_scores(sport_key: str, yyyymmdd: str) -> dict:
    """Final scores for one date, keyed by ESPN event id (used to grade picks).

    {event_id: {'home_score', 'away_score', 'completed'}}. {} on failure.
    Since our game ids ARE ESPN event ids, picks grade by exact id lookup.
    """
    sport_info = ESPN_SPORT_MAP.get(sport_key)
    if not sport_info:
        return {}
    sport, league = sport_info
    url = ESPN_SCOREBOARD.format(sport=sport, league=league)
    out = {}
    try:
        r = requests.get(url, params={"dates": yyyymmdd}, headers=ESPN_HDR, timeout=8)
        if r.status_code != 200:
            return {}
        for ev in r.json().get("events", []):
            state = ev.get("status", {}).get("type", {}).get("state")
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            try:
                hs = int(home.get("score"))
                as_ = int(away.get("score"))
            except (TypeError, ValueError):
                continue
            out[ev.get("id")] = {"home_score": hs, "away_score": as_,
                                 "completed": state == "post"}
        return out
    except requests.exceptions.RequestException:
        return {}


def get_game_lines(sport_key: str) -> list:
    """
    Moneyline (h2h), spreads, and totals for today's + the next 2 days' games,
    from ESPN's free scoreboard API (DraftKings line). No API key, no quota.

    Returns the same shape as the Odds API path so the front end is unchanged:
    each game has h2h / spreads / totals (or None), plus source_book. Games
    without a posted line are still listed (schedule) with null markets.
    """
    sport_info = ESPN_SPORT_MAP.get(sport_key)
    if not sport_info:
        return []
    sport, league = sport_info
    url = ESPN_SCOREBOARD.format(sport=sport, league=league)

    # Today + next 2 days (ESPN posts odds ~24–36h out), as explicit US-Eastern
    # dates so we never miss today's slate. ESPN is free, so extra fetches cost
    # nothing.
    results, seen = [], set()
    try:
        for dp in _espn_dates(days_forward=2):
            resp = requests.get(url, params={"dates": dp},
                                headers=ESPN_HDR, timeout=8)
            if resp.status_code != 200:
                continue
            for ev in resp.json().get("events", []):
                eid = ev.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                away = next((c for c in competitors if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue

                game = {
                    "id":            eid,
                    "sport":         sport_key,
                    "home_team":     home.get("team", {}).get("displayName"),
                    "away_team":     away.get("team", {}).get("displayName"),
                    "commence_time": ev.get("date"),
                    "h2h":           None,
                    "spreads":       None,
                    "totals":        None,
                    "source_book":   None,
                }

                odds_list = comp.get("odds") or []
                o = odds_list[0] if odds_list else None
                if o:
                    game["source_book"] = (o.get("provider") or {}).get("name")

                    ml = o.get("moneyline") or {}
                    mh = _american(_odds_phase(ml.get("home"), "odds"))
                    ma = _american(_odds_phase(ml.get("away"), "odds"))
                    if mh is not None and ma is not None:
                        game["h2h"] = {"home": mh, "away": ma}

                    ps = o.get("pointSpread") or {}
                    hl = _spread_line(_odds_phase(ps.get("home"), "line"))
                    al = _spread_line(_odds_phase(ps.get("away"), "line"))
                    if hl is not None and al is not None:
                        game["spreads"] = {
                            "home_line": hl, "home_odds": _american(_odds_phase(ps.get("home"), "odds")),
                            "away_line": al, "away_odds": _american(_odds_phase(ps.get("away"), "odds")),
                        }

                    tot = o.get("total") or {}
                    over_odds  = _american(_odds_phase(tot.get("over"), "odds"))
                    under_odds = _american(_odds_phase(tot.get("under"), "odds"))
                    line = _spread_line(_odds_phase(tot.get("over"), "line"))
                    if line is None:
                        line = o.get("overUnder")
                    if line is not None and over_odds is not None and under_odds is not None:
                        game["totals"] = {"line": line, "over_odds": over_odds, "under_odds": under_odds}

                results.append(game)
        return results
    except requests.exceptions.RequestException as e:
        print(f"[PropJunkie] ESPN lines error for {sport_key}: {e}")
        return []


def get_game_lines_oddsapi(sport_key: str) -> list:
    """
    Pull moneyline (h2h), spreads, and totals from The Odds API (multi-book).

    DORMANT: PropJunkie runs on ESPN's free odds by default (see get_game_lines).
    This path costs Odds API quota but compares many books — revive it as a paid
    "best line across books" upgrade once the product earns revenue.
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
    _safe_http_raise(resp)

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
    if resp.status_code in (401, 403):
        raise ValueError(
            f"Player prop markets ({', '.join(markets)}) require an upgraded Odds API subscription. "
            f"Analysis will continue without live sportsbook lines — projection and ESPN data still apply."
        )
    if resp.status_code == 422:
        raise ValueError(
            f"No odds available for this market on this game. "
            f"The sportsbooks may not have posted lines yet, or this market "
            f"({', '.join(markets)}) isn't offered for this event."
        )
    _safe_http_raise(resp)
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
    # (cat_name_fragment, [stat_name_fragments...], human_label)
    # ESPN often uses abbreviations (H, HR, K, REB, AST) not full names.
    # Provide multiple search terms — the first match wins.
    'player_pass_yds':                ('passing',   ['passingyards', 'yds', 'yards'],           'pass yards'),
    'player_pass_tds':                ('passing',   ['touchdowns', 'td', 'tds'],                'pass TDs'),
    'player_rush_yds':                ('rushing',   ['rushingyards', 'yds', 'yards'],           'rush yards'),
    'player_reception_yds':           ('receiving', ['receivingyards', 'yds', 'yards'],         'receiving yards'),
    'player_receptions':              ('receiving', ['receptions', 'rec'],                      'receptions'),
    'player_anytime_td':              ('scoring',   ['touchdowns', 'td', 'tds'],               'total TDs'),
    'player_points':                  ('scoring',   ['points', 'pts'],                          'points'),
    'player_rebounds':                ('rebounds',  ['rebounds', 'reb'],                        'rebounds'),
    'player_assists':                 ('general',   ['assists', 'ast'],                         'assists'),
    'player_threes':                  ('general',   ['threepointfieldgoalsmade', '3pm', 'fg3m'],'3-pointers'),
    'player_blocks':                  ('general',   ['blocks', 'blk'],                          'blocks'),
    'player_steals':                  ('general',   ['steals', 'stl'],                          'steals'),
    'player_points_rebounds_assists': ('scoring',   ['points', 'pts'],                          'pts+reb+ast'),
    'player_pitcher_strikeouts':      ('pitching',  ['strikeouts', 'so', ' k'],                'strikeouts'),
    'player_pitcher_outs':            ('pitching',  ['outs', 'ip'],                             'outs'),
    'player_batter_hits':             ('batting',   [' h ', 'hits', ' h\t'],                   'hits'),
    'player_batter_home_runs':        ('batting',   ['hr', 'homeruns', 'home runs'],            'home runs'),
    'player_batter_total_bases':      ('batting',   ['totalbases', 'tb'],                       'total bases'),
    'player_batter_rbis':             ('batting',   ['rbi', 'rbis', 'runsbattedin'],            'RBIs'),
    'player_batter_runs_scored':      ('batting',   [' r ', 'runs', 'runsscored'],              'runs scored'),
    'player_batter_stolen_bases':     ('batting',   ['stolenbases', 'sb', ' sb '],              'stolen bases'),
    'player_pitcher_hits_allowed':    ('pitching',  [' h ', 'hits', 'hitsallowed'],             'hits allowed'),
    'player_batter_singles':          ('batting',   ['singles', '1b'],                          'singles'),
    'player_batter_doubles':          ('batting',   ['doubles', '2b'],                          'doubles'),
    'player_batter_triples':          ('batting',   ['triples', '3b'],                          'triples'),
    'player_batter_walks':            ('batting',   ['walks', 'bb', 'baseonballs'],             'walks'),
    'player_batter_strikeouts':       ('batting',   ['strikeouts', 'so', ' k'],                 'strikeouts'),
    'player_hits_runs_rbis':          ('batting',   ['hits'],                                   'hits + runs + RBIs'),
    'player_pitcher_walks_allowed':   ('pitching',  ['walks', 'bb', 'baseonballs'],             'walks allowed'),
    'player_pitcher_earned_runs':     ('pitching',  ['earnedruns', 'er'],                       'earned runs'),
    'player_shots_on_goal':           ('skating',   ['shots', 'sog'],                           'shots'),
    'player_goals':                   ('skating',   ['goals', ' g '],                           'goals'),
}


def fetch_espn_player_context(player_name: str, market_key: str, sport_key: str) -> str:
    """Build the AI prompt's recent-game context line from free game logs.

    Uses the same data layer as the projection engine (``fetch_recent_stat_values``):
    MLB via the official MLB Stats API, NBA/NFL/NHL via ESPN game logs. This keeps
    the analysis and the projection citing the *same* real recent-game numbers.
    Returns '' on any error so it never blocks the main analysis.
    """
    try:
        values = fetch_recent_stat_values(player_name, market_key, sport_key, limit=10)
        if len(values) < 2:
            return ''
        recent = values[-5:]
        label  = MARKET_ESPN_MAP.get(market_key, (None, None, market_key))[2]
        source = "MLB Stats API" if sport_key == "baseball_mlb" else "ESPN"

        def _fmt(v):
            return str(int(v)) if float(v).is_integer() else f"{v:.1f}"

        shown = ', '.join(_fmt(v) for v in recent)
        avg   = sum(recent) / len(recent)
        return (
            f"📊 REAL PLAYER DATA ({source} — cite these, not generic league avgs): "
            f"{player_name} last {len(recent)} games — {label}: "
            f"{shown}  |  recent avg: {avg:.1f}"
        )
    except Exception:
        return ''


# ─────────────────────────────────────────
# ESPN OPPONENT DEFENSIVE STATS
# ─────────────────────────────────────────

def _words_overlap(a: str, b: str) -> bool:
    """True if any meaningful word (>2 chars) appears in both team names."""
    aw = {w for w in (a or "").lower().split() if len(w) > 2}
    bw = {w for w in (b or "").lower().split() if len(w) > 2}
    return bool(aw & bw)


def _match_rating(team_name: str, ratings: dict):
    """Find a team's rating row by exact name, then by word overlap."""
    if team_name in ratings:
        return ratings[team_name]
    for t, r in ratings.items():
        if _words_overlap(team_name, t):
            return r
    return None


def _player_team(player_name: str, sport_key: str) -> str:
    """The player's current team name, for the opponent lookup. '' on failure.

    MLB: the official Stats API (currentTeam). NBA/NFL/NHL: ESPN's search API,
    which returns the team in each result's subtitle.
    """
    try:
        if sport_key == "baseball_mlb":
            r = requests.get(f"{MLB_STATS_BASE}/people/search",
                             params={"names": player_name, "hydrate": "currentTeam"},
                             headers=ESPN_HDR, timeout=6)
            if r.status_code != 200:
                return ""
            ppl = r.json().get("people", [])
            return ((ppl[0].get("currentTeam") or {}).get("name") or "") if ppl else ""
        r = requests.get(ESPN_SEARCH_URL, params={"query": player_name, "limit": 5},
                         headers=ESPN_HDR, timeout=6)
        if r.status_code != 200:
            return ""
        for grp in r.json().get("results", []):
            if "player" in str(grp.get("type", "")).lower():
                for c in grp.get("contents", []):
                    if c.get("subtitle"):
                        return c["subtitle"]
        return ""
    except requests.exceptions.RequestException:
        return ""


def fetch_espn_defense_context(
    player_name: str,
    market_key:  str,
    sport_key:   str,
    home_team:   str,
    away_team:   str,
) -> str:
    """Opponent-matchup context for the AI analysis, from free data.

    Finds the player's team, identifies the opponent, and reports how the
    opponent has performed recently — the opponent's runs/points/goals allowed
    per game, plus (MLB) the opposing starting pitcher, which is the single
    biggest factor in a batter's night. Returns '' on any failure so it never
    blocks the main analysis. Results reuse the cached recent-form data.
    """
    try:
        if not home_team or not away_team:
            return ''
        player_team = _player_team(player_name, sport_key)
        if not player_team:
            return ''

        if _words_overlap(player_team, home_team):
            opponent, player_is_home = away_team, True
        elif _words_overlap(player_team, away_team):
            opponent, player_is_home = home_team, False
        else:
            return ''

        # A pitcher faces the opposing lineup; a hitter (and non-MLB scorer)
        # faces the opposing defense/pitching. That flips which stats matter.
        is_pitcher = market_key.startswith("player_pitcher")
        parts = []

        # MLB hitter: the opposing starting pitcher is the dominant factor.
        if sport_key == "baseball_mlb" and not is_pitcher:
            sp_map, _ = get_probable_pitchers(sport_key)
            m = sp_map.get((_norm_team(away_team), _norm_team(home_team)), {})
            if player_is_home:
                sp_name, sp_era = m.get("away_sp_name"), m.get("away_sp_era")
            else:
                sp_name, sp_era = m.get("home_sp_name"), m.get("home_sp_era")
            if sp_name:
                era = f" ({sp_era:.2f} ERA)" if sp_era is not None else ""
                parts.append(f"opposing starter {sp_name}{era}")

        # The opponent's recent form: how much the lineup a pitcher faces SCORES,
        # or how much the defense a hitter/scorer faces ALLOWS.
        cfg = GAME_MODEL_CONFIG.get(sport_key)
        if cfg:
            ratings, lg = get_team_ratings(sport_key)
            opp = _match_rating(opponent, ratings)
            if opp and opp["games"] >= 3:
                if is_pitcher:
                    parts.append(f"{opponent} score {opp['rs']:.1f} {cfg['unit']}/gm recently"
                                 f" (league avg {lg:.1f})")
                else:
                    parts.append(f"{opponent} allow {opp['ra']:.1f} {cfg['unit']}/gm recently"
                                 f" (league avg {lg:.1f})")

        if not parts:
            return ''
        return f"🛡️ OPPONENT MATCHUP ({opponent}): " + "; ".join(parts)
    except Exception:
        return ''


# ─────────────────────────────────────────
# PROJECTION ENGINE
# Generate our own projection for a player's stat from their recent games.
# Data source is per-sport: MLB uses MLB's official free Stats API (reliable);
# other sports use ESPN (whose unofficial endpoints are currently flaky and
# need their own verified source before those seasons matter).
# ─────────────────────────────────────────

MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"

# market_key → (statsapi stat group, spec). spec is a field name, a tuple of
# fields to SUM (e.g. hits+runs+RBIs), or "SINGLES" (derived: H − 2B − 3B − HR).
MLB_STAT_MAP = {
    # Batting
    "player_batter_hits":          ("hitting", "hits"),
    "player_batter_total_bases":   ("hitting", "totalBases"),
    "player_batter_home_runs":     ("hitting", "homeRuns"),
    "player_batter_rbis":          ("hitting", "rbi"),
    "player_batter_runs_scored":   ("hitting", "runs"),
    "player_batter_singles":       ("hitting", "SINGLES"),
    "player_batter_doubles":       ("hitting", "doubles"),
    "player_batter_triples":       ("hitting", "triples"),
    "player_batter_walks":         ("hitting", "baseOnBalls"),
    "player_batter_stolen_bases":  ("hitting", "stolenBases"),
    "player_batter_strikeouts":    ("hitting", "strikeOuts"),
    "player_hits_runs_rbis":       ("hitting", ("hits", "runs", "rbi")),
    # Pitching
    "player_pitcher_strikeouts":   ("pitching", "strikeOuts"),
    "player_pitcher_hits_allowed": ("pitching", "hits"),
    "player_pitcher_outs":         ("pitching", "outs"),
    "player_pitcher_walks_allowed":("pitching", "baseOnBalls"),
    "player_pitcher_earned_runs":  ("pitching", "earnedRuns"),
}


def _mlb_stat_value(stat: dict, spec):
    """Read one per-game value from a statsapi split, handling derived specs."""
    if spec == "SINGLES":
        parts = [stat.get(k) for k in ("hits", "doubles", "triples", "homeRuns")]
        if any(p is None for p in parts):
            return None
        h, d, t, hr = parts
        return h - d - t - hr
    if isinstance(spec, tuple):
        parts = [stat.get(k) for k in spec]
        return sum(parts) if all(p is not None for p in parts) else None
    return stat.get(spec)


def fetch_recent_stat_values(player_name: str, market_key: str, sport_key: str,
                             limit: int = 10) -> list:
    """Return a player's recent numeric values for a stat (oldest → newest).

    Dispatches to the right free data source per sport. Returns [] on failure.
    """
    if sport_key == "baseball_mlb":
        return _fetch_mlb_stat_values(player_name, market_key, sport_key, limit)
    return _fetch_espn_stat_values(player_name, market_key, sport_key, limit)


def _fetch_mlb_stat_values(player_name: str, market_key: str, sport_key: str,
                           limit: int = 10) -> list:
    """Recent per-game values from MLB's official free Stats API (no key needed)."""
    try:
        mapping = MLB_STAT_MAP.get(market_key)
        if not mapping:
            return []
        group, spec = mapping

        # 1. Resolve the player's MLB id by name
        r = requests.get(f"{MLB_STATS_BASE}/people/search",
                         params={"names": player_name}, headers=ESPN_HDR, timeout=6)
        if r.status_code != 200:
            return []
        people = r.json().get("people", [])
        if not people:
            return []
        pid = people[0]["id"]

        # 2. Pull this season's game log for the right stat group
        r = requests.get(
            f"{MLB_STATS_BASE}/people/{pid}/stats",
            params={"stats": "gameLog", "group": group, "season": date.today().year},
            headers=ESPN_HDR, timeout=8,
        )
        if r.status_code != 200:
            return []
        stat_blocks = r.json().get("stats", [])
        splits = stat_blocks[0].get("splits", []) if stat_blocks else []

        # 3. Pull the per-game value (splits are already oldest → newest)
        values = []
        for s in splits:
            v = _mlb_stat_value(s.get("stat", {}), spec)
            if v is not None:
                try:
                    values.append(float(v))
                except (ValueError, TypeError):
                    pass
        return values[-limit:] if limit else values
    except Exception:
        return []


# ESPN game-log data layer (NBA / NFL / NHL). MLB uses its own official API above.
# The old "/athletes?search=" endpoint returns 404; these two work:
ESPN_SEARCH_URL  = "https://site.web.api.espn.com/apis/search/v2"
ESPN_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/{sport}/{league}/athletes/{aid}/gamelog"

# market_key → the machine-readable column name in the gamelog's "names" list.
# Compound columns (e.g. "made-attempted") are matched by prefix and the value's
# first number (the "made" side) is used.
ESPN_GAMELOG_STAT = {
    # NBA
    'player_points':        'points',
    'player_rebounds':      'totalRebounds',
    'player_assists':       'assists',
    'player_threes':        'threePointFieldGoalsMade',   # compound "2-6" → 2
    'player_blocks':        'blocks',
    'player_steals':        'steals',
    # NFL
    'player_pass_yds':      'passingYards',
    'player_pass_tds':      'passingTouchdowns',
    'player_rush_yds':      'rushingYards',
    'player_reception_yds': 'receivingYards',
    'player_receptions':    'receptions',
    # NHL
    'player_goals':         'goals',
    'player_shots_on_goal': 'shotsTotal',
}


def _espn_athlete_id(player_name: str) -> str | None:
    """Resolve a player name to their ESPN athlete id via the site search API."""
    r = requests.get(ESPN_SEARCH_URL, params={"query": player_name, "limit": 5},
                     headers=ESPN_HDR, timeout=6)
    if r.status_code != 200:
        return None
    for group in r.json().get("results", []):
        if "player" in str(group.get("type", "")).lower():
            for c in group.get("contents", []):
                # Player links look like ".../id/1966/lebron-james" or ".../id/3895074"
                m = re.search(r"/id/(\d+)", (c.get("link") or {}).get("web", ""))
                if m:
                    return m.group(1)
    return None


def _fetch_espn_stat_values(player_name: str, market_key: str, sport_key: str,
                            limit: int = 10) -> list:
    """Recent per-game values for NBA/NFL/NHL from ESPN's free game-log API.

    Returns values oldest → newest (matching the MLB fetcher), or [] on any
    failure — never raises.
    """
    try:
        sport_info = ESPN_SPORT_MAP.get(sport_key)
        target = ESPN_GAMELOG_STAT.get(market_key)
        if not sport_info or not target:
            return []
        sport, league = sport_info

        aid = _espn_athlete_id(player_name)
        if not aid:
            return []

        r = requests.get(
            ESPN_GAMELOG_URL.format(sport=sport, league=league, aid=aid),
            headers=ESPN_HDR, timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()

        # The "names" list labels each column; find our stat's index.
        names = data.get("names") or []
        idx = next((i for i, n in enumerate(names)
                    if n == target or n.startswith(target + '-')), None)
        if idx is None:
            return []
        compound = '-' in names[idx]   # e.g. "made-attempted" → take the made side

        # Flatten every game across season types. ESPN lists them newest-first
        # (both across blocks and within each), so we reverse to oldest → newest.
        values = []
        for season in data.get("seasonTypes", []):
            for cat in season.get("categories", []):
                for ev in cat.get("events", []):
                    stats = ev.get("stats", [])
                    if idx < len(stats):
                        raw = str(stats[idx])
                        if compound:
                            raw = raw.split('-')[0]
                        try:
                            values.append(float(raw))
                        except ValueError:
                            pass
        values.reverse()
        return values[-limit:] if limit else values
    except Exception:
        return []


def weighted_projection(values: list, half_life: float = 3.0) -> float | None:
    """Recency-weighted average of recent game values (oldest → newest).

    Recent games count more, so hot/cold streaks move the projection. `half_life`
    is how many games back a game's weight halves (3 = a game 3 games ago counts
    half as much as the latest). Returns None for an empty list.
    """
    if not values:
        return None
    n = len(values)
    weights = [0.5 ** ((n - 1 - i) / half_life) for i in range(n)]
    total = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total


def generate_projection(player_name: str, market_key: str, sport_key: str,
                        min_games: int = 3) -> dict:
    """Produce PropJunkie's own projection for a player's stat.

    Returns a dict with the projection plus context so the UI can be honest
    about confidence:
        projection:      the projected value (None if not enough data)
        games_used:      how many recent games fed the projection
        recent_values:   those game values (oldest → newest)
        low_confidence:  True if the sample is thin
        reason:          why there's no projection, if applicable
    """
    values = fetch_recent_stat_values(player_name, market_key, sport_key, limit=10)
    if len(values) < min_games:
        return {
            "projection": None,
            "games_used": len(values),
            "recent_values": values,
            "low_confidence": True,
            "reason": "Not enough recent games to project this stat yet.",
        }
    proj = weighted_projection(values)
    return {
        "projection": round(proj, 1),
        "games_used": len(values),
        "recent_values": values,
        "low_confidence": len(values) < 5,
        "reason": None,
    }


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


# Suggested std_dev_pct defaults by (sport_key, market_key).
#
# Keyed by sport AS WELL AS market because NBA and NHL share the market keys
# `player_points` and `player_assists` — a market-only dict silently collapsed
# those to the NHL values, so NBA points/assists were scored with ~double the
# intended volatility. Look these up via get_std_dev_pct().
STD_DEV_DEFAULTS = {
    # NBA
    ("basketball_nba", "player_points"):                   0.28,
    ("basketball_nba", "player_rebounds"):                 0.32,
    ("basketball_nba", "player_assists"):                  0.35,
    ("basketball_nba", "player_threes"):                   0.55,
    ("basketball_nba", "player_blocks"):                   0.60,
    ("basketball_nba", "player_steals"):                   0.65,
    ("basketball_nba", "player_points_rebounds_assists"):  0.22,
    # NFL
    ("americanfootball_nfl", "player_pass_yds"):           0.26,
    ("americanfootball_nfl", "player_pass_tds"):           0.65,
    ("americanfootball_nfl", "player_rush_yds"):           0.40,
    ("americanfootball_nfl", "player_reception_yds"):      0.45,
    ("americanfootball_nfl", "player_receptions"):         0.40,
    ("americanfootball_nfl", "player_anytime_td"):         0.70,
    # MLB — pitchers
    ("baseball_mlb", "player_pitcher_strikeouts"):         0.30,
    ("baseball_mlb", "player_pitcher_outs"):               0.28,
    ("baseball_mlb", "player_pitcher_hits_allowed"):       0.35,
    ("baseball_mlb", "player_pitcher_walks_allowed"):      0.60,
    ("baseball_mlb", "player_pitcher_earned_runs"):        0.75,
    # MLB — batters (rare events = high variance)
    ("baseball_mlb", "player_batter_home_runs"):           0.90,
    ("baseball_mlb", "player_batter_hits"):                0.45,
    ("baseball_mlb", "player_batter_total_bases"):         0.50,
    ("baseball_mlb", "player_hits_runs_rbis"):             0.45,
    ("baseball_mlb", "player_batter_rbis"):                0.70,
    ("baseball_mlb", "player_batter_runs_scored"):         0.65,
    ("baseball_mlb", "player_batter_singles"):             0.60,
    ("baseball_mlb", "player_batter_doubles"):             0.85,
    ("baseball_mlb", "player_batter_triples"):             0.95,
    ("baseball_mlb", "player_batter_walks"):               0.70,
    ("baseball_mlb", "player_batter_strikeouts"):          0.45,
    ("baseball_mlb", "player_batter_stolen_bases"):        0.80,
    # NHL
    ("icehockey_nhl", "player_shots_on_goal"):             0.35,
    ("icehockey_nhl", "player_goals"):                     0.80,
    ("icehockey_nhl", "player_assists"):                   0.70,
    ("icehockey_nhl", "player_points"):                    0.55,
}

# Fallback volatility when a (sport, market) pair isn't in the table above.
DEFAULT_STD_DEV_PCT = 0.25


def get_std_dev_pct(sport_key: str, market_key: str) -> float:
    """Return the modelled game-to-game volatility for a (sport, market) pair.

    Falls back to DEFAULT_STD_DEV_PCT for anything not explicitly listed.
    """
    return STD_DEV_DEFAULTS.get((sport_key, market_key), DEFAULT_STD_DEV_PCT)


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

    Higher American odds are always the better payout for the bettor
    (+150 > +120 > -110 > -130), so the best line is simply the max.
    """
    if not props:
        return None
    key = "over_odds" if side == "over" else "under_odds"
    return max(props, key=lambda p: p[key])


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
    # Use stat-specific std dev if not overridden.
    # Keyed by (sport, market) so NBA and NHL points/assists don't collide.
    if std_dev_pct is None:
        std_dev_pct = get_std_dev_pct(sport_key, market_key)

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
        if "error" not in result and not result.get("no_lines"):
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
            "No fluff. Max 4 sentences. "
            "FORMATTING RULES: No markdown tables (no pipe characters). "
            "Use plain sentences only. Bold key numbers with **value**."
        ),
        "casual": (
            "You are a friendly sports betting guide. Explain what the numbers mean "
            "in plain English — is this a good bet? Why or why not? "
            "Reference the player's actual recent stats from the context block when available. "
            "Keep it under 5 sentences. "
            "FORMATTING RULES: No markdown tables (no pipe characters). Plain text only."
        ),
        "detailed": (
            "You are a professional sports betting analyst. Give a thorough breakdown: "
            "what the model projects vs the player's real recent stats (cite specific numbers from the context block), "
            "what the market implies, where the edge comes from, "
            "which book has the best line, and any caveats. Use 6–8 sentences. "
            "FORMATTING RULES: No markdown tables (no pipe characters). "
            "Use bold (**value**) for key numbers. Use dashes (—) for comparisons. "
            "Never write pipe-separated tables."
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
# GAME MODEL — value picks (moneyline + total)
#
# A lightweight, recency-based model built entirely on free ESPN final scores.
# For each upcoming game we estimate each team's expected runs/points from its
# recent offense and the opponent's recent defense (scaled to league average),
# then compare our projection to the market line to flag value. This is a
# transparent "lean," not a sharp model — it uses recent form only, no injuries,
# pitchers, rest, or travel. Keep the honesty in any UI copy.
# ─────────────────────────────────────────

# Per-sport tuning. pyth_exp = Pythagorean win-probability exponent (standard
# values: MLB 1.83, NBA 13.9, NFL 2.37, NHL 2.0). home_adv and thresholds are
# in that sport's scoring unit (runs/points/goals).
GAME_MODEL_CONFIG = {
    # market_weight: how much to trust the market vs our model (0.6 = market is
    #   the prior, model nudges it). ml_gap_cap: if the raw model/market win-prob
    #   gap exceeds this, the model is likely missing info (e.g. the starting
    #   pitcher) — suppress the pick. total_edge_cap: max believable total edge.
    "baseball_mlb":         {"lookback_days": 18, "home_adv": 0.15, "pyth_exp": 1.83, "regress_games": 5,
                             "min_games": 5, "total_threshold": 0.5, "ml_threshold": 0.03, "unit": "runs",
                             "market_weight": 0.6, "ml_gap_cap": 0.18, "total_edge_cap": 2.5, "sp_weight": 0.55,
                             # MLB run line is ±1.5 with heavy juice; only lean on a clearly
                             # mispriced margin so we're selective, not "always take the dog +1.5".
                             "spread_threshold": 2.0, "spread_edge_cap": 3.0, "form_half_life": 10},
    "basketball_nba":       {"lookback_days": 24, "home_adv": 2.5,  "pyth_exp": 13.9, "regress_games": 4,
                             "min_games": 5, "total_threshold": 3.0, "ml_threshold": 0.03, "unit": "pts",
                             "market_weight": 0.6, "ml_gap_cap": 0.18, "total_edge_cap": 14,
                             "spread_threshold": 2.5, "spread_edge_cap": 12, "form_half_life": 14},
    "americanfootball_nfl": {"lookback_days": 45, "home_adv": 2.0,  "pyth_exp": 2.37, "regress_games": 3,
                             "min_games": 3, "total_threshold": 2.5, "ml_threshold": 0.03, "unit": "pts",
                             "market_weight": 0.6, "ml_gap_cap": 0.18, "total_edge_cap": 12,
                             "spread_threshold": 2.0, "spread_edge_cap": 10, "form_half_life": 28},
    "icehockey_nhl":        {"lookback_days": 21, "home_adv": 0.3,  "pyth_exp": 2.0, "regress_games": 5,
                             "min_games": 5, "total_threshold": 0.6, "ml_threshold": 0.03, "unit": "goals",
                             "market_weight": 0.6, "ml_gap_cap": 0.18, "total_edge_cap": 2.5,
                             "spread_threshold": 0.75, "spread_edge_cap": 3.0, "form_half_life": 14},
}

# MLB park run-environment factors (≈1.0 neutral): hitter-friendly parks inflate
# the total, pitcher-friendly ones suppress it. Applied to the projected total
# only — a park lifts/dampens both sides, so it barely moves the margin. Static,
# free, and a well-established driver of run scoring. Unlisted parks = 1.0.
MLB_PARK_FACTORS = {
    "Colorado Rockies": 1.15, "Boston Red Sox": 1.05, "Cincinnati Reds": 1.06,
    "Kansas City Royals": 1.03, "Baltimore Orioles": 1.03, "Arizona Diamondbacks": 1.02,
    "Philadelphia Phillies": 1.02, "Texas Rangers": 1.02, "Chicago Cubs": 1.01,
    "San Diego Padres": 0.91, "San Francisco Giants": 0.92, "Miami Marlins": 0.92,
    "Seattle Mariners": 0.93, "New York Mets": 0.95, "Tampa Bay Rays": 0.95,
    "Detroit Tigers": 0.95, "Cleveland Guardians": 0.96, "St. Louis Cardinals": 0.96,
    "Oakland Athletics": 0.96, "Athletics": 0.97,
}


def fetch_recent_results(sport_key: str, lookback_days: int) -> list:
    """Final scores from ESPN over the last N days (free, one call per day).

    Returns [{'home','away','home_score','away_score'}], finals only. [] on
    failure — never raises.
    """
    sport_info = ESPN_SPORT_MAP.get(sport_key)
    if not sport_info:
        return []
    sport, league = sport_info
    url = ESPN_SCOREBOARD.format(sport=sport, league=league)

    results = []
    for i in range(1, int(lookback_days) + 1):
        day = (date.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            resp = requests.get(url, params={"dates": day}, headers=ESPN_HDR, timeout=8)
            if resp.status_code != 200:
                continue
            for ev in resp.json().get("events", []):
                if ev.get("status", {}).get("type", {}).get("state") != "post":
                    continue
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                away = next((c for c in competitors if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue
                home_name = home.get("team", {}).get("displayName") or ""
                away_name = away.get("team", {}).get("displayName") or ""
                # Skip exhibitions (e.g. the All-Star Game) — not real team form.
                if "All-Star" in home_name or "All-Star" in away_name:
                    continue
                try:
                    hs = int(home.get("score"))
                    as_ = int(away.get("score"))
                except (TypeError, ValueError):
                    continue
                results.append({
                    "home":       home_name,
                    "away":       away_name,
                    "home_score": hs,
                    "away_score": as_,
                    "days_ago":   i,   # for recency weighting
                })
        except requests.exceptions.RequestException:
            continue
    return results


def compute_team_ratings(results: list, half_life: float = None) -> tuple:
    """From recent games, build per-team scoring rates + the league average.

    Returns (ratings, lg_avg) where ratings[team] = {'rs','ra','games'}:
    rs = avg scored per game, ra = avg allowed per game. lg_avg = average points
    a team scores per game across the sample (the model's baseline).

    When ``half_life`` (in days) is given, games are recency-weighted — a game
    ``half_life`` days old counts half as much — so hot/cold form shows through.
    Flat average otherwise; 'games' stays the raw count for the min-games gate.
    """
    stats = {}
    total_w_points, total_w = 0.0, 0.0
    for g in results:
        w = 0.5 ** (g.get("days_ago", 0) / half_life) if half_life else 1.0
        for team, sf, sa in ((g["home"], g["home_score"], g["away_score"]),
                             (g["away"], g["away_score"], g["home_score"])):
            d = stats.setdefault(team, {"scored": 0.0, "allowed": 0.0, "w": 0.0, "games": 0})
            d["scored"] += sf * w
            d["allowed"] += sa * w
            d["w"] += w
            d["games"] += 1
            total_w_points += sf * w
            total_w += w
    lg_avg = (total_w_points / total_w) if total_w else 0.0
    ratings = {t: {"rs": d["scored"] / d["w"],
                   "ra": d["allowed"] / d["w"],
                   "games": d["games"]}
               for t, d in stats.items() if d["w"] > 0}
    return ratings, lg_avg


def project_game(home: str, away: str, ratings: dict, lg_avg: float, cfg: dict,
                 home_sp_era: float = None, away_sp_era: float = None,
                 avg_sp_era: float = None):
    """Project a single game's total and home win probability. None if either
    team lacks enough recent games.

    For MLB, the opposing starting pitcher (home_sp_era / away_sp_era, relative
    to the slate's average starter avg_sp_era) is blended into each team's run
    prevention — the single biggest factor a team-form model otherwise misses.
    """
    rh = ratings.get(home)
    ra = ratings.get(away)
    if not rh or not ra or lg_avg <= 0:
        return None
    if rh["games"] < cfg["min_games"] or ra["games"] < cfg["min_games"]:
        return None

    # Regress each rate toward league average by sample size, so a few blowouts
    # (or a thin sample) can't produce absurd projections. K "phantom" league-
    # average games are mixed in; more real games → less shrink.
    K = cfg.get("regress_games", 4.0)

    def _shrink(rate, games):
        return (rate * games + K * lg_avg) / (games + K)

    home_rs = _shrink(rh["rs"], rh["games"])
    home_ra = _shrink(rh["ra"], rh["games"])
    away_rs = _shrink(ra["rs"], ra["games"])
    away_ra = _shrink(ra["ra"], ra["games"])

    # Blend the opposing starter into each team's run prevention. A starter's ERA
    # relative to the average starter (centered at 1.0) scales league-average runs;
    # the rest of the game (bullpen/defense) stays the team rate. w = starter share.
    w = cfg.get("sp_weight", 0)
    if w and avg_sp_era and avg_sp_era > 0:
        if away_sp_era:   # home team faces the away starter
            away_ra = w * (away_sp_era / avg_sp_era) * lg_avg + (1 - w) * away_ra
        if home_sp_era:   # away team faces the home starter
            home_ra = w * (home_sp_era / avg_sp_era) * lg_avg + (1 - w) * home_ra

    # Each team's expected score = its offense scaled by the opponent's defense,
    # relative to league average (a log5-style matchup adjustment).
    home_exp = home_rs * away_ra / lg_avg
    away_exp = away_rs * home_ra / lg_avg
    proj_total = home_exp + away_exp

    # Home edge shifts win probability (redistribute, leaving the total intact).
    adv = cfg["home_adv"] / 2.0
    k = cfg["pyth_exp"]
    he = max(home_exp + adv, 0.1)
    ae = max(away_exp - adv, 0.1)
    home_win_prob = he ** k / (he ** k + ae ** k)

    return {
        "proj_total":    proj_total,
        "home_exp":      home_exp,
        "away_exp":      away_exp,
        # Home margin including home advantage (positive = home favored).
        "proj_margin":   (home_exp - away_exp) + cfg["home_adv"],
        "home_win_prob": home_win_prob,
        "min_games":     min(rh["games"], ra["games"]),
    }


MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_PEOPLE_URL   = "https://statsapi.mlb.com/api/v1/people"


def _norm_team(name: str) -> str:
    """Normalize a team name for matching across ESPN and MLB Stats API."""
    return (name or "").lower().replace(".", "").strip()


def fetch_probable_pitchers(sport_key: str) -> tuple:
    """Probable starting pitchers + ERAs for MLB's next few days (free statsapi).

    Returns (matchup_map, avg_starter_era) where matchup_map is keyed by
    (norm(away_team), norm(home_team)) → {home_sp_era, away_sp_era, names...}.
    avg_starter_era is the mean ERA across the fetched starters — the baseline
    the model normalizes each pitcher against. ({}, None) for non-MLB or failure.
    """
    if sport_key != "baseball_mlb":
        return {}, None
    try:
        matchups = {}   # (away, home) → {ids + names}
        pids = set()
        for i in range(0, 3):
            day = (date.today() + timedelta(days=i)).strftime("%Y-%m-%d")
            r = requests.get(MLB_SCHEDULE_URL,
                             params={"sportId": 1, "date": day, "hydrate": "probablePitcher"},
                             headers=ESPN_HDR, timeout=8)
            if r.status_code != 200:
                continue
            for block in r.json().get("dates", []):
                for g in block.get("games", []):
                    teams = g.get("teams", {})
                    home = teams.get("home", {}); away = teams.get("away", {})
                    hname = home.get("team", {}).get("name")
                    aname = away.get("team", {}).get("name")
                    if not hname or not aname:
                        continue
                    hp = (home.get("probablePitcher") or {})
                    ap = (away.get("probablePitcher") or {})
                    key = (_norm_team(aname), _norm_team(hname))
                    matchups.setdefault(key, {
                        "home_sp_id":   hp.get("id"),
                        "away_sp_id":   ap.get("id"),
                        "home_sp_name": hp.get("fullName"),
                        "away_sp_name": ap.get("fullName"),
                    })
                    if hp.get("id"): pids.add(hp["id"])
                    if ap.get("id"): pids.add(ap["id"])

        if not pids:
            return {}, None

        # One batched call for every starter's season ERA.
        eras = {}
        r = requests.get(MLB_PEOPLE_URL, params={
            "personIds": ",".join(str(p) for p in pids),
            "hydrate": f"stats(group=[pitching],type=[season],season={date.today().year})",
        }, headers=ESPN_HDR, timeout=10)
        if r.status_code == 200:
            for p in r.json().get("people", []):
                era = None
                for s in p.get("stats", []):
                    for split in s.get("splits", []):
                        try:
                            era = float(split.get("stat", {}).get("era"))
                        except (TypeError, ValueError):
                            pass
                if era is not None:
                    eras[p.get("id")] = era

        if not eras:
            return {}, None
        avg_era = sum(eras.values()) / len(eras)

        # Attach ERAs to each matchup.
        for key, m in matchups.items():
            m["home_sp_era"] = eras.get(m.get("home_sp_id"))
            m["away_sp_era"] = eras.get(m.get("away_sp_id"))
        return matchups, avg_era
    except requests.exceptions.RequestException:
        return {}, None


# Recent-form inputs move slowly, so cache them for reuse across the picks
# endpoint and the prop-analysis opponent context (which would otherwise re-scan
# ~3 weeks of games on every request).
_ratings_cache: dict = {}
_pitchers_cache: dict = {}
_MODEL_CACHE_TTL = 3600   # seconds (1h)


def get_team_ratings(sport_key: str) -> tuple:
    """Cached (ratings, lg_avg) for a sport. ({}, 0.0) if unsupported."""
    cfg = GAME_MODEL_CONFIG.get(sport_key)
    if not cfg:
        return {}, 0.0
    c = _ratings_cache.get(sport_key)
    if c and time.time() - c["ts"] < _MODEL_CACHE_TTL:
        return c["ratings"], c["lg"]
    ratings, lg = compute_team_ratings(fetch_recent_results(sport_key, cfg["lookback_days"]),
                                       half_life=cfg.get("form_half_life"))
    if ratings:
        _ratings_cache[sport_key] = {"ratings": ratings, "lg": lg, "ts": time.time()}
    return ratings, lg


def get_probable_pitchers(sport_key: str) -> tuple:
    """Cached probable-pitcher map + average starter ERA (MLB)."""
    c = _pitchers_cache.get(sport_key)
    if c and time.time() - c["ts"] < _MODEL_CACHE_TTL:
        return c["map"], c["avg"]
    m, avg = fetch_probable_pitchers(sport_key)
    if m:
        _pitchers_cache[sport_key] = {"map": m, "avg": avg, "ts": time.time()}
    return m, avg


def generate_game_picks(sport_key: str) -> dict:
    """Value picks for today's slate, keyed by game id.

    picks[game_id] = {'totals': {...}, 'h2h': {...}, 'min_games', 'confidence'}
    Only games where the model disagrees with the market by more than the
    per-sport threshold get a pick. Returns {} if the sport isn't supported or
    there isn't enough recent data.
    """
    cfg = GAME_MODEL_CONFIG.get(sport_key)
    if not cfg:
        return {}

    games = get_game_lines(sport_key)
    if not games:
        return {}
    ratings, lg_avg = compute_team_ratings(fetch_recent_results(sport_key, cfg["lookback_days"]),
                                           half_life=cfg.get("form_half_life"))
    if not ratings:
        return {}

    # Starting pitchers (MLB) — the model's biggest single-game input.
    sp_map, avg_sp_era = ({}, None)
    if cfg.get("sp_weight"):
        sp_map, avg_sp_era = fetch_probable_pitchers(sport_key)

    picks = {}
    for g in games:
        sp = sp_map.get((_norm_team(g["away_team"]), _norm_team(g["home_team"])), {})
        proj = project_game(g["home_team"], g["away_team"], ratings, lg_avg, cfg,
                            home_sp_era=sp.get("home_sp_era"),
                            away_sp_era=sp.get("away_sp_era"),
                            avg_sp_era=avg_sp_era)
        if not proj:
            continue
        entry = {}

        # ── Total: model projection vs the posted line ──
        # Flag a lean when the model differs by more than the threshold, but not
        # by an implausible amount (that signals model noise, not a real edge).
        # For MLB, scale the total by the home park's run environment.
        park = MLB_PARK_FACTORS.get(g["home_team"], 1.0) if sport_key == "baseball_mlb" else 1.0
        proj_total = proj["proj_total"] * park
        totals = g.get("totals")
        if totals and totals.get("line") is not None:
            line = totals["line"]
            diff = proj_total - line
            if cfg["total_threshold"] <= abs(diff) <= cfg["total_edge_cap"]:
                side = "Over" if diff > 0 else "Under"
                entry["totals"] = {
                    "pick":  f"{side} {line}",
                    "side":  side.lower(),
                    "model": round(proj_total, 1),
                    "line":  line,
                    "edge":  round(abs(diff), 1),
                    "unit":  cfg["unit"],
                }

        # ── Spread / run line: model margin vs the posted spread ──
        # Home covers when the projected margin beats the line (margin + home_line
        # > 0). We lean the side the model has covering by more than the threshold,
        # capped so noise doesn't masquerade as an edge. (For MLB's ±1.5 run line
        # this rarely fires — margins are small — which is honest.)
        spreads = g.get("spreads")
        if spreads and spreads.get("home_line") is not None:
            home_line = spreads["home_line"]
            cover_edge = proj["proj_margin"] + home_line   # + = home covers
            if cfg["spread_threshold"] <= abs(cover_edge) <= cfg["spread_edge_cap"]:
                if cover_edge > 0:
                    team, shown_line, side = g["home_team"], home_line, "home"
                else:
                    team, shown_line, side = g["away_team"], spreads.get("away_line"), "away"
                sign = "+" if (shown_line is not None and shown_line > 0) else ""
                entry["spreads"] = {
                    "pick":         f"{team.split()[-1]} {sign}{shown_line}",
                    "side":         side,
                    "line":         home_line,   # store home_line; grading is home-relative
                    "model_margin": round(proj["proj_margin"], 1),
                    "edge":         round(abs(cover_edge), 1),
                    "unit":         cfg["unit"],
                }

        # ── Moneyline: a humble lean anchored to the market ──
        # The market prices in things our free model can't (pitchers, injuries),
        # so we treat it as the prior and only nudge it. If the raw model wildly
        # disagrees, we're missing info — suppress rather than pretend it's value.
        h2h = g.get("h2h")
        if h2h and h2h.get("home") is not None and h2h.get("away") is not None:
            mkt_home, mkt_away = remove_vig(h2h["home"], h2h["away"])
            model_home = proj["home_win_prob"]
            if abs(model_home - mkt_home) <= cfg["ml_gap_cap"]:
                w = cfg["market_weight"]
                blended_home = w * mkt_home + (1 - w) * model_home
                home_edge = blended_home - mkt_home
                away_edge = (1 - blended_home) - mkt_away
                if home_edge >= cfg["ml_threshold"] and home_edge >= away_edge:
                    entry["h2h"] = {
                        "pick":        f"{g['home_team'].split()[-1]} ML",
                        "side":        "home",
                        "model_prob":  round(blended_home, 3),
                        "market_prob": round(mkt_home, 3),
                        "edge":        round(home_edge * 100, 1),
                    }
                elif away_edge >= cfg["ml_threshold"]:
                    entry["h2h"] = {
                        "pick":        f"{g['away_team'].split()[-1]} ML",
                        "side":        "away",
                        "model_prob":  round(1 - blended_home, 3),
                        "market_prob": round(mkt_away, 3),
                        "edge":        round(away_edge * 100, 1),
                    }

        if entry:
            mg = proj["min_games"]
            entry["min_games"] = mg
            entry["confidence"] = "high" if mg >= 10 else "medium" if mg >= 7 else "low"
            # Game metadata so the pick is self-contained (used for accuracy tracking).
            entry["home"]     = g["home_team"]
            entry["away"]     = g["away_team"]
            entry["commence"] = g["commence_time"]
            if sp.get("home_sp_name") and sp.get("away_sp_name"):
                entry["pitchers"] = {
                    "home":     sp["home_sp_name"], "home_era": sp.get("home_sp_era"),
                    "away":     sp["away_sp_name"], "away_era": sp.get("away_sp_era"),
                }
            picks[g["id"]] = entry

    return picks


def _is_today_et(iso: str) -> bool:
    """True if an ISO commence-time falls on the current US-Eastern date."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(ESPN_TZ)
        return dt.date() == datetime.now(ESPN_TZ).date()
    except (ValueError, TypeError):
        return False


def generate_prop_board(sport_key: str) -> list:
    """A board of player-prop projections for today's games (100% free data).

    v1 (MLB): each game's probable starting pitchers → a strikeout projection
    with recent form. No betting line or edge (we have no free prop lines), so
    the card shows PropJunkie's projection + recent games only — never a
    fabricated edge or hit-rate. Sorted by projection, highest first.
    """
    if sport_key != "baseball_mlb":
        return []
    games = [g for g in get_game_lines(sport_key) if _is_today_et(g.get("commence_time"))]
    if not games:
        return []
    sp_map, _ = get_probable_pitchers(sport_key)

    cards, seen = [], set()
    for g in games:
        sp = sp_map.get((_norm_team(g["away_team"]), _norm_team(g["home_team"])))
        if not sp:
            continue
        for who, team, opp in (("away", g["away_team"], g["home_team"]),
                               ("home", g["home_team"], g["away_team"])):
            name = sp.get(f"{who}_sp_name")
            if not name or name in seen:
                continue
            seen.add(name)
            proj = generate_projection(name, "player_pitcher_strikeouts", sport_key)
            if proj.get("projection") is None:
                continue
            vals = proj.get("recent_values") or []
            pid = sp.get(f"{who}_sp_id")
            cards.append({
                "player":         name,
                "headshot":       (f"https://midfield.mlbstatic.com/v1/people/{pid}/spots/120"
                                   if pid else None),
                "role":           "SP",
                "market":         "Strikeouts",
                "market_key":     "player_pitcher_strikeouts",
                "team":           team,
                "opponent":       opp,
                "matchup":        f"{team.split()[-1]} vs {opp.split()[-1]}",
                "commence_time":  g["commence_time"],
                "game_id":        g["id"],
                "projection":     proj["projection"],
                "recent":         [int(v) if float(v).is_integer() else v for v in vals[-5:]],
                "l5_avg":         round(sum(vals[-5:]) / len(vals[-5:]), 1) if vals else None,
                "l10_avg":        round(sum(vals[-10:]) / len(vals[-10:]), 1) if vals else None,
                "games_used":     proj["games_used"],
                "low_confidence": proj["low_confidence"],
                "era":            sp.get(f"{who}_sp_era"),
            })
    cards.sort(key=lambda c: c["projection"], reverse=True)
    return cards


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
