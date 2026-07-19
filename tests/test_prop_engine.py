"""
Unit tests for prop_engine's pure math + analysis pipeline.

These tests are fully offline — the only external call in the analysis path
(get_event_props → The Odds API) is monkeypatched, so no API keys or network
access are required.

Run:  pytest -q
"""

import math

import pytest

import prop_engine as pe


# ─────────────────────────────────────────
# ODDS MATH
# ─────────────────────────────────────────

class TestAmericanToProb:
    def test_even_money(self):
        assert pe.american_to_prob(100) == pytest.approx(0.5)

    def test_favorite(self):
        # -110 → 110 / 210
        assert pe.american_to_prob(-110) == pytest.approx(110 / 210)

    def test_underdog(self):
        # +150 → 100 / 250
        assert pe.american_to_prob(150) == pytest.approx(0.4)

    def test_heavy_favorite(self):
        assert pe.american_to_prob(-200) == pytest.approx(200 / 300)


class TestRemoveVig:
    def test_probs_sum_to_one(self):
        over, under = pe.remove_vig(-110, -110)
        assert over + under == pytest.approx(1.0)

    def test_symmetric_odds_are_fifty_fifty(self):
        over, under = pe.remove_vig(-110, -110)
        assert over == pytest.approx(0.5)
        assert under == pytest.approx(0.5)

    def test_favored_side_has_higher_true_prob(self):
        # Over is the favorite (-200) vs Under (+150)
        over, under = pe.remove_vig(-200, 150)
        assert over > under
        assert over + under == pytest.approx(1.0)


# ─────────────────────────────────────────
# PROBABILITY MODEL
# ─────────────────────────────────────────

class TestCalculateHitProbability:
    def test_projection_above_line_over_50pct(self):
        assert pe.calculate_hit_probability(29.0, 24.5, 0.28) > 0.5

    def test_projection_below_line_under_50pct(self):
        assert pe.calculate_hit_probability(20.0, 24.5, 0.28) < 0.5

    def test_projection_on_line_is_fifty_fifty(self):
        assert pe.calculate_hit_probability(24.5, 24.5, 0.28) == pytest.approx(0.5)

    def test_returns_valid_probability(self):
        p = pe.calculate_hit_probability(26.4, 24.5, 0.28)
        assert 0.0 <= p <= 1.0

    def test_zero_std_dev_degenerate_over(self):
        # projection 0 → std_dev 0 → deterministic branch
        assert pe.calculate_hit_probability(0.0, -1.0, 0.25) == 1.0

    def test_zero_std_dev_degenerate_under(self):
        assert pe.calculate_hit_probability(0.0, 1.0, 0.25) == 0.0


# ─────────────────────────────────────────
# STD DEV LOOKUP  (regression test for the NBA/NHL key collision)
# ─────────────────────────────────────────

class TestGetStdDevPct:
    def test_nba_points_not_overwritten_by_nhl(self):
        # The whole point of the fix: NBA points must stay 0.28, not the
        # NHL player_points value (0.55) that used to clobber it.
        assert pe.get_std_dev_pct("basketball_nba", "player_points") == 0.28

    def test_nhl_points_distinct_from_nba(self):
        assert pe.get_std_dev_pct("icehockey_nhl", "player_points") == 0.55

    def test_nba_assists_not_overwritten_by_nhl(self):
        assert pe.get_std_dev_pct("basketball_nba", "player_assists") == 0.35

    def test_nhl_assists_distinct_from_nba(self):
        assert pe.get_std_dev_pct("icehockey_nhl", "player_assists") == 0.70

    def test_unknown_pair_falls_back_to_default(self):
        assert pe.get_std_dev_pct("basketball_nba", "not_a_market") == pe.DEFAULT_STD_DEV_PCT
        assert pe.get_std_dev_pct("cricket_ipl", "player_points") == pe.DEFAULT_STD_DEV_PCT

    def test_non_colliding_markets_unaffected(self):
        assert pe.get_std_dev_pct("americanfootball_nfl", "player_pass_yds") == 0.26
        assert pe.get_std_dev_pct("baseball_mlb", "player_batter_home_runs") == 0.90


# ─────────────────────────────────────────
# PROP EXTRACTION / BEST LINE
# ─────────────────────────────────────────

def _event_with_two_books():
    """Odds-API-shaped payload: two books quoting LeBron James points."""
    return {
        "bookmakers": [
            {
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"name": "Over", "description": "LeBron James", "point": 24.5, "price": -110},
                            {"name": "Under", "description": "LeBron James", "point": 24.5, "price": -110},
                        ],
                    }
                ],
            },
            {
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"name": "Over", "description": "LeBron James", "point": 24.5, "price": -105},
                            {"name": "Under", "description": "LeBron James", "point": 24.5, "price": -115},
                        ],
                    }
                ],
            },
        ]
    }


