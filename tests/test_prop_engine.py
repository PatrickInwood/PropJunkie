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
