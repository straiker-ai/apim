"""Settings for the tests/v3 harness, read in one place.

Precedence (later wins):
  1. the file named by $STRAIKER_ENV_FILE (optional; e.g. a secrets file kept outside the repo)
  2. tests/v3/.env (gitignored; setup_dev_apis.py writes the gateway and test-client entries)
  3. the process environment

Keys:
  APIM_GATEWAY_URL, APIM_SUBSCRIPTION_KEY,
  E2E_KEY_ALICE, E2E_KEY_RAJ, E2E_TEST_TOKEN     written by setup_dev_apis.py
  STRAIKER_API_KEY          v3 integration key (sk_agt_...) the test APIs send to Straiker
  STRAIKER_PLATFORM_PAT     Platform API personal access token (console, enumeration, block checks)
  STRAIKER_INTEGRATION_ID   the gateway integration that key belongs to (intg_...)
  STRAIKER_API_BASE         default https://api.prod.straiker.ai
  FOUNDRY_TENANT_ID, FOUNDRY_CLIENT_ID, FOUNDRY_CLIENT_SECRET, FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL
  OPENAI_API_KEY, ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
  AZURE_OPENAI_API_VERSION                        upstream credentials, read by setup_dev_apis.py only
"""
from __future__ import annotations

import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def load_env(path: pathlib.Path) -> dict[str, str]:
    """KEY=value lines (an `export ` prefix and surrounding quotes are allowed). A missing file is empty."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.replace("export ", "").strip()] = v.strip().strip('"')
    return env


def settings() -> dict[str, str]:
    merged: dict[str, str] = {}
    extra = os.environ.get("STRAIKER_ENV_FILE", "")
    if extra:
        merged.update(load_env(pathlib.Path(extra).expanduser()))
    merged.update(load_env(HERE / ".env"))
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


def require(s: dict[str, str], *keys: str) -> None:
    missing = [k for k in keys if not s.get(k)]
    if missing:
        raise SystemExit(f"missing settings: {', '.join(missing)} (tests/v3/.env, $STRAIKER_ENV_FILE or the environment; see tests/v3/harness_env.py)")


API_BASE = settings().get("STRAIKER_API_BASE", "https://api.prod.straiker.ai").rstrip("/")
