# Presence of this file puts the repo root on sys.path so tests can import
# prop_engine / propjunkie_server / models / forms / emails directly.
#
# It also sets safe defaults for the env vars the server reads at import time,
# so importing propjunkie_server during tests doesn't crash on the SECRET_KEY
# guard and uses an isolated throwaway SQLite database — never a real one.

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.gettempdir(), "propjunkie_test.db"),
)
