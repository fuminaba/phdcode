#!/bin/bash
# Starts all three of ALiRA2's long-running services, each in its own
# tmux session -- the same three sessions (celery_server, live_viewer,
# telegram_server) and launch commands that have been run manually all
# along, just scripted. Each pane activates the alira conda env and
# sources .env itself before running its command, matching exactly how
# they've always been started (live_viewer's own command needs
# $LIVE_VIEWER_PORT from the shell; the other two self-load .env via
# python-dotenv, but sourcing it everywhere keeps all three consistent).
#
# Safe to re-run: skips any session that's already up rather than
# starting a second, colliding instance. That matters most for the
# celery worker specifically -- its whole point is concurrency=1 GPU
# serialization, and a second one silently defeats that (confirmed a
# real issue earlier: found 3 celery workers running at once by accident).
#
# Usage: scripts/start_services.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="alira"
# Deliberately OUTSIDE the repo: the llama.cpp fork is a build artifact
# of another project, not part of this one, and the GGUF weights are
# several GB. Override either here or in the environment if they move.
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-$HOME/Desktop/dev/llama.cpp-prism}"
MODELS_PRESET="${MODELS_PRESET:-$HOME/models/alira-models.ini}"

start_session() {
    local name="$1"
    local command="$2"

    if tmux has-session -t "$name" 2>/dev/null; then
        echo "[$name] already running -- skipping (tmux attach -t $name to check it)"
        return
    fi

    tmux new-session -d -s "$name" -c "$PROJECT_DIR"
    tmux send-keys -t "$name" \
        "conda activate $CONDA_ENV && set -a && source .env && set +a && $command" \
        Enter
    echo "[$name] started: $command"
}

# llama-server (PrismML llama.cpp fork) -- the second model backend,
# for Ternary Bonsai 2 27B, whose ternary GGML tensor types stock Ollama
# cannot load at all. Ollama keeps serving everything else on 11434;
# this serves bonsai2-27b on 8080, and LLM_ROUTING picks per model.
#
# ROUTER mode (--models-preset, deliberately no -m): that is the only
# mode where GET /models and POST /models/unload exist -- they 404 under
# -m, verified directly -- and those are what
# GenerativeService.unload_all_models() calls to free VRAM before
# marker-pdf extraction. Per-model flags (context size, --jinja for tool
# calls, -np 1, -ngl) live in the preset, not here.
#
# Started FIRST, before celery/telegram: those two will happily accept a
# chat turn the moment they're up, and a turn routed to bonsai2-27b
# before this is listening just fails.
# --models-max 1: the router defaults to loading up to FOUR models at
# once. bonsai2-27b alone is 13,518 MiB of this card's 16,303 MiB, so a
# second concurrent load has nowhere to go -- pin it to one.
start_session "llama_server" \
    "$LLAMA_CPP_DIR/build/bin/llama-server --models-preset $MODELS_PRESET --models-max 1 --host 0.0.0.0 --port 8080"
start_session "celery_server" \
    "celery -A llm_engine.tasks worker --loglevel=info --concurrency=1 --pool=prefork -Q chat,literature"
start_session "live_viewer" \
    "uvicorn live_viewer:app --host 0.0.0.0 --port \$LIVE_VIEWER_PORT"
start_session "telegram_server" \
    "python webhook_server.py"

echo
echo "tmux list-sessions          -- see all four"
echo "tmux attach -t <name>       -- watch one (detach with Ctrl-b d)"
