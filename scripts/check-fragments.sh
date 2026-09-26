#!/usr/bin/env bash
# Static gates for the policy fragments. APIM policy XML is not well-formed XML
# (raw quotes and < > inside expressions), so these are text checks; the only
# real compile check is a server-side PUT (deploy.sh does that implicitly).
set -euo pipefail
cd "$(dirname "$0")/.."
status=0
for f in policy/fragments/*.xml; do
  # 1. Every <fragment> opens and closes; choose/when/otherwise/retry balance.
  # Open tags may span lines (multi-line condition attributes), so count the tag
  # start token, not a whole single-line element. None of these tags is ever
  # written self-closing in this repo.
  for tag in fragment choose when otherwise retry send-request send-one-way-request return-response; do
    o=$({ grep -oE "<$tag([[:space:]>]|$)" "$f" || true; } | wc -l | tr -d ' ')
    c=$({ grep -oE "</$tag>" "$f" || true; } | wc -l | tr -d ' ')
    if [[ "$o" != "$c" ]]; then echo "FAIL $f: <$tag> opens $o closes $c"; status=1; fi
  done
  # 2. send-one-way-request timeout must be a literal int (APIM rejects expressions at save).
  if grep -n 'send-one-way-request' "$f" | grep -q 'timeout="@'; then echo "FAIL $f: send-one-way-request timeout must be a literal"; status=1; fi
done
# 3. The block-stub builder is duplicated in the v3 pair (fragments cannot include fragments); keep the two copies identical.
extract() { awk '/STRAIKER_V3_STUB_BEGIN/{f=1} f{print} /STRAIKER_V3_STUB_END/{f=0}' "$1" | sed 's/^[[:space:]]*//'; }
if ! diff <(extract policy/fragments/straiker-v3-inbound.xml) <(extract policy/fragments/straiker-v3-outbound.xml) >/dev/null; then
  echo "FAIL: STRAIKER_V3_STUB block differs between straiker-v3-inbound.xml and straiker-v3-outbound.xml"; status=1
fi
# 4. v3 fragments never send v1-era headers or the legacy route.
if { grep -n -i -E 'x-tool|x-straiker-phase|x-straiker-user|Straiker-Debug|/api/v1/' policy/fragments/straiker-v3-*.xml || true; } | grep -v -E '^[^:]+:[0-9]+:\s*(<!--|.*belongs to the v1|.*straiker-defendai|.*v1 UUID)' | grep -q .; then
  echo "FAIL: v1-era header or route in a v3 fragment"; status=1
fi
# 5. Test fixtures parse.
for j in tests/fixtures/*.json tests/v3/fixtures/*.json; do [[ -f "$j" ]] || continue; python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$j" || { echo "FAIL: $j is not JSON"; status=1; }; done
[[ $status -eq 0 ]] && echo "OK: fragment checks passed"
exit $status
