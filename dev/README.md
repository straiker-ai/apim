# Local dev loop with the APIM self-hosted gateway

This is the fast iteration path: edit `policy/straiker-policy.xml`, push to APIM with `deploy/deploy.sh`, the self-hosted gateway picks it up within ~30 s, hit `localhost:5000` with curl. No portal clicks.

## One-time setup

1. Create an APIM instance (free Developer tier):
   ```bash
   az group create --name straiker-apim-dev --location eastus
   az apim create --resource-group straiker-apim-dev \
                  --name <your-apim-name> \
                  --publisher-name "Straiker Dev" \
                  --publisher-email you@example.com \
                  --sku-name Developer --no-wait
   ```
   Provisioning takes ~30 minutes. Watch with `az apim wait --created --name <name> --resource-group straiker-apim-dev`.

2. In the Azure portal, register a Self-hosted Gateway:
   - APIM resource → **Gateways** → **+ Add gateway**
   - Name it (e.g. `local-dev`), pick the location closest to you
   - Open the new gateway → **Deployment** → copy `config.service.endpoint` and `config.service.auth` into `.env`

3. Deploy the Straiker policy + test API once via Bicep:
   ```bash
   STRAIKER_API_KEY=xxxx ../deploy/deploy.sh straiker-apim-dev <your-apim-name>
   ```

4. Assign the test API to the self-hosted gateway:
   - APIM → APIs → `openai-protected` → **Settings** → Gateways → check `local-dev`

## Run

```bash
cp .env.example .env  # fill in APIM_CONFIG_ENDPOINT and APIM_CONFIG_AUTH
docker-compose up
```

Test:
```bash
APIM_GATEWAY_URL=http://localhost:5000 \
APIM_SUBSCRIPTION_KEY=<from APIM Subscriptions blade> \
OPENAI_API_KEY=sk-... \
../tests/test.sh
```

## Iteration loop

```
edit policy/straiker-policy.xml
  → ../deploy/deploy.sh straiker-apim-dev <apim>
  → wait ~30s for self-hosted gateway to pull new config
  → ../tests/test.sh
```

To skip the gateway entirely and hit the managed APIM gateway directly:
```bash
APIM_GATEWAY_URL=https://<your-apim-name>.azure-api.net ../tests/test.sh
```