class TestExtractPlayerProp:
    def test_extracts_all_books(self):
        props = pe.extract_player_prop(_event_with_two_books(), "LeBron James", "player_points")
        assert len(props) == 2
        assert {p["bookmaker"] for p in props} == {"DraftKings", "FanDuel"}

    def test_no_match_for_other_player(self):
        props = pe.extract_player_prop(_event_with_two_books(), "Stephen Curry", "player_points")
        assert props == []

    def test_no_match_for_other_market(self):
        props = pe.extract_player_prop(_event_with_two_books(), "LeBron James", "player_rebounds")
        assert props == []


class TestBestLine:
    def test_empty_returns_none(self):
        assert pe.best_line([]) is None

    def test_picks_highest_over_odds(self):
        props = pe.extract_player_prop(_event_with_two_books(), "LeBron James", "player_points")
        # FanDuel -105 pays more than DraftKings -110
        assert pe.best_line(props, side="over")["bookmaker"] == "FanDuel"

    def test_picks_highest_under_odds(self):
        props = pe.extract_player_prop(_event_with_two_books(), "LeBron James", "player_points")
        # DraftKings -110 pays more than FanDuel -115
        assert pe.best_line(props, side="under")["bookmaker"] == "DraftKings"


# ─────────────────────────────────────────
# FULL ANALYSIS PIPELINE  (get_event_props monkeypatched)
# ─────────────────────────────────────────

class TestAnalyzeProp:
    def test_strong_over_edge(self, monkeypatch):
        monkeypatch.setattr(pe, "get_event_props", lambda *a, **k: _event_with_two_books())
        result = pe.analyze_prop(
            player_name="LeBron James",
            projection=29.0,          # well above the 24.5 line
            market_key="player_points",
            sport_key="basketball_nba",
            event_id="evt123",
        )
        assert "error" not in result
        assert result["line"] == 24.5
        assert result["model_prob_over_pct"] > 50
        assert result["edge_over_pct"] > 0
        assert "OVER" in result["recommendation"]

    def test_uses_sport_specific_std_dev(self, monkeypatch):
        # Regression guard: NBA points analysis must use 28.0%, not NHL's 55.0%.
        monkeypatch.setattr(pe, "get_event_props", lambda *a, **k: _event_with_two_books())
        result = pe.analyze_prop(
            player_name="LeBron James",
            projection=26.4,
            market_key="player_points",
            sport_key="basketball_nba",
            event_id="evt123",
        )
        assert result["std_dev_used_pct"] == 28.0

    def test_no_lines_path(self, monkeypatch):
        monkeypatch.setattr(pe, "get_event_props", lambda *a, **k: {"bookmakers": []})
        result = pe.analyze_prop(
            player_name="Nobody",
            projection=10.0,
            market_key="player_points",
            sport_key="basketball_nba",
            event_id="evt123",
        )
        assert result["no_lines"] is True
        assert result["line"] is None
        assert result["hit_probability"] is None

    def test_api_value_error_becomes_no_lines(self, monkeypatch):
        def _raise(*a, **k):
            raise ValueError("Player prop markets require an upgraded subscription.")
        monkeypatch.setattr(pe, "get_event_props", _raise)
        result = pe.analyze_prop(
            player_name="LeBron James",
            projection=26.4,
            market_key="player_points",
            sport_key="basketball_nba",
            event_id="evt123",
        )
        assert result["no_lines"] is True
        assert "upgraded subscription" in result["no_lines_reason"]

    def test_unexpected_error_returns_error_dict(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(pe, "get_event_props", _boom)
        result = pe.analyze_prop(
            player_name="LeBron James",
            projection=26.4,
            market_key="player_points",
            sport_key="basketball_nba",
            event_id="evt123",
        )
        assert "error" in result


# ─────────────────────────────────────────
# BATCH SCAN
# ─────────────────────────────────────────

class TestScanProps:
    def test_filters_and_sorts_by_edge(self, monkeypatch):
        monkeypatch.setattr(pe, "get_event_props", lambda *a, **k: _event_with_two_books())
        props = [
            {"player": "LeBron James", "projection": 29.0, "market": "player_points"},  # big edge
            {"player": "LeBron James", "projection": 24.6, "market": "player_points"},  # tiny edge
        ]
        results = pe.scan_props(props, "basketball_nba", "evt123", min_edge=0.05)
        # Only the high-edge play clears a 5% threshold.
        assert len(results) == 1
        assert results[0]["projection"] == 29.0

    def test_skips_no_lines(self, monkeypatch):
        monkeypatch.setattr(pe, "get_event_props", lambda *a, **k: {"bookmakers": []})
        props = [{"player": "Nobody", "projection": 10.0, "market": "player_points"}]
        assert pe.scan_props(props, "basketball_nba", "evt123") == []


# ─────────────────────────────────────────
# LIVE SCORES (ESPN scoreboard — free, mocked)
# ─────────────────────────────────────────

class _ScoreResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_scoreboard(url, **kwargs):
    # Only today's board (no ?dates=) returns games; prior days are empty.
    if kwargs.get("params"):
        return _ScoreResp({"events": []})
    return _ScoreResp({"events": [
        {"id": "1", "date": "2026-07-19T23:00Z",
         "status": {"type": {"state": "in", "shortDetail": "Top 7th"}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"displayName": "Philadelphia Phillies"}, "score": "0"},
             {"homeAway": "away", "team": {"displayName": "New York Mets"}, "score": "6"},
         ]}]},
        {"id": "2", "date": "2026-07-19T20:00Z",
         "status": {"type": {"state": "post", "shortDetail": "Final"}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"displayName": "New York Yankees"}, "score": "2"},
             {"homeAway": "away", "team": {"displayName": "Los Angeles Dodgers"}, "score": "8"},
         ]}]},
        {"id": "3", "date": "2026-07-20T02:00Z",   # not started → no scores
         "status": {"type": {"state": "pre", "shortDetail": "9:00 PM"}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"displayName": "San Diego Padres"}, "score": "0"},
             {"homeAway": "away", "team": {"displayName": "San Francisco Giants"}, "score": "0"},
         ]}]},
    ]})


