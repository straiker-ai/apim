#!/usr/bin/env python3
"""Register the v3 fragments and create the e2e test APIs on a dev APIM instance.

Everything goes through ARM (the fragment PUT is the only compile check APIM
offers). Idempotent: rerun after editing a fragment to re-register it.

Creates on the instance:
  Named Values : straiker-v3-api-key (sk_agt_), straiker-client-keys (per-dev map),
                 straiker-test-token, openai-backend-key, anthropic-backend-key, aoai-backend-key
  Fragments    : straiker-v3-inbound, straiker-v3-outbound, straiker-gateway-auth
  APIs         : v3-openai      /v3-openai/*     -> https://api.openai.com   (subscription)
                 v3-anthropic   /v3-anthropic/*  -> https://api.anthropic.com (per-developer keys, Claude Code)
                 v3-aoai        /v3-aoai/*       -> Azure OpenAI resource     (subscription)
  Subscription : e2e-v3 on product "unlimited" (key written to tests/v3/.env)

The test-API policies accept `x-test-<knob>` request headers and copy them into
the straiker* variables before including the fragment, so one API drives the
whole configuration matrix, but only when the request also carries
`x-test-token` equal to the secret Named Value straiker-test-token (without the
gate, a subscription key alone could repoint straikerDetectUrl and receive the
Straiker key). The x-test-* headers never reach the model provider. That block
exists ONLY in these test policies.

Settings (tests/v3/harness_env.py; --env-file adds a file, default $STRAIKER_ENV_FILE):
  STRAIKER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
from harness_env import load_env, settings  # noqa: E402
FRAGMENTS = REPO / "policy" / "fragments"
API_VERSION = "2024-06-01-preview"

KNOBS = [
    "straikerFailClosed", "straikerResponsePhase", "straikerBlockMode", "straikerDetectUrl",
    "straikerTimeoutSec", "straikerScoreRequest", "straikerScoreResponse", "straikerMaxBodyBytes",
    "straikerAgentRef", "straikerAllowCallerAgent", "straikerClient", "straikerIdentityMode",
    "straikerReplayMemory", "straikerFormatHint", "straikerClaudeAgentName", "straikerApiKey",
    "straikerAgentNameFrom", "straikerAllowCustomDetectUrl", "straikerBlockUnparseable",
]


def knob_passthrough() -> str:
    sets, deletes = [], []
    for k in KNOBS:
        h = "x-test-" + k[len("straiker"):].lower()
        sets.append(
            f'        <choose><when condition="@(context.Request.Headers.ContainsKey(\\"{h}\\"))">'
            f'<set-variable name="{k}" value="@(context.Request.Headers.GetValueOrDefault(\\"{h}\\", \\"\\"))" /></when></choose>\n'
        )
        deletes.append(f'    <set-header name="{h}" exists-action="delete" />\n')
    gate = ('    <choose>\n'
            '      <when condition="@(context.Request.Headers.GetValueOrDefault(\\"x-test-token\\", \\"\\") == \\"{{straiker-test-token}}\\")">\n'
            + "".join(sets)
            + '      </when>\n    </choose>\n')
    return (gate + "".join(deletes) + '    <set-header name="x-test-token" exists-action="delete" />\n').replace('\\"', '"')


def policy_openai(key_nv: str, backend_header: str, backend_value: str, extra_inbound: str = "", gateway_auth: bool = False) -> str:
    auth = '    <include-fragment fragment-id="straiker-gateway-auth" />\n' if gateway_auth else ""
    return f"""<policies>
  <inbound>
    <base />
{auth}    <set-variable name="straikerApiKey" value="{{{{{key_nv}}}}}" />
{knob_passthrough()}{extra_inbound}    <include-fragment fragment-id="straiker-v3-inbound" />
    <set-header name="{backend_header}" exists-action="override">
      <value>{backend_value}</value>
    </set-header>
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
    <include-fragment fragment-id="straiker-v3-outbound" />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
