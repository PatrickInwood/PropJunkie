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
    # Every fetch now passes an explicit ?dates=; return the same board for any
    # date (get_game_scores dedupes by event id).
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
    # Every fetch now passes an explicit ?dates=; return the same board for any
    # date (get_game_lines dedupes by event id).
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

    def test_flags_spread_when_margin_clears_line(self, monkeypatch):
        lines = [{"id": "g2", "sport": "baseball_mlb", "home_team": "A", "away_team": "B",
                  "commence_time": "2026-07-20T23:00Z",
                  "h2h": {"home": -200, "away": 170},
                  "spreads": {"home_line": -1.5, "home_odds": 100, "away_line": 1.5, "away_odds": -120},
                  "totals": None, "source_book": "DraftKings"}]
        # A is a moderate favorite over B (7-2), even vs C → a home margin that
        # clears -1.5 without exceeding the noise cap.
        results = []
        for _ in range(3):
            for h, a, hs, as_ in [("A", "B", 7, 2), ("B", "A", 2, 7),
                                  ("A", "C", 4, 4), ("C", "A", 4, 4),
                                  ("B", "C", 4, 4), ("C", "B", 4, 4)]:
                results.append({"home": h, "away": a, "home_score": hs, "away_score": as_})
        monkeypatch.setattr(pe, "get_game_lines", lambda s: lines)
        monkeypatch.setattr(pe, "fetch_recent_results", lambda s, d: results)
        monkeypatch.setattr(pe, "fetch_probable_pitchers", lambda s: ({}, None))
        picks = pe.generate_game_picks("baseball_mlb")
        assert picks["g2"]["spreads"]["side"] == "home"
        assert picks["g2"]["spreads"]["edge"] <= pe.GAME_MODEL_CONFIG["baseball_mlb"]["spread_edge_cap"]

    def test_unsupported_sport_returns_empty(self):
        assert pe.generate_game_picks("quidditch_pro") == {}

    def test_ml_edges_are_tempered(self, monkeypatch):
        monkeypatch.setattr(pe, "get_game_lines", lambda s: self._lines())
        monkeypatch.setattr(pe, "fetch_recent_results", lambda s, d: self._results())
        picks = pe.generate_game_picks("baseball_mlb")
        if "h2h" in picks.get("g1", {}):
            # Market-anchored → no double-digit "edges".
            assert picks["g1"]["h2h"]["edge"] < 10


class TestStartingPitcherAdjustment:
    def _ratings(self):
        # Two average-ish teams over enough games.
        return ({"A": {"rs": 4.5, "ra": 4.5, "games": 12},
                 "B": {"rs": 4.5, "ra": 4.5, "games": 12}}, 4.5)

    def test_aces_lower_total_weak_starters_raise_it(self):
        ratings, lg = self._ratings()
        cfg = pe.GAME_MODEL_CONFIG["baseball_mlb"]
        base = pe.project_game("A", "B", ratings, lg, cfg)
        aces = pe.project_game("A", "B", ratings, lg, cfg,
                               home_sp_era=2.5, away_sp_era=2.5, avg_sp_era=4.2)
        weak = pe.project_game("A", "B", ratings, lg, cfg,
                               home_sp_era=6.0, away_sp_era=6.0, avg_sp_era=4.2)
        assert aces["proj_total"] < base["proj_total"] < weak["proj_total"]

    def test_no_avg_era_means_no_adjustment(self):
        ratings, lg = self._ratings()
        cfg = pe.GAME_MODEL_CONFIG["baseball_mlb"]
        base = pe.project_game("A", "B", ratings, lg, cfg)
        # Without avg_sp_era the pitcher inputs are ignored.
        same = pe.project_game("A", "B", ratings, lg, cfg,
                               home_sp_era=2.5, away_sp_era=2.5, avg_sp_era=None)
        assert same["proj_total"] == pytest.approx(base["proj_total"])

    def test_probable_pitchers_non_mlb_is_empty(self):
        assert pe.fetch_probable_pitchers("basketball_nba") == ({}, None)


# ─────────────────────────────────────────
# OPPONENT MATCHUP CONTEXT (for the AI analysis)
# ─────────────────────────────────────────

