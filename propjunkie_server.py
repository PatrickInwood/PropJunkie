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
import time
import logging
from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
from prop_engine import analyze_prop, claude_explain, get_events, scan_props, get_game_lines, get_game_scores, fetch_espn_player_context, fetch_espn_defense_context
from models import db, User
from forms import SignupForm, LogoutForm, SPORT_CHOICES
from emails import send_welcome_email

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


@app.route('/privacy', methods=['GET'])
@limiter.exempt
def privacy_page():
    return render_template('privacy.html')


@app.route('/terms', methods=['GET'])
@limiter.exempt
def terms_page():
    return render_template('terms.html')


# ─────────────────────────────────────────
# AUTH — signup, logout
# ─────────────────────────────────────────

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
            # Welcome email is fire-and-forget: it never blocks or breaks signup.
            send_welcome_email(user)
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


# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────

@app.route('/health', methods=['GET'])
@limiter.exempt
def health():
    return jsonify({'status': 'ok', 'service': 'PropJunkie API'})


# ─────────────────────────────────────────
# LIVE SCORES
# ─────────────────────────────────────────

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
        data = get_game_scores(sport, days_from=days_from)
        return jsonify(data)
    except Exception:
        logger.exception("Error fetching scores for %s", sport)
        return jsonify({'error': _GENERIC_ERROR}), 500


# ─────────────────────────────────────────
# GAME LINES — ML, spreads, totals
# 5-minute server-side cache to conserve Odds API quota
# ─────────────────────────────────────────

_lines_cache: dict = {}   # sport_key → {'data': list, 'ts': float}
_LINES_TTL = 300          # seconds

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
# RUN
# ─────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"PropJunkie API running on http://localhost:{port}")
    app.run(port=port, debug=True)
