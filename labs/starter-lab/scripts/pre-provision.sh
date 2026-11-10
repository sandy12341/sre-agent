#!/usr/bin/env bash
set -euo pipefail

existing_agent=$(azd env get-value EXISTING_SRE_AGENT_RESOURCE_ID 2>/dev/null || true)
if [[ -z "$existing_agent" ]]; then
  exit 0
fi

for candidate in python python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c "import yaml" >/dev/null 2>&1; then
    exec "$candidate" "$(dirname "$0")/existing-agent.py" preflight
  fi
done
echo "ERROR: Existing-agent preflight requires Python 3 with PyYAML." >&2
exit 1