class TestOpponentContext:
    def test_batter_gets_opposing_starter_and_defense(self, monkeypatch):
        monkeypatch.setattr(pe, "_player_team", lambda n, s: "New York Yankees")
        monkeypatch.setattr(pe, "get_probable_pitchers", lambda s: (
            {("pittsburgh pirates", "new york yankees"):
             {"away_sp_name": "A. Starter", "away_sp_era": 3.49,
              "home_sp_name": "H. Starter", "home_sp_era": 4.0}}, 4.1))
        monkeypatch.setattr(pe, "get_team_ratings", lambda s: (
            {"Pittsburgh Pirates": {"rs": 3.9, "ra": 3.5, "games": 12}}, 4.5))
        ctx = pe.fetch_espn_defense_context("Aaron Judge", "player_batter_hits",
                                            "baseball_mlb", "New York Yankees", "Pittsburgh Pirates")
        assert "opposing starter A. Starter" in ctx and "3.49" in ctx
        assert "allow 3.5 runs/gm" in ctx

    def test_pitcher_gets_opposing_offense_not_a_starter(self, monkeypatch):
        monkeypatch.setattr(pe, "_player_team", lambda n, s: "Detroit Tigers")
        monkeypatch.setattr(pe, "get_probable_pitchers", lambda s: ({}, None))
        monkeypatch.setattr(pe, "get_team_ratings", lambda s: (
            {"Chicago Cubs": {"rs": 4.6, "ra": 4.2, "games": 12}}, 4.5))
        ctx = pe.fetch_espn_defense_context("Tarik Skubal", "player_pitcher_strikeouts",
                                            "baseball_mlb", "Chicago Cubs", "Detroit Tigers")
        assert "score 4.6 runs/gm" in ctx
        assert "opposing starter" not in ctx

    def test_unknown_player_team_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pe, "_player_team", lambda n, s: "")
        assert pe.fetch_espn_defense_context("X", "player_batter_hits",
                                             "baseball_mlb", "A", "B") == ""


# ─────────────────────────────────────────
# MODEL SHARPENING — recency weighting + park factors
# ─────────────────────────────────────────

class TestModelSharpening:
    def test_recency_weighting_favors_recent_form(self):
        games = [
            {"home": "H", "away": "X", "home_score": 9, "away_score": 2, "days_ago": 1},
            {"home": "H", "away": "Y", "home_score": 8, "away_score": 3, "days_ago": 2},
            {"home": "H", "away": "Z", "home_score": 2, "away_score": 5, "days_ago": 16},
            {"home": "H", "away": "W", "home_score": 1, "away_score": 6, "days_ago": 17},
        ]
        flat, _ = pe.compute_team_ratings(games)
        wtd, _ = pe.compute_team_ratings(games, half_life=10)
        assert wtd["H"]["rs"] > flat["H"]["rs"]   # recent surge counts more
        assert wtd["H"]["games"] == 4             # raw count kept for the min-games gate

    def _even(self, home):
        games = []
        for _ in range(3):
            for h, a in ((home, "Opp"), (home, "Third"), ("Opp", "Third")):
                games.append({"home": h, "away": a, "home_score": 4, "away_score": 4, "days_ago": 1})
        return games

    def _line(self, home):
        return [{"id": "g", "sport": "baseball_mlb", "home_team": home, "away_team": "Opp",
                 "commence_time": "2026-07-20T23:00Z", "h2h": None, "spreads": None,
                 "totals": {"line": 7.0, "over_odds": -110, "under_odds": -110}, "source_book": "DK"}]

    def _model_total(self, monkeypatch, home):
        pe._ratings_cache.clear()
        monkeypatch.setattr(pe, "fetch_probable_pitchers", lambda s: ({}, None))
        monkeypatch.setattr(pe, "get_game_lines", lambda s: self._line(home))
        monkeypatch.setattr(pe, "fetch_recent_results", lambda s, d: self._even(home))
        picks = pe.generate_game_picks("baseball_mlb")
        return next(iter(picks.values()), {}).get("totals", {}).get("model")

    def test_park_factor_inflates_hitter_park(self, monkeypatch):
        # Identical even 4-4 matchup (base total 8.0); only the home park changes.
        coors = self._model_total(monkeypatch, "Colorado Rockies")   # 1.15
        neutral = self._model_total(monkeypatch, "Houston Astros")   # unlisted → 1.0
        assert neutral == pytest.approx(8.0, abs=0.2)
        assert coors > neutral                                       # Coors inflates the total
        assert coors == pytest.approx(8.0 * 1.15, abs=0.2)