class TestGetGameScores:
    def test_parses_live_final_and_pregame(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", _fake_scoreboard)
        games = pe.get_game_scores("baseball_mlb", days_from=0)
        assert len(games) == 3
        live = next(g for g in games if g["id"] == "1")
        assert live["completed"] is False
        assert live["status_detail"] == "Top 7th"
        assert {"name": "New York Mets", "score": "6"} in live["scores"]
        final = next(g for g in games if g["id"] == "2")
        assert final["completed"] is True
        pre = next(g for g in games if g["id"] == "3")
        assert pre["scores"] == []          # no score until the game starts

    def test_unknown_sport_returns_empty(self):
        assert pe.get_game_scores("quidditch_pro") == []

    def test_network_error_returns_empty(self, monkeypatch):
        def _boom(url, **k):
            raise pe.requests.exceptions.RequestException("down")
        monkeypatch.setattr(pe.requests, "get", _boom)
        assert pe.get_game_scores("baseball_mlb") == []


# ─────────────────────────────────────────
# GAME LINES (ESPN scoreboard odds — free, mocked)
# ─────────────────────────────────────────

def _fake_lines_board(url, **kwargs):
    if kwargs.get("params"):          # future-day fetches → empty
        return _ScoreResp({"events": []})
    return _ScoreResp({"events": [
        {"id": "10", "date": "2026-07-20T23:00Z", "competitions": [{
            "competitors": [
                {"homeAway": "home", "team": {"displayName": "Cleveland Guardians"}},
                {"homeAway": "away", "team": {"displayName": "Minnesota Twins"}},
            ],
            "odds": [{
                "provider": {"name": "DraftKings"}, "overUnder": 7.5,
                "moneyline":   {"home": {"close": {"odds": "+101"}}, "away": {"close": {"odds": "-121"}}},
                "pointSpread": {"home": {"close": {"line": "+1.5", "odds": "-163"}},
                                "away": {"close": {"line": "-1.5", "odds": "+135"}}},
                "total":       {"over": {"close": {"line": "o7.5", "odds": "-108"}},
                                "under": {"close": {"line": "u7.5", "odds": "-111"}}},
            }],
        }]},
        {"id": "11", "date": "2026-07-20T20:00Z", "competitions": [{   # no odds posted
            "competitors": [
                {"homeAway": "home", "team": {"displayName": "Boston Red Sox"}},
                {"homeAway": "away", "team": {"displayName": "Tampa Bay Rays"}},
            ],
        }]},
    ]})


class TestGetGameLinesESPN:
    def test_parses_all_three_markets(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", _fake_lines_board)
        games = pe.get_game_lines("baseball_mlb")
        g = next(x for x in games if x["id"] == "10")
        assert g["source_book"] == "DraftKings"
        assert g["h2h"] == {"home": 101, "away": -121}
        assert g["spreads"] == {"home_line": 1.5, "home_odds": -163,
                                "away_line": -1.5, "away_odds": 135}
        assert g["totals"] == {"line": 7.5, "over_odds": -108, "under_odds": -111}

    def test_game_without_odds_still_listed(self, monkeypatch):
        monkeypatch.setattr(pe.requests, "get", _fake_lines_board)
        games = pe.get_game_lines("baseball_mlb")
        g = next(x for x in games if x["id"] == "11")
        assert g["h2h"] is None and g["spreads"] is None and g["totals"] is None
        assert g["home_team"] == "Boston Red Sox"   # schedule still shown

    def test_unknown_sport_returns_empty(self):
        assert pe.get_game_lines("quidditch_pro") == []


# ─────────────────────────────────────────
# GAME MODEL — value picks
# ─────────────────────────────────────────

class TestGameModelMath:
    def test_ratings_and_league_average(self):
        results = [
            {"home": "A", "away": "B", "home_score": 6, "away_score": 3},
            {"home": "B", "away": "A", "home_score": 2, "away_score": 5},
        ]
        ratings, lg = pe.compute_team_ratings(results)
        # A scored 6 then 5 → 5.5; allowed 3 then 2 → 2.5
        assert ratings["A"]["rs"] == pytest.approx(5.5)
        assert ratings["A"]["ra"] == pytest.approx(2.5)
        # League avg = all points / all team-games = (6+3+2+5)/4 = 4.0
        assert lg == pytest.approx(4.0)

    def test_stronger_offense_is_favored(self):
        results = [
            {"home": "A", "away": "B", "home_score": 8, "away_score": 2},
            {"home": "B", "away": "A", "home_score": 2, "away_score": 8},
            {"home": "A", "away": "C", "home_score": 7, "away_score": 3},
            {"home": "C", "away": "B", "home_score": 5, "away_score": 4},
        ]
        ratings, lg = pe.compute_team_ratings(results)
        cfg = dict(pe.GAME_MODEL_CONFIG["baseball_mlb"], min_games=1)
        proj = pe.project_game("A", "B", ratings, lg, cfg)
        assert proj["home_win_prob"] > 0.5
        assert proj["proj_total"] > 0

    def test_insufficient_games_returns_none(self):
        ratings = {"A": {"rs": 5, "ra": 4, "games": 2},
                   "B": {"rs": 4, "ra": 5, "games": 2}}
        cfg = pe.GAME_MODEL_CONFIG["baseball_mlb"]   # min_games = 5
        assert pe.project_game("A", "B", ratings, 4.5, cfg) is None

    def test_missing_team_returns_none(self):
        ratings = {"A": {"rs": 5, "ra": 4, "games": 9}}
        cfg = dict(pe.GAME_MODEL_CONFIG["baseball_mlb"], min_games=1)
        assert pe.project_game("A", "Ghost", ratings, 4.5, cfg) is None


class TestGenerateGamePicks:
    def _lines(self):
        # Line (9.5) sits above the model's total (teams avg 4 → ~8) → Under lean.
        return [{"id": "g1", "sport": "baseball_mlb",
                 "home_team": "A", "away_team": "B",
                 "commence_time": "2026-07-20T23:00Z",
                 "h2h": {"home": -120, "away": 100},
                 "spreads": None,
                 "totals": {"line": 9.5, "over_odds": -110, "under_odds": -110},
                 "source_book": "DraftKings"}]

    def _results(self):
        # Three round-robins of 4-4 games → every team averages 4 RS / 4 RA over
        # 6 games (clears min_games=5), so the model total for A vs B is ~8.0.
        games = []
        for _ in range(3):
            for h, a in (("A", "B"), ("A", "C"), ("B", "C")):
                games.append({"home": h, "away": a, "home_score": 4, "away_score": 4})
        return games

    def test_flags_under_on_inflated_line(self, monkeypatch):
        monkeypatch.setattr(pe, "get_game_lines", lambda s: self._lines())
        monkeypatch.setattr(pe, "fetch_recent_results", lambda s, d: self._results())
        picks = pe.generate_game_picks("baseball_mlb")
        assert "g1" in picks
        # These teams average ~3 runs; a 12.0 total is far too high → Under lean.
        assert picks["g1"]["totals"]["pick"].startswith("Under")
        assert picks["g1"]["totals"]["edge"] <= pe.GAME_MODEL_CONFIG["baseball_mlb"]["total_edge_cap"]

    def test_unsupported_sport_returns_empty(self):
        assert pe.generate_game_picks("quidditch_pro") == {}

    def test_ml_edges_are_tempered(self, monkeypatch):
        monkeypatch.setattr(pe, "get_game_lines", lambda s: self._lines())
        monkeypatch.setattr(pe, "fetch_recent_results", lambda s, d: self._results())
        picks = pe.generate_game_picks("baseball_mlb")
        if "h2h" in picks.get("g1", {}):
            # Market-anchored → no double-digit "edges".
            assert picks["g1"]["h2h"]["edge"] < 10
