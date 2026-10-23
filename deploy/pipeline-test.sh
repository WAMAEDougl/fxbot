#!/usr/bin/env bash
# Paper-only pipeline test with fast 5-minute averages. Run as root in /opt/fxbot.
#   deploy/pipeline-test.sh start   # swap journal + config, restart engine
#   deploy/pipeline-test.sh stop    # flatten paper account, restore daily config + journal
set -euo pipefail
cd "$(dirname "$0")/.."
grep -q '^TS_ENV=paper' .env || { echo "refusing: TS_ENV is not paper"; exit 1; }
J=state/journal-paper.sqlite; DAILY=state/journal-paper.daily.sqlite

case "${1:-}" in
start)
  docker compose --profile engine stop engine
  [ -f "$DAILY" ] || mv "$J" "$DAILY"        # keep the daily-strategy cursor safe
  rm -f "$J" state/HALT
  sed -i '/^TS_CONFIG=/d' .env; echo "TS_CONFIG=configs/ma_crossover.alpaca-test.yaml" >> .env
  docker compose --profile engine up -d --force-recreate engine ;;
stop)
  docker compose --profile engine stop engine
  docker run --rm -i --env-file .env fxbot-engine python - <<PY
import asyncio
from data.adapters.alpaca import AlpacaHttp, PAPER_URL
async def m():
    h = AlpacaHttp()
    await h.request("DELETE", PAPER_URL + "/v2/orders")
    await h.request("DELETE", PAPER_URL + "/v2/positions", params={"cancel_orders": "true"})
    print("paper account flattened; positions left:", len(await h.request("GET", PAPER_URL + "/v2/positions")))
asyncio.run(m())
PY
  sed -i '/^TS_CONFIG=/d' .env
  [ -f "$DAILY" ] && { mv "$J" state/journal-paper.test-$(date +%F).sqlite; mv "$DAILY" "$J"; }
  chown -R 1000:1000 state
  docker compose --profile engine up -d --force-recreate engine ;;
*) echo "usage: $0 start|stop"; exit 1 ;;
esac
