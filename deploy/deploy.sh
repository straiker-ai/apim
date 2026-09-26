#!/usr/bin/env bash
# Deploy the Straiker DefendAI policy to an existing Azure APIM instance.
#
# Usage:
#   STRAIKER_API_KEY=xxxx ./deploy.sh <resource-group> <apim-name> [--no-test-api] [--no-attach] [--monolith] [--contract v3|rich|webhook] [--key-named-value <name>]
#
# Default deploys the policy FRAGMENTS (v3 + rich + webhook pairs registered
# instance-wide) plus a thin include-fragment policy on the target API using
# the selected contract:
#   --contract v3       POST /api/v3/detect, sk_agt_ key, platform verdicts (default)
#   --contract rich     /api/v1/detect[?agentic], UUID key, local score>threshold
#   --contract webhook  /api/v1/detect/webhook, UUID key (v1 preview)
# The contract is picked by the key you hold: sk_agt_ -> v3, UUID -> rich/webhook.
# --no-attach registers fragments and Named Values only (your APIs include
# the fragments in their own policy). --monolith deploys the generated
# single-file policy/straiker-policy.xml instead (portal-parity, rich only).
# STRAIKER_CLIENT_KEYS_JSON='{"alice@contoso.com":"<key>"}' also registers the
# straiker-gateway-auth fragment (per-developer keys for Claude Code).
# --key-named-value stores the key under another Named Value name (default
# straiker-api-key) so a v1 and a v3 key can coexist on one instance while you
# migrate; the thin policy then sets straikerApiKey from it.
#
# Requires: az cli logged in (`az login`), bicep installed (`az bicep install`).

set -euo pipefail

RG="${1:?usage: $0 <resource-group> <apim-name> [--no-test-api] [--no-attach] [--monolith] [--contract v3|rich|webhook]}"
APIM="${2:?usage: $0 <resource-group> <apim-name> [--no-test-api] [--no-attach] [--monolith] [--contract v3|rich|webhook]}"
shift 2

CREATE_TEST_API="true"
ATTACH_POLICY="true"
USE_MONOLITH="false"
CONTRACT="v3"
KEY_NV="straiker-api-key"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-test-api) CREATE_TEST_API="false"; shift ;;
    --no-attach)   CREATE_TEST_API="false"; ATTACH_POLICY="false"; shift ;;
    --monolith)    USE_MONOLITH="true"; shift ;;
    --contract)    CONTRACT="${2:?--contract needs v3|rich|webhook}"; shift 2 ;;
    --key-named-value) KEY_NV="${2:?--key-named-value needs a name}"; shift 2 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done
[[ "$CONTRACT" == "v3" || "$CONTRACT" == "rich" || "$CONTRACT" == "webhook" ]] || { echo "--contract must be v3, rich or webhook"; exit 1; }
[[ "$USE_MONOLITH" == "true" && "$CONTRACT" != "rich" ]] && { echo "--monolith supports the rich contract only"; exit 1; }

: "${STRAIKER_API_KEY:?STRAIKER_API_KEY env var required}"
case "$CONTRACT" in
  v3)   [[ "$STRAIKER_API_KEY" == sk_agt_* ]] || { echo "--contract v3 needs an sk_agt_ integration key (yours is not); a UUID key belongs to --contract rich|webhook"; exit 1; } ;;
  *)    [[ "$STRAIKER_API_KEY" == sk_agt_* ]] && { echo "--contract $CONTRACT needs a v1 UUID key; an sk_agt_ key belongs to --contract v3"; exit 1; } ;;
esac
CLIENT_KEYS_JSON="${STRAIKER_CLIENT_KEYS_JSON:-}"

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
      attachPolicy="$ATTACH_POLICY" \
      contract="$CONTRACT" \
      keyNamedValueName="$KEY_NV" \
      clientKeysJson="$CLIENT_KEYS_JSON" \
      ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"} \
  --query 'properties.outputs' \
  -o json

echo "==> Done. Test with tests/test.sh once the deployment completes."
