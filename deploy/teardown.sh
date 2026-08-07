#!/usr/bin/env bash
# Tear down everything created by deploy.sh and the APIM provisioning.
#
# Deletes the entire resource group, which removes:
#   - The APIM instance (stops the ~$0.07/hr Developer-tier charge)
#   - The Straiker policy + Named Value
#   - The test API
#   - Any Self-hosted Gateway entities
#
# Usage:
#   ./teardown.sh <resource-group>

set -euo pipefail
RG="${1:?usage: $0 <resource-group>   e.g. straiker-apim-dev}"

echo "==> Resources currently in '$RG':"
az resource list --resource-group "$RG" \
  --query "[].{name:name,type:type}" -o table 2>&1 | head -20 || true

echo ""
read -p "Delete resource group '$RG' and EVERYTHING in it? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

echo "==> Deleting resource group '$RG' (background; takes a few minutes)..."
az group delete --name "$RG" --yes --no-wait
echo "==> Delete request submitted. Track with: az group show -n $RG -o table"
echo "==> Or watch until gone: az group wait --deleted -n $RG"
