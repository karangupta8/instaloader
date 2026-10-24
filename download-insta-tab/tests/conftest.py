import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Repo root (the parent instaloader package), download-insta-tab/ (pipeline, session, server...)
# and the tests' own helpers must all be importable.
for path in (HERE.parent.parent, HERE.parent, HERE):
    sys.path.insert(0, str(path))
