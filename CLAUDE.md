# PropJunkie

A Python sports prop-betting engine with a web front end.

**Note to Claude:** some details below were verified against the repo on 2026-07-16. If the repo drifts and anything here becomes wrong, correct it — and remind me to update this file.

## What this project is

PropJunkie generates and serves sports player-prop analysis (NBA, NFL, MLB, and NHL). It has a calculation engine plus a web server that renders results in the browser.

## Project layout

- `prop_engine.py` — core logic: the math/model that produces prop projections and edges.
- `propjunkie_server.py` — the Flask web server that serves pages and calls the engine.
- `templates/` — HTML pages rendered by the server (landing, app, lines, privacy, terms).
- `static/` — front-end assets (currently `favicon.svg`).
- `tests/` — automated tests (`test_prop_engine.py`).
- `conftest.py` — at the project root; puts the repo root on Python's import path so the tests can find `prop_engine`. (Shared fixtures would also go here if we add any.)
- `.github/workflows/tests.yml` — GitHub Actions: runs the tests automatically on every push to `main` and every pull request.
- `requirements.txt` — Python packages the app needs to run (Flask, scipy, anthropic, requests, etc.).
- `requirements-dev.txt` — extra packages needed only for development/testing (pytest).
- `.env.example` — template for secrets/config (copy to `.env`; never commit `.env`).

## How to run and test

- Install everything (app + test deps): `pip install -r requirements.txt -r requirements-dev.txt`
- Run tests: `pytest`
- Run the server locally: `python propjunkie_server.py`

## Conventions and rules for this project

- Explain every change in plain English — what changed, why, and what it affects. I'm learning, so treat each fix as a small lesson.
- Never commit secrets. Keep real keys in `.env` (which is gitignored). `.env.example` shows the shape only.
- Tests must pass before we call something done. If you change engine logic, run `pytest` and report results.
- Keep NBA / NFL / MLB / NHL logic clearly separated — we've already hit a bug where NBA and NHL stats collided. Watch for shared/global state that mixes sports.
- Small, focused commits with clear messages describing what and why.
- Ask before adding a new Python package or changing the project structure.
- Workflow files (`.github/workflows/`) can't be pushed from the terminal (the token lacks `workflow` scope) — edit those via the GitHub web UI.

## Where I want help

- Building and shipping features (the app itself).
- Understanding the code as we go.
- Catching bugs before they reach the site.
