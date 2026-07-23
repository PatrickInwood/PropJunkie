"""
propjunkie_server.py
====================
Flask web server that exposes the PropJunkie API.

Endpoints:
  GET  /                      — landing page
  GET  /app                   — prop analyzer UI
  GET  /lines                 — live lines browser (ML, spreads, totals)
  GET  /health                — health check
  GET  /events/<sport>        — list today's games for a sport
  GET  /game-lines/<sport>    — ML, spread, total odds for upcoming games
  POST /analyze-prop          — analyze a player prop
  POST /scan-props            — batch scan props
  POST /create-checkout       — create a Stripe checkout session (Phase 4)

Run locally:
  python propjunkie_server.py

Run in production (Railway):
  gunicorn propjunkie_server:app --bind 0.0.0.0:$PORT --workers 4
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
from prop_engine import analyze_prop, claude_explain, get_events, scan_props, get_game_lines, get_game_scores, fetch_espn_player_context, fetch_espn_defense_context, generate_projection, generate_game_picks, generate_prop_board, fetch_final_scores
from models import db, User, Pick
from forms import (
    SignupForm, LoginForm, LogoutForm, ForgotPasswordForm, ResetPasswordForm, SPORT_CHOICES,
)
from emails import send_welcome_email, send_verification_email, send_password_reset_email
from tokens import (
    generate_reset_token, verify_reset_token, generate_email_token, verify_email_token,
)

# Load .env file when running locally
load_dotenv()

app = Flask(__name__)
CORS(app)

logger = logging.getLogger("propjunkie")

# Generic message returned to clients on unexpected errors — the real
# exception is logged server-side, never leaked in the HTTP response.
_GENERIC_ERROR = "Something went wrong. Please try again."

# ─────────────────────────────────────────
# DATABASE + SESSION CONFIG (user accounts)
# ─────────────────────────────────────────
# SECRET_KEY signs login sessions and email links. It MUST be a real, secret
# value in production. Rather than silently fall back to a public placeholder
# (which would let anyone forge a logged-in session), we refuse to boot in
# production without it. Local dev — with no production markers set — is allowed
# an insecure default so it stays zero-config.
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    _in_production = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("DATABASE_URL"))
    if _in_production:
        raise RuntimeError("SECRET_KEY environment variable must be set in production")
    _secret_key = "dev-insecure-change-me"  # local development only
app.config["SECRET_KEY"] = _secret_key
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# In production Railway injects DATABASE_URL. Locally it's blank, so we fall
# back to a SQLite file (a simple, zero-setup database stored on disk).
_db_url = os.getenv("DATABASE_URL") or "sqlite:///propjunkie.db"
# Railway-style URLs start with postgres://, but SQLAlchemy wants postgresql://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url

db.init_app(app)

# ── Login sessions (Flask-Login) ──
login_manager = LoginManager()
login_manager.init_app(app)
# Where to send users who hit a login-required page while logged out.
login_manager.login_view = "login"
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    """Flask-Login calls this on each request to reload the logged-in user."""
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        # Malformed session id (e.g. a tampered cookie) — treat as logged out.
        return None


# Create any missing tables on startup. Safe to run every boot — it only
# creates tables that don't already exist, and never drops or alters data.
with app.app_context():
    db.create_all()
    # Lightweight migration: create_all() won't add columns to an existing table,
    # so add the picks.odds column (for ROI) if it isn't there yet.
    try:
        dialect = db.engine.dialect.name
        if dialect == "postgresql":
            db.session.execute(db.text("ALTER TABLE picks ADD COLUMN IF NOT EXISTS odds DOUBLE PRECISION"))
        else:  # sqlite has no IF NOT EXISTS — check the schema first
            cols = [r[1] for r in db.session.execute(db.text("PRAGMA table_info(picks)"))]
            if "odds" not in cols:
                db.session.execute(db.text("ALTER TABLE picks ADD COLUMN odds FLOAT"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("picks.odds column migration failed")

# ─────────────────────────────────────────
# RATE LIMITING
# 60 requests/minute globally per IP
# Analyze-prop capped at 5/minute (free tier protection)
# ─────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)


# ─────────────────────────────────────────
# PAGES
# ─────────────────────────────────────────

@app.route('/', methods=['GET'])
@limiter.exempt
def landing():
    return render_template('landing.html')


@app.route('/app', methods=['GET'])
@limiter.exempt
def app_page():
    return render_template('index.html')


@app.route('/lines', methods=['GET'])
@limiter.exempt
def lines_page():
    return render_template('lines.html')


@app.route('/slate', methods=['GET'])
@limiter.exempt
def slate_page():
    return render_template('slate.html')


@app.route('/props', methods=['GET'])
@limiter.exempt
def props_page():
    return render_template('props.html')


@app.route('/record', methods=['GET'])
@limiter.exempt
def record_page():
    return render_template('record.html')


@app.route('/privacy', methods=['GET'])
@limiter.exempt
def privacy_page():
    return render_template('privacy.html')


@app.route('/terms', methods=['GET'])
@limiter.exempt
def terms_page():
    return render_template('terms.html')


# ─────────────────────────────────────────
# AUTH — signup, login, logout
# ─────────────────────────────────────────

def _send_email_async(func, *args):
    """Send a transactional email without blocking the response.

    This keeps response timing the same whether or not the email exists, so an
    attacker can't tell which addresses have accounts by measuring how long the
    request takes. Sends synchronously under TESTING so tests stay deterministic.
    """
    if app.config.get("TESTING"):
        func(*args)
    else:
        threading.Thread(target=func, args=args, daemon=True).start()


def _safe_next():
    """Return the ?next= redirect target only if it's a safe, same-site path.

    This prevents an 'open redirect' — a login link that quietly bounces the
    user off to an attacker's site after they sign in. We only allow paths
    that start with a single '/' and carry no other domain.
    """
    target = request.values.get('next')
    if target and target.startswith('/') and not target.startswith('//') \
            and not urlparse(target).netloc:
        return target
    return None


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute; 50 per hour", methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('app_page'))

    form = LoginForm()
    if form.validate_on_submit():
        email = User.normalize_email(form.email.data)
        user = User.query.filter_by(email=email).first()
        if user is not None and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(_safe_next() or url_for('app_page'))
        # One generic message for both "no such email" and "wrong password",
        # so the form can't be used to discover which emails have accounts.
        form.password.errors.append("Incorrect email or password.")

    return render_template('login.html', form=form, next=_safe_next())


@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=['POST'])
def signup():
    # Already signed in? Skip straight to the app.
    if current_user.is_authenticated:
        return redirect(url_for('app_page'))

    form = SignupForm()
    if form.validate_on_submit():
        try:
            user = User.create_account(
                email=form.email.data,
                password=form.password.data,
                name=form.name.data or None,
                favorite_sports=",".join(form.favorite_sports.data) or None,
                referral_source=form.referral_source.data or None,
                date_of_birth=form.date_of_birth.data,
            )
        except IntegrityError:
            # Rare race: someone registered this email between the form's
            # uniqueness check and this insert. Roll back and show the
            # normal "already exists" error instead of crashing with a 500.
            db.session.rollback()
            form.email.errors.append("An account with this email already exists.")
        else:
            # "Let them in right away" — start a logged-in session immediately.
            login_user(user)
            # Fire-and-forget emails (sent in the background so signup stays
            # snappy and never breaks if email fails): a welcome, plus a
            # "confirm your email" link.
            verify_url = url_for('verify_email', token=generate_email_token(user), _external=True)
            _send_email_async(send_welcome_email, user)
            _send_email_async(send_verification_email, user, verify_url)
            return redirect(url_for('app_page'))

    # GET, or a POST that failed validation (errors render on the form).
    min_age = int(os.getenv("MIN_AGE", "21"))
    return render_template('signup.html', form=form, min_age=min_age, sport_choices=SPORT_CHOICES)


@app.route('/account', methods=['GET'])
@login_required
def account():
    # Minimal account page — for now its main job is a reliable log-out.
    return render_template('account.html', form=LogoutForm())


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    # POST-only, and we validate the form's CSRF token, so another site can't
    # force-log-out users via a link, an <img> tag, or a cross-site POST.
    form = LogoutForm()
    if form.validate_on_submit():
        logout_user()
    return redirect(url_for('landing'))


@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=['POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('app_page'))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = User.normalize_email(form.email.data)
        user = User.query.filter_by(email=email).first()
        if user is not None:
            reset_url = url_for('reset_password', token=generate_reset_token(user), _external=True)
            # Background send so response timing doesn't leak whether the email
            # exists. The user's id/email are already loaded, so the thread never
            # touches the request's database session.
            _send_email_async(send_password_reset_email, user, reset_url)
        # Always show the SAME confirmation, whether or not the email exists,
        # so this form can't reveal which emails have accounts.
        return render_template('forgot_password.html', form=form, sent=True)

    return render_template('forgot_password.html', form=form, sent=False)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=['POST'])
def reset_password(token):
    # Do NOT bounce logged-in users here: a reset link is often opened in a
    # browser where the user is already signed in, and they still need to be
    # able to set a new password.
    user = verify_reset_token(token)
    if user is None:
        # Bad, expired, or already-used link.
        return render_template('reset_password.html', form=None, invalid=True)

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()  # changing the hash also invalidates this reset link
        logout_user()        # end the current session so they sign in fresh with the new password
        flash("Your password has been updated. Please log in.")
        return redirect(url_for('login'))

    return render_template('reset_password.html', form=form, invalid=False)


@app.route('/verify-email/<token>', methods=['GET'])
def verify_email(token):
    user = verify_email_token(token)
    if user is None:
        flash("That verification link is invalid or has expired.")
    else:
        if not user.email_verified:
            user.email_verified = True
            db.session.commit()
        flash("Your email address has been verified. 🎉")
    return redirect(url_for('account') if current_user.is_authenticated else url_for('login'))


@app.route('/resend-verification', methods=['POST'])
@limiter.limit("5 per hour")
@login_required
def resend_verification():
    # Reuse the empty CSRF-only form to validate the request's token.
    form = LogoutForm()
    if form.validate_on_submit():
        if current_user.email_verified:
            flash("Your email is already verified.")
        else:
            verify_url = url_for('verify_email', token=generate_email_token(current_user), _external=True)
            _send_email_async(send_verification_email, current_user, verify_url)
            flash("Verification email sent — check your inbox.")
    return redirect(url_for('account'))


# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────

@app.route('/health', methods=['GET'])
@limiter.exempt
def health():
    return jsonify({'status': 'ok', 'service': 'PropJunkie API'})


# ─────────────────────────────────────────
# LIVE SCORES
# Shared server-side cache: the Odds API /scores endpoint consumes quota, and
# the Live Lines page polls it while open. Without a shared cache, every viewer
# every minute is a fresh API hit — enough to drain the free tier in a day.
# One cache entry per (sport, daysFrom) serves all viewers for _SCORES_TTL.
# ─────────────────────────────────────────

_scores_cache: dict = {}   # (sport, days_from) → {'data': list, 'ts': float}
_SCORES_TTL = 45           # seconds — still feels live, but caps API burn

@app.route('/scores/<sport>', methods=['GET'])
@limiter.limit("30 per minute")
def game_scores(sport):
    """
    GET /scores/baseball_mlb?daysFrom=1
    Returns live scores and recently completed game results.
    daysFrom: 0 = today only, 1 = include yesterday (default), 3 = max
    """
    try:
        days_from = int(request.args.get('daysFrom', 1))
    except (TypeError, ValueError):
        days_from = 1

    cache_key = (sport, days_from)
    now = time.time()
    cached = _scores_cache.get(cache_key)
    if cached and (now - cached['ts']) < _SCORES_TTL:
        return jsonify(cached['data'])
    try:
        data = get_game_scores(sport, days_from=days_from)
        _scores_cache[cache_key] = {'data': data, 'ts': now}
        return jsonify(data)
    except Exception:
        logger.exception("Error fetching scores for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# GAME LINES — ML, spreads, totals
# Server-side cache to conserve Odds API quota. Odds move slowly for our
# purposes, so a longer TTL trades a little freshness for a lot of quota.
# ─────────────────────────────────────────

_lines_cache: dict = {}   # sport_key → {'data': list, 'ts': float}
_LINES_TTL = 900          # seconds (15 min) — shared across all viewers

@app.route('/game-lines/<sport>', methods=['GET'])
@limiter.limit("20 per minute")
def game_lines(sport):
    """
    GET /game-lines/basketball_nba
    Returns moneyline, spread, and totals for upcoming games.
    Responses are cached for 5 minutes to conserve API quota.
    """
    now = time.time()
    cached = _lines_cache.get(sport)
    if cached and (now - cached['ts']) < _LINES_TTL:
        return jsonify(cached['data'])
    try:
        data = get_game_lines(sport)
        _lines_cache[sport] = {'data': data, 'ts': now}
        return jsonify(data)
    except Exception:
        logger.exception("Error fetching game lines for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# PROP BOARD — player-prop projections (free: projection + recent form only)
# ─────────────────────────────────────────

_prop_board_cache: dict = {}   # sport → {'data': list, 'ts': float}
_PROP_BOARD_TTL = 3600         # 1h — projections move slowly, and it's heavy to build

@app.route('/prop-predictions/<sport>', methods=['GET'])
@limiter.limit("20 per minute")
def prop_predictions(sport):
    """GET /prop-predictions/baseball_mlb — today's player-prop projection cards."""
    now = time.time()
    cached = _prop_board_cache.get(sport)
    if cached and (now - cached['ts']) < _PROP_BOARD_TTL:
        return jsonify(cached['data'])
    try:
        data = generate_prop_board(sport)
        _prop_board_cache[sport] = {'data': data, 'ts': now}
        return jsonify(data)
    except Exception:
        logger.exception("Error generating prop board for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# GAME PICKS — model value leans (moneyline + total)
# Heavier to compute (scans ~3 weeks of ESPN scores), so cache for 2 hours.
# ─────────────────────────────────────────

_picks_cache: dict = {}   # sport_key → {'data': dict, 'ts': float}
_PICKS_TTL = 7200         # seconds (2h) — the model moves slowly

@app.route('/game-picks/<sport>', methods=['GET'])
@limiter.limit("20 per minute")
def game_picks(sport):
    """
    GET /game-picks/baseball_mlb
    Returns PropJunkie's model leans keyed by game id: {gameId: {h2h, totals, ...}}.
    """
    now = time.time()
    cached = _picks_cache.get(sport)
    if cached and (now - cached['ts']) < _PICKS_TTL:
        return jsonify(cached['data'])
    try:
        data = generate_game_picks(sport)
        _picks_cache[sport] = {'data': data, 'ts': now}
        _snapshot_picks(sport, data)   # freeze new leans for accuracy tracking
        return jsonify(data)
    except Exception:
        logger.exception("Error generating game picks for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


def _parse_commence(iso: str):
    """Parse an ESPN commence-time string (e.g. '2026-07-20T23:05Z') to datetime."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _snapshot_picks(sport, picks):
    """Freeze each newly-flagged lean into the DB (idempotent by game+market).

    Never raise — accuracy tracking must not break the picks endpoint.
    """
    try:
        # The frozen model_value differs by market: total projection, home win
        # probability, or projected margin.
        model_val = {"totals": "model", "h2h": "model_prob", "spreads": "model_margin"}
        added = False
        for gid, e in (picks or {}).items():
            for market in ("totals", "spreads", "h2h"):
                m = e.get(market)
                if not m:
                    continue
                if Pick.query.filter_by(game_id=gid, market=market).first():
                    continue
                db.session.add(Pick(
                    game_id=gid, sport=sport, market=market,
                    commence_time=_parse_commence(e.get("commence")),
                    home_team=e.get("home"), away_team=e.get("away"),
                    pick=m.get("pick"), side=m.get("side"), line=m.get("line"),
                    odds=m.get("odds"),
                    model_value=m.get(model_val[market]),
                    edge=m.get("edge"),
                ))
                added = True
        if added:
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Error snapshotting picks for %s", sport)


def _grade_pending_picks(sport=None):
    """Grade any ungraded picks whose games have finished. Never raises."""
    try:
        q = Pick.query.filter_by(graded=False)
        if sport:
            q = q.filter_by(sport=sport)
        pending = q.all()
        if not pending:
            return

        now = datetime.now(timezone.utc)
        by_date = {}   # (sport, YYYYMMDD) → [picks]
        for p in pending:
            ct = p.commence_time
            if ct is None:
                continue
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            # Give the game time to finish before looking for a final.
            if (now - ct).total_seconds() < 4 * 3600:
                continue
            by_date.setdefault((p.sport, ct.strftime("%Y%m%d")), []).append(p)

        changed = False
        for (sp, ymd), plist in by_date.items():
            finals = fetch_final_scores(sp, ymd)
            for p in plist:
                f = finals.get(p.game_id)
                if not f or not f.get("completed"):
                    continue
                p.home_score = f["home_score"]
                p.away_score = f["away_score"]
                p.result = p.grade(f["home_score"], f["away_score"])
                p.graded = True
                p.graded_at = now
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Error grading pending picks")


def _record_from(picks):
    w = sum(1 for p in picks if p.result == "win")
    l = sum(1 for p in picks if p.result == "loss")
    push = sum(1 for p in picks if p.result == "push")
    decided = w + l
    # Units / ROI over the graded picks that carry odds (a flat 1-unit bet each).
    # Picks snapshotted before odds were tracked are excluded from ROI.
    with_odds = [p for p in picks if p.odds is not None and p.units() is not None]
    units = sum(p.units() for p in with_odds)
    roi = round(100 * units / len(with_odds), 1) if with_odds else None
    return {"wins": w, "losses": l, "pushes": push,
            "win_pct": round(100 * w / decided, 1) if decided else None,
            "units": round(units, 2) if with_odds else None,
            "roi": roi, "priced": len(with_odds)}


@app.route('/model-record', methods=['GET'])
@limiter.limit("30 per minute")
def model_record():
    """PropJunkie's graded accuracy: overall, last 10, and by market."""
    _grade_pending_picks()
    graded = (Pick.query.filter_by(graded=True)
              .order_by(Pick.graded_at.desc()).all())
    pending = Pick.query.filter_by(graded=False).count()
    return jsonify({
        "overall":   _record_from(graded),
        "last10":    _record_from(graded[:10]),
        "moneyline": _record_from([p for p in graded if p.market == "h2h"]),
        "spread":    _record_from([p for p in graded if p.market == "spreads"]),
        "total":     _record_from([p for p in graded if p.market == "totals"]),
        "graded":    len(graded),
        "pending":   pending,
    })


_MARKET_LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}

_SPORT_LABELS = {
    "baseball_mlb": "MLB", "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL", "icehockey_nhl": "NHL",
}


@app.route('/record-data', methods=['GET'])
@limiter.limit("30 per minute")
def record_data():
    """Full accuracy detail for the /record page: overall, by market, by sport,
    and a graded-pick history."""
    _grade_pending_picks()
    graded = (Pick.query.filter_by(graded=True)
              .order_by(Pick.graded_at.desc()).all())
    pending = Pick.query.filter_by(graded=False).count()

    by_sport = {}
    for sk, label in _SPORT_LABELS.items():
        rows = [p for p in graded if p.sport == sk]
        if rows:
            by_sport[label] = _record_from(rows)

    history = [{
        "sport":  _SPORT_LABELS.get(p.sport, p.sport),
        "date":   p.commence_time.strftime("%b %-d") if p.commence_time else "",
        "away":   p.away_team,
        "home":   p.home_team,
        "market": _MARKET_LABELS.get(p.market, p.market),
        "pick":   p.pick,
        "result": p.result,
        "score":  (f"{p.away_score}–{p.home_score}"
                   if p.away_score is not None else ""),
    } for p in graded[:100]]

    return jsonify({
        "overall":   _record_from(graded),
        "last10":    _record_from(graded[:10]),
        "moneyline": _record_from([p for p in graded if p.market == "h2h"]),
        "spread":    _record_from([p for p in graded if p.market == "spreads"]),
        "total":     _record_from([p for p in graded if p.market == "totals"]),
        "by_sport":  by_sport,
        "history":   history,
        "graded":    len(graded),
        "pending":   pending,
    })


# ─────────────────────────────────────────
# EVENTS — list today's games
# ─────────────────────────────────────────

@app.route('/events/<sport>', methods=['GET'])
@limiter.limit("30 per minute")
def events(sport):
    """
    GET /events/basketball_nba
    GET /events/americanfootball_nfl
    GET /events/baseball_mlb
    GET /events/icehockey_nhl
    """
    try:
        data = get_events(sport)
        return jsonify(data)
    except Exception:
        logger.exception("Error fetching events for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# ANALYZE PROP — core endpoint
# Rate limited to 5/minute per IP (free tier)
# ─────────────────────────────────────────

@app.route('/generate-projection', methods=['POST'])
@limiter.limit("30 per minute")
def generate_projection_route():
    """Generate PropJunkie's own projection for a player's stat (free, no line needed)."""
    data = request.get_json(silent=True) or {}
    player = (data.get('player') or '').strip()
    market = data.get('market')
    sport = data.get('sport')
    if not player or not market or not sport:
        return jsonify({'error': 'Required fields: player, market, sport'}), 400
    try:
        return jsonify(generate_projection(player, market, sport))
    except Exception:
        logger.exception("Error generating projection for player=%s", player)
        return jsonify({'error': _GENERIC_ERROR}), 500


@app.route('/analyze-prop', methods=['POST'])
@limiter.limit("5 per minute; 20 per hour")
def analyze():
    """
    POST /analyze-prop
    Body (JSON):
    {
        "player":      "LeBron James",
        "projection":  26.4,
        "market":      "player_points",
        "sport":       "basketball_nba",
        "event_id":    "abc123",
        "user_email":  "user@example.com",   (optional, for subscription check later)
        "style":       "sharp"               (optional: sharp / casual / detailed)
    }
    """
    data = request.json or {}

    player     = data.get('player')
    projection = data.get('projection')
    market     = data.get('market', 'player_points')
    sport      = data.get('sport', 'basketball_nba')
    event_id   = data.get('event_id')
    style      = data.get('style', 'sharp')
    home_team  = data.get('home_team')
    away_team  = data.get('away_team')

    # Optional game lines from the frontend (auto-populated when user selects game)
    game_total  = data.get('game_total')
    home_spread = data.get('home_spread')
    away_spread = data.get('away_spread')
    home_ml     = data.get('home_ml')
    away_ml     = data.get('away_ml')

    # Validate required fields
    if not player or projection is None or not event_id:
        return jsonify({'error': 'Required fields: player, projection, event_id'}), 400

    try:
        projection = float(projection)
    except (TypeError, ValueError):
        return jsonify({'error': 'projection must be a number'}), 400

    try:
        result = analyze_prop(
            player_name = player,
            projection  = projection,
            market_key  = market,
            sport_key   = sport,
            event_id    = event_id,
        )

        if 'error' not in result:
            # Fetch real player stats from ESPN (free, no key, fails silently)
            player_ctx = fetch_espn_player_context(player, market, sport)

            # Fetch opponent's defensive stats from ESPN (fails silently)
            defense_ctx = fetch_espn_defense_context(player, market, sport, home_team, away_team)
            if defense_ctx:
                player_ctx = (player_ctx + "\n" + defense_ctx).strip() if player_ctx else defense_ctx

            # Append game lines context so Claude can reference ML/spread/total
            def fmt_odds(v):
                if v is None: return None
                return f"+{v}" if v > 0 else str(v)

            game_parts = []
            if away_ml is not None and home_ml is not None and away_team and home_team:
                game_parts.append(
                    f"ML: {away_team} {fmt_odds(away_ml)} / {home_team} {fmt_odds(home_ml)}"
                )
            if away_spread is not None and home_spread is not None and away_team and home_team:
                a_sp = f"+{away_spread}" if away_spread > 0 else str(away_spread)
                h_sp = f"+{home_spread}" if home_spread > 0 else str(home_spread)
                game_parts.append(f"Spread: {away_team} {a_sp} / {home_team} {h_sp}")
            if game_total is not None:
                game_parts.append(f"O/U: {game_total}")
            if game_parts:
                game_ctx = "🎰 GAME LINES: " + "  |  ".join(game_parts)
                player_ctx = (player_ctx + "\n" + game_ctx).strip() if player_ctx else game_ctx

            result['claude_take'] = claude_explain(
                result, style=style,
                home_team=home_team, away_team=away_team,
                player_context=player_ctx,
            )

        return jsonify(result)

    except Exception:
        logger.exception("Error analyzing prop for player=%s", player)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# BATCH SCAN — scan multiple props at once
# ─────────────────────────────────────────

@app.route('/scan-props', methods=['POST'])
@limiter.limit("2 per minute; 10 per hour")
def scan():
    """
    POST /scan-props
    Body (JSON):
    {
        "sport":    "basketball_nba",
        "event_id": "abc123",
        "min_edge": 0.03,
        "props": [
            {"player": "LeBron James",  "projection": 26.4, "market": "player_points"},
            {"player": "Anthony Davis", "projection": 11.2, "market": "player_rebounds"}
        ]
    }
    """
    data     = request.json or {}
    sport    = data.get('sport', 'basketball_nba')
    event_id = data.get('event_id')
    min_edge = data.get('min_edge', 0.03)
    props    = data.get('props', [])

    if not event_id or not props:
        return jsonify({'error': 'Required: event_id and props array'}), 400

    try:
        results = scan_props(props, sport, event_id, min_edge=min_edge)
        return jsonify({'results': results, 'count': len(results)})
    except Exception:
        logger.exception("Error scanning props for event=%s", event_id)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# STRIPE CHECKOUT (Phase 4)
# ─────────────────────────────────────────

@app.route('/create-checkout', methods=['POST'])
@limiter.limit("10 per hour")
def create_checkout():
    """
    POST /create-checkout
    Body: { "email": "user@example.com", "tier": "pro" }
    Returns: { "checkout_url": "https://checkout.stripe.com/..." }

    Requires: pip install stripe
              STRIPE_SECRET_KEY in .env
              STRIPE_PRO_PRICE_ID in .env
    """
    try:
        import stripe
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

        data  = request.json or {}
        email = data.get('email')
        tier  = data.get('tier', 'pro')

        price_id_map = {
            'pro': os.getenv('STRIPE_PRO_PRICE_ID'),
        }
        price_id = price_id_map.get(tier)

        if not price_id:
            return jsonify({'error': f'Unknown tier: {tier}'}), 400

        session = stripe.checkout.Session.create(
            customer_email       = email,
            payment_method_types = ['card'],
            line_items           = [{'price': price_id, 'quantity': 1}],
            mode                 = 'subscription',
            success_url          = os.getenv('SUCCESS_URL', 'https://propjunkie.app/app'),
            cancel_url           = os.getenv('CANCEL_URL',  'https://propjunkie.app/#pricing'),
        )
        return jsonify({'checkout_url': session.url})

    except ImportError:
        logger.error("Stripe library not installed")
        return jsonify({'error': _GENERIC_ERROR}), 500
    except Exception:
        logger.exception("Error creating checkout session")
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# BACKGROUND GRADER
# Grade finished games on a timer so the record isn't only updated when someone
# happens to load /record. Runs in-process (fine for Railway); grading is
# idempotent, so the mild redundancy across gunicorn workers is harmless.
# ─────────────────────────────────────────

def _background_grader(interval=1800):
    time.sleep(60)   # let the app settle after boot
    while True:
        try:
            with app.app_context():
                _grade_pending_picks()
        except Exception:
            logger.exception("Background grader run failed")
        time.sleep(interval)


# Don't start the thread under pytest (it would make live network calls); it
# runs in local dev and in production (gunicorn imports this module).
if "pytest" not in sys.modules:
    threading.Thread(target=_background_grader, name="pick-grader", daemon=True).start()


# ─────────────────────────────────────────
# RUN
# ─────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"PropJunkie API running on http://localhost:{port}")
    app.run(port=port, debug=True)
