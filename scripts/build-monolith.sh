#!/usr/bin/env bash
# Regenerates policy/straiker-policy.xml (the single-file paste-in policy) from
# the canonical policy fragments. The fragments are the source of truth; the
# monolith is a convenience artifact for portal paste-in users.
#
# Usage:
#   scripts/build-monolith.sh            # rewrite policy/straiker-policy.xml
#   scripts/build-monolith.sh --check    # exit 1 if the monolith is out of date (CI)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INBOUND="$REPO/policy/fragments/straiker-defendai-inbound.xml"
OUTBOUND="$REPO/policy/fragments/straiker-defendai-outbound.xml"
TARGET="$REPO/policy/straiker-policy.xml"

[[ -f "$INBOUND" ]]  || { echo "missing $INBOUND"; exit 1; }
[[ -f "$OUTBOUND" ]] || { echo "missing $OUTBOUND"; exit 1; }

# Emit the fragment body: everything between <fragment> and </fragment>, exclusive.
fragment_body() {
  awk '/^<fragment>$/{flag=1;next} /^<\/fragment>$/{flag=0} flag' "$1"
}

generate() {
cat <<'EOF'
<!--
  GENERATED FILE - do not edit by hand.
  Source of truth: policy/fragments/straiker-defendai-inbound.xml
                   policy/fragments/straiker-defendai-outbound.xml
  Regenerate with: scripts/build-monolith.sh

  Straiker DefendAI guardrail policy for Azure API Management (single-file
  paste-in variant of the policy fragments, for portal users who don't want
  to register fragments).

  Behaviour, knobs, and defaults are identical to the fragments:
    inbound  = pre-call detection + block (HTTP 403 when score > threshold)
    outbound = post-call detection; blocks when straikerBlockOnPostCall=true
  Override knobs (straikerAgentic, straikerSource, straikerMode,
  straikerThreshold, straikerFailOpen, straikerBlockOnPostCall, ...) by adding
  <set-variable> lines ABOVE the config-defaults block - each default only
  applies when the variable is not already set.

  Required Named Value: straiker-api-key (Key Vault-backed Secret recommended).
-->
<policies>
  <inbound>
    <base />
EOF
fragment_body "$INBOUND"
cat <<'EOF'
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
EOF
fragment_body "$OUTBOUND"
cat <<'EOF'
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
EOF
}

if [[ "${1:-}" == "--check" ]]; then
  if ! diff -u "$TARGET" <(generate) >/dev/null 2>&1; then
    echo "ERROR: policy/straiker-policy.xml is out of date with the fragments."
    echo "Run scripts/build-monolith.sh and commit the result."
    exit 1
  fi
  echo "OK: monolith matches fragments."
else
  generate > "$TARGET"
  echo "Regenerated $TARGET from fragments."
fi
