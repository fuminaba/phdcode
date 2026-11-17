"""phdcode."""
from pathlib import Path

# The folder of this clone: its code, .env, logs and docs (ADR-0035: two clones run side by side).
CLONE = Path(__file__).resolve().parents[1]