"""


class Arm:
    def __init__(self, sub: str, rg: str, apim: str):
        tok = subprocess.run(["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True, check=True).stdout.strip()
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        self.base = f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ApiManagement/service/{apim}"

    def put(self, path: str, body: dict, wait: bool = True) -> dict:
        r = requests.put(f"{self.base}/{path}?api-version={API_VERSION}", headers=self.h, json=body, timeout=120)
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"PUT {path} -> {r.status_code}: {r.text[:1500]}")
        # APIM validates policies and fragments ASYNCHRONOUSLY: the PUT can answer 200 (echoing the resource as it
        # was) while the validation that follows rejects the new value. Always follow the async operation to its
        # end, or a compile error is silently reported as success and the old policy keeps running.
        loc = r.headers.get("Azure-AsyncOperation") or r.headers.get("Location")
        if wait and loc:
            for _ in range(120):
                p = requests.get(loc, headers=self.h, timeout=60)
                t = p.text.lstrip("\ufeff")
                if p.status_code == 202:
                    time.sleep(2)
                    continue
                if p.status_code >= 400:
                    raise RuntimeError(f"PUT {path} rejected ({p.status_code}): {t[:2000]}")
                try:
                    j = json.loads(t) if t.strip() else {}
                except ValueError:
                    j = {}
                st = (j.get("status") or "").lower()
                if st in ("failed", "canceled", "cancelled"):
                    raise RuntimeError(f"PUT {path} failed: {t[:2000]}")
                if st in ("inprogress", "running", "accepted", "notstarted"):
                    time.sleep(2)
                    continue
                return j
            raise RuntimeError(f"PUT {path}: async operation did not finish")
        try:
            return json.loads(r.text.lstrip("\ufeff")) if r.text.strip() else {}
        except ValueError:
            return {}

    def post(self, path: str, body: dict | None = None) -> dict:
        r = requests.post(f"{self.base}/{path}?api-version={API_VERSION}", headers=self.h, json=body or {}, timeout=60)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.text.strip() else {}

    def get(self, path: str, params: str = "") -> dict | None:
        r = requests.get(f"{self.base}/{path}?api-version={API_VERSION}{params}", headers=self.h, timeout=60)
        return json.loads(r.text.lstrip("\ufeff")) if r.status_code == 200 else None


def named_value(arm: Arm, name: str, value: str, secret: bool = True) -> None:
    arm.put(f"namedValues/{name}", {"properties": {"displayName": name, "secret": secret, "value": value}})
    print(f"  named value {name}: ok")


def fragment(arm: Arm, name: str, description: str) -> None:
    xml = (FRAGMENTS / f"{name}.xml").read_text()
    t = time.time()
    arm.put(f"policyFragments/{name}", {"properties": {"format": "rawxml", "value": xml, "description": description}})
    # Read it back: the only proof the NEW value is live is the stored value, not the PUT's status.
    # APIM re-serializes what it stores (tabs for indentation, blank lines dropped, multi-line tags joined),
    # so compare with whitespace removed: that still proves the new content, not the old, is what runs.
    stored = ((arm.get(f"policyFragments/{name}", "&format=rawxml") or {}).get("properties") or {}).get("value", "")
    norm = lambda x: re.sub(r"\s+", "", x)  # noqa: E731
    if norm(stored) != norm(xml):
        raise RuntimeError(f"fragment {name}: stored value differs from {FRAGMENTS / (name + '.xml')} ({len(stored)} vs {len(xml)} chars)")
    print(f"  fragment {name}: compiled + registered + read back ({time.time() - t:.1f}s, {len(xml)} chars)")


def api(arm: Arm, api_id: str, path: str, backend: str, sub_required: bool, policy: str, display: str | None = None) -> None:
    arm.put(f"apis/{api_id}", {"properties": {"displayName": display or api_id, "path": path, "protocols": ["https"], "serviceUrl": backend, "subscriptionRequired": sub_required}})
    for op, method in (("any-post", "POST"), ("any-get", "GET"), ("any-delete", "DELETE")):
        try:
            arm.put(f"apis/{api_id}/operations/{op}", {"properties": {"displayName": f"{method} *", "method": method, "urlTemplate": "/*"}})
        except RuntimeError as e:
            if "same method and URL template already exists" not in str(e):
                raise
    arm.put(f"apis/{api_id}/policies/policy", {"properties": {"format": "rawxml", "value": policy}})
    arm.put(f"products/unlimited/apis/{api_id}", {})
    print(f"  api {api_id} (/{path}) -> {backend}: ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=os.environ.get("AZURE_SUBSCRIPTION_ID", ""), help="Azure subscription id (or AZURE_SUBSCRIPTION_ID)")
    ap.add_argument("--rg", default=os.environ.get("APIM_RG", ""), help="resource group of the dev APIM instance (or APIM_RG)")
    ap.add_argument("--apim", default=os.environ.get("APIM_NAME", ""), help="dev APIM instance name (or APIM_NAME)")
    ap.add_argument("--env-file", default=os.environ.get("STRAIKER_ENV_FILE", ""), help="extra KEY=value file (default $STRAIKER_ENV_FILE)")
    ap.add_argument("--fragments-only", action="store_true")
    a = ap.parse_args()
    if not (a.sub and a.rg and a.apim):
        print("need --sub/--rg/--apim (or AZURE_SUBSCRIPTION_ID / APIM_RG / APIM_NAME)")
        return 2

    if a.env_file:
        os.environ["STRAIKER_ENV_FILE"] = a.env_file
    env = settings()
    env.setdefault("ANTHROPIC_API_KEY", env.get("ANTHROPIC_KEY", ""))
    need = ["STRAIKER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        print("missing settings:", missing)
        return 2
    if not env["STRAIKER_API_KEY"].startswith("sk_agt_"):
        print("STRAIKER_API_KEY must be a v3 integration key (sk_agt_...)")
        return 2

    arm = Arm(a.sub, a.rg, a.apim)
    print(f"== {a.apim} ({a.rg})")

    print("-- named values")
    named_value(arm, "straiker-v3-api-key", env["STRAIKER_API_KEY"])
    local_env = HERE / ".env"
    existing = load_env(local_env)
    test_token = existing.get("E2E_TEST_TOKEN") or secrets.token_urlsafe(32)
    named_value(arm, "straiker-test-token", test_token)
    client_keys = {
        "alice@e2e.test": existing.get("E2E_KEY_ALICE") or "e2e-" + secrets.token_urlsafe(24),
        "raj@e2e.test": existing.get("E2E_KEY_RAJ") or "e2e-" + secrets.token_urlsafe(24),
    }
    named_value(arm, "straiker-client-keys", json.dumps(client_keys))
    named_value(arm, "openai-backend-key", env["OPENAI_API_KEY"])
    named_value(arm, "anthropic-backend-key", env["ANTHROPIC_API_KEY"])
    named_value(arm, "aoai-backend-key", env["AZURE_OPENAI_API_KEY"])

    print("-- fragments (server-side compile)")
    fragment(arm, "straiker-gateway-auth", "Per-developer gateway auth (x-api-key or Bearer -> email)")
    fragment(arm, "straiker-v3-inbound", "Straiker v3 platform request phase (/api/v3/detect, Kong/LiteLLM parity)")
    fragment(arm, "straiker-v3-outbound", "Straiker v3 platform response phase (response-sync envelope)")
    if a.fragments_only:
        return 0

    print("-- apis")
    api(arm, "v3-openai", "v3-openai", "https://api.openai.com", True,
        policy_openai("straiker-v3-api-key", "Authorization", "Bearer {{openai-backend-key}}"))
    anthropic_extra = ('    <set-header name="anthropic-version" exists-action="skip">\n      <value>2023-06-01</value>\n    </set-header>\n'
                       '    <set-header name="Authorization" exists-action="delete" />\n')
    api(arm, "v3-anthropic", "v3-anthropic", "https://api.anthropic.com", False,
        policy_openai("straiker-v3-api-key", "x-api-key", "{{anthropic-backend-key}}", extra_inbound=anthropic_extra, gateway_auth=True))
    aoai_extra = '    <set-header name="Authorization" exists-action="delete" />\n'
    api(arm, "v3-aoai", "v3-aoai", env["AZURE_OPENAI_ENDPOINT"].rstrip("/"), True,
        policy_openai("straiker-v3-api-key", "api-key", "{{aoai-backend-key}}", extra_inbound=aoai_extra))

    # OpenAI-compatible route for CLIs that cannot send a subscription header (Codex, OpenCode): per-developer keys via gateway-auth
    api(arm, "v3-openai-cli", "v3-openai-cli", "https://api.openai.com", False,
        policy_openai("straiker-v3-api-key", "Authorization", "Bearer {{openai-backend-key}}", gateway_auth=True))
    # Azure AI Foundry models endpoint (Mistral / Phi / gpt-4.1 under the same resource)
    api(arm, "v3-foundry", "v3-foundry", env["AZURE_OPENAI_ENDPOINT"].rstrip("/").replace(".cognitiveservices.azure.com", ".services.ai.azure.com"), True,
        policy_openai("straiker-v3-api-key", "api-key", "{{aoai-backend-key}}", extra_inbound=aoai_extra))
    # Entra app-only token as the identity (validate-azure-ad-token -> validatedJwt -> straikerIdentityMode=jwt).
    # Tenant and client ids come from the environment (tests/v3/.env FOUNDRY_* or ENTRA_TEST_*); skipped when absent.
    entra_tenant = env.get("ENTRA_TEST_TENANT_ID") or env.get("FOUNDRY_TENANT_ID", "")
    entra_client = env.get("ENTRA_TEST_CLIENT_ID") or env.get("FOUNDRY_CLIENT_ID", "")
    entra_extra = (f'    <validate-azure-ad-token tenant-id="{entra_tenant}" output-token-variable-name="validatedJwt">\n'
                   f'      <client-application-ids>\n        <application-id>{entra_client}</application-id>\n      </client-application-ids>\n'
                   f'      <audiences>\n        <audience>https://management.azure.com</audience>\n        <audience>https://management.azure.com/</audience>\n      </audiences>\n'
                   f'    </validate-azure-ad-token>\n    <set-variable name="straikerIdentityMode" value="jwt" />\n')
    if entra_tenant and entra_client:
        api(arm, "v3-entra", "v3-entra", "https://api.openai.com", False,
            policy_openai("straiker-v3-api-key", "Authorization", "Bearer {{openai-backend-key}}", extra_inbound=entra_extra))
    else:
        print("  api v3-entra: skipped (set FOUNDRY_TENANT_ID / FOUNDRY_CLIENT_ID in tests/v3/.env)")

    # Automatic enumeration: display name differs from the id, so the minted agent name shows which one the fragment uses
    api(arm, "v3-enum", "v3-enum", "https://api.openai.com", True,
        policy_openai("straiker-v3-api-key", "Authorization", "Bearer {{openai-backend-key}}"), display="Contoso Claims Assistant")
    # Azure AI Foundry Agent Service (threads/runs): the client's Entra token passes through to the project
    local = env
    if local.get("FOUNDRY_PROJECT_ENDPOINT"):
        ft = local["FOUNDRY_TENANT_ID"]
        foundry_pol = f"""<policies>
  <inbound>
    <base />
    <set-variable name="straikerApiKey" value="{{{{straiker-v3-api-key}}}}" />
    <set-variable name="straikerAgentRef" value="APIM Foundry Agent" />
    <set-variable name="straikerIdentityMode" value="jwt" />
    <validate-azure-ad-token tenant-id="{ft}" output-token-variable-name="validatedJwt" failed-validation-httpcode="401">
      <audiences><audience>https://ai.azure.com</audience><audience>https://cognitiveservices.azure.com</audience></audiences>
    </validate-azure-ad-token>
    <include-fragment fragment-id="straiker-v3-inbound" />
  </inbound>
  <backend><base /></backend>
  <outbound><base /><include-fragment fragment-id="straiker-v3-outbound" /></outbound>
  <on-error><base /></on-error>
</policies>"""
        api(arm, "v3-foundry-agent", "v3-foundry-agent", local["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/"), False, foundry_pol)

    print("-- subscription")
    arm.put("subscriptions/e2e-v3", {"properties": {"scope": "/products/unlimited", "displayName": "e2e-v3", "state": "active"}})
    sec = arm.post("subscriptions/e2e-v3/listSecrets")
    gw = arm.get("")["properties"]["gatewayUrl"]
    managed = {
        "APIM_GATEWAY_URL": gw, "APIM_SUBSCRIPTION_KEY": sec["primaryKey"],
        "E2E_KEY_ALICE": client_keys["alice@e2e.test"], "E2E_KEY_RAJ": client_keys["raj@e2e.test"], "E2E_TEST_TOKEN": test_token,
        "AOAI_DEPLOYMENT": "gpt-4.1", "AOAI_API_VERSION": env.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
    }
    merged = {**existing, **managed}   # entries this script does not manage (FOUNDRY_*, STRAIKER_*) are kept
    local_env.write_text("".join(f"{k}={v}\n" for k, v in merged.items()))
    local_env.chmod(0o600)
    print(f"  wrote {local_env} (gateway {gw})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
