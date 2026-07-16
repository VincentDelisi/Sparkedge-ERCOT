#!/usr/bin/env bash
set -euo pipefail

# Railway provides $PORT; default to 8501 for local runs.
PORT="${PORT:-8501}"

# Persist the SQLite cache to the mounted volume if one exists, else local dir.
export SPARKEDGE_HOME="${SPARKEDGE_HOME:-/data}"
mkdir -p "$SPARKEDGE_HOME" 2>/dev/null || export SPARKEDGE_HOME="$HOME/.sparkedge_ercot"
mkdir -p "$SPARKEDGE_HOME"
export SPARKEDGE_DB_PATH="${SPARKEDGE_DB_PATH:-$SPARKEDGE_HOME/sparkedge_ercot.db}"

echo "[start] SPARKEDGE_DB_PATH=$SPARKEDGE_DB_PATH"
echo "[start] cache status before backfill:"
python -m sparkedge_ercot --status || true

# Seed the cache on first boot if it's empty (needed for the 30d rolling window).
# This is idempotent — upserts skip existing rows, so restarts are cheap.
if [ ! -f "$SPARKEDGE_DB_PATH" ] || [ ! -s "$SPARKEDGE_DB_PATH" ]; then
  echo "[start] empty cache detected — running initial backfill (background)"
  ( python -m sparkedge_ercot --backfill || echo "[start] backfill failed (non-fatal)" ) &
else
  echo "[start] cache present — running quick refresh (background)"
  ( python -m sparkedge_ercot --refresh || echo "[start] refresh failed (non-fatal)" ) &
fi

echo "[start] launching Streamlit on port $PORT"
exec python -m streamlit run sparkedge_ercot/app.py \
  --server.port="$PORT" \
  --server.address=0.0.0.0 \
  --server.headless=true \
  --server.enableCORS=false \
  --server.enableXsrfProtection=false
