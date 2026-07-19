"""
Tests for accuracy tracking: freezing model leans into the DB and grading them
against final scores. Uses the shared `client` fixture; the picks table is
cleared per-test for isolation, and score/pick generation is monkeypatched.
"""

from datetime import datetime, timezone, timedelta

import propjunkie_server as srv
from models import db, Pick


def _clear_picks():
    with srv.app.app_context():
        db.session.query(Pick).delete()
        db.session.commit()


class TestPickGrading:
    def test_totals_grading(self):
        over = Pick(market="totals", side="over", line=8.5)
        assert over.grade(5, 5) == "win"    # total 10 > 8.5
        assert over.grade(4, 4) == "loss"   # total 8 < 8.5
        under = Pick(market="totals", side="under", line=8.5)
        assert under.grade(4, 4) == "win"
        push = Pick(market="totals", side="over", line=8.0)
        assert push.grade(4, 4) == "push"   # total 8 == 8.0

    def test_moneyline_grading(self):
        home = Pick(market="h2h", side="home")
        assert home.grade(5, 3) == "win"
        assert home.grade(3, 5) == "loss"
        away = Pick(market="h2h", side="away")
        assert away.grade(3, 5) == "win"


class TestSnapshotAndRecord:
    def _picks_payload(self, commence):
        return {"g1": {
            "totals": {"pick": "Under 9.0", "side": "under", "line": 9.0, "model": 7.0, "edge": 2.0},
            "h2h":    {"pick": "Yankees ML", "side": "home", "model_prob": 0.55,
                       "market_prob": 0.5, "edge": 5.0},
            "home": "New York Yankees", "away": "Boston Red Sox",
            "commence": commence, "min_games": 12,
        }}

    def test_picks_are_snapshotted_once(self, client, monkeypatch):
        _clear_picks()
        payload = self._picks_payload("2026-07-20T23:05Z")
        monkeypatch.setattr(srv, "generate_game_picks", lambda s: payload)
        srv._picks_cache.clear()
        assert client.get("/game-picks/baseball_mlb").status_code == 200
        srv._picks_cache.clear()
        client.get("/game-picks/baseball_mlb")   # second pass must not duplicate
        with srv.app.app_context():
            assert Pick.query.filter_by(game_id="g1").count() == 2   # totals + h2h, once each

    def test_record_grades_finished_games(self, client, monkeypatch):
        _clear_picks()
        past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        monkeypatch.setattr(srv, "generate_game_picks", lambda s: self._picks_payload(past))
        srv._picks_cache.clear()
        client.get("/game-picks/baseball_mlb")

        # Final: Yankees (home) 3, Red Sox (away) 5. Total 8 → Under 9 wins;
        # home lost → Yankees ML loses.
        monkeypatch.setattr(srv, "fetch_final_scores",
                            lambda sp, ymd: {"g1": {"home_score": 3, "away_score": 5, "completed": True}})
        rec = client.get("/model-record").get_json()
        assert rec["graded"] == 2
        assert rec["total"]["wins"] == 1 and rec["total"]["losses"] == 0
        assert rec["moneyline"]["losses"] == 1 and rec["moneyline"]["wins"] == 0
        assert rec["overall"]["win_pct"] == 50.0

    def test_recent_games_not_yet_graded(self, client, monkeypatch):
        _clear_picks()
        # Game starts in the future → never graded even if a (bogus) final exists.
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        monkeypatch.setattr(srv, "generate_game_picks", lambda s: self._picks_payload(future))
        srv._picks_cache.clear()
        client.get("/game-picks/baseball_mlb")
        monkeypatch.setattr(srv, "fetch_final_scores",
                            lambda sp, ymd: {"g1": {"home_score": 3, "away_score": 5, "completed": True}})
        rec = client.get("/model-record").get_json()
        assert rec["graded"] == 0
        assert rec["pending"] == 2
