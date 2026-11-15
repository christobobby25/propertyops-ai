#!/usr/bin/env bash
# Start a throwaway Firestore emulator on localhost, run the emulator test suite
# against it, and stop it. Never touches cloud Firestore.
#
#   scripts/run_emulator_tests.sh            # tests/emulator
#   scripts/run_emulator_tests.sh -k counter # extra pytest args
#
# Requires: gcloud with the cloud-firestore-emulator component, and Java 21+.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${EMULATOR_PORT:-8085}"
HOST="127.0.0.1:${PORT}"
PYTHON="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python )}"

java_major() {  # prints the major version of the given java binary
  "$1" -version 2>&1 | head -1 | sed -E 's/.*version "([0-9]+).*/\1/'
}
# Use the first Java 21+ found (JAVA_HOME, Homebrew keg-only installs, PATH).
JAVA_BIN=""
for candidate in "${JAVA_HOME:-}/bin/java" /opt/homebrew/opt/openjdk@21/bin/java \
                 /usr/local/opt/openjdk@21/bin/java "$(command -v java || true)"; do
  if [ -x "$candidate" ] && [ "$(java_major "$candidate")" -ge 21 ] 2>/dev/null; then
    JAVA_BIN="$candidate"
    break
  fi
done
if [ -z "$JAVA_BIN" ]; then
  echo "Java 21+ is required by the Firestore emulator (e.g. brew install openjdk@21)" >&2
  exit 1
fi
export PATH="$(dirname "$JAVA_BIN"):$PATH"
export JAVA_HOME="$(cd "$(dirname "$JAVA_BIN")/.." && pwd)"
"$JAVA_BIN" -version 2>&1 | head -1

LOG="$(mktemp -t firestore-emulator.XXXXXX)"
gcloud emulators firestore start --host-port="$HOST" >"$LOG" 2>&1 &
EMULATOR_PID=$!
cleanup() {
  kill "$EMULATOR_PID" 2>/dev/null || true
  # gcloud spawns the Java process; stop anything still bound to the port.
  pkill -f "cloud-firestore-emulator.*${PORT}" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 120); do
  if curl -fs "http://${HOST}/" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$EMULATOR_PID" 2>/dev/null; then cat "$LOG"; exit 1; fi
  sleep 0.5
done
curl -fs "http://${HOST}/" >/dev/null || { echo "emulator did not start"; cat "$LOG"; exit 1; }
echo "Firestore emulator ready at ${HOST}"

FIRESTORE_EMULATOR_HOST="$HOST" REQUIRE_EMULATOR=1 \
  "$PYTHON" -m pytest -p no:cacheprovider tests/emulator "$@"
