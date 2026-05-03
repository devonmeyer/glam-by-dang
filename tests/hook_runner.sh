#!/bin/bash
# Runs after edits to src/ — restarts server and runs tests.
# Exit 0: all good (silent). Exit 2: failures found (wakes Claude with details).

PROJECT_DIR="/root/projects/personal_assistant/glam-by-dang"

# Restart server
pkill -f "uvicorn server:app" 2>/dev/null || true
sleep 1
cd "$PROJECT_DIR/src"
python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 > /tmp/gbd_server.log 2>&1 &
sleep 4

# Run tests
output=$(python3 "$PROJECT_DIR/tests/run_tests.py" 2>&1)
exit_code=$?

if [ $exit_code -ne 0 ]; then
  echo "=== Glam by Dang test failures after src/ edit ==="
  echo "$output"
  exit 2
fi

exit 0
