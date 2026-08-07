#!/usr/bin/env bash
# Deploy the Straiker DefendAI policy to an existing Azure APIM instance.
#
# Usage:
#   STRAIKER_API_KEY=xxxx ./deploy.sh <resource-group> <apim-name> [--no-test-api] [--monolith] [--contract rich|webhook]
#
# Default deploys the policy FRAGMENTS (rich + webhook pairs registered
# instance-wide) plus a thin include-fragment policy on the target API using
# the selected contract:
#   --contract rich     /api/v1/detect[?agentic], local score>threshold (default)
#   --contract webhook  /api/v1/detect/webhook, server-side action blocking
# --monolith deploys the generated single-file policy/straiker-policy.xml
# instead (portal-parity mode, rich contract only).
#
# Requires: az cli logged in (`az login`), bicep installed (`az bicep install`).

set -euo pipefail

RG="${1:?usage: $0 <resource-group> <apim-name> [--no-test-api] [--monolith] [--contract rich|webhook]}"
APIM="${2:?usage: $0 <resource-group> <apim-name> [--no-test-api] [--monolith] [--contract rich|webhook]}"
shift 2

CREATE_TEST_API="true"
USE_MONOLITH="false"
CONTRACT="rich"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-test-api) CREATE_TEST_API="false"; shift ;;
    --monolith)    USE_MONOLITH="true"; shift ;;
    --contract)    CONTRACT="${2:?--contract needs rich|webhook}"; shift 2 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done
[[ "$CONTRACT" == "rich" || "$CONTRACT" == "webhook" ]] || { echo "--contract must be rich or webhook"; exit 1; }
[[ "$USE_MONOLITH" == "true" && "$CONTRACT" == "webhook" ]] && { echo "--monolith supports the rich contract only"; exit 1; }

: "${STRAIKER_API_KEY:?STRAIKER_API_KEY env var required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$SCRIPT_DIR/.."
BICEP="$REPO/bicep/main.bicep"
POLICY_XML="$REPO/policy/straiker-policy.xml"

[[ -f "$BICEP" ]] || { echo "missing $BICEP"; exit 1; }

EXTRA_PARAMS=()
if [[ "$USE_MONOLITH" == "true" ]]; then
  # Never ship a stale monolith: fail if it drifted from the fragments.
  "$REPO/scripts/build-monolith.sh" --check
  EXTRA_PARAMS+=(useMonolith=true "policyXml=@$POLICY_XML")
fi

echo "==> Deploying Straiker policy ($([[ "$USE_MONOLITH" == "true" ]] && echo monolith || echo "fragments/$CONTRACT") mode) to APIM '$APIM' in RG '$RG'"

az deployment group create \
  --resource-group "$RG" \
  --template-file "$BICEP" \
  --parameters \
      apimName="$APIM" \
      straikerApiKey="$STRAIKER_API_KEY" \
      createTestApi="$CREATE_TEST_API" \
      contract="$CONTRACT" \
      ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"} \
  --query 'properties.outputs' \
  -o json

echo "==> Done. Test with tests/test.sh once the deployment completes."
