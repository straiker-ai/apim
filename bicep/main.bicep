// Deploys the Straiker DefendAI guardrail policy to an existing Azure API
// Management instance. Creates:
//   1. straiker-api-key Named Value (secret)
//   2. straiker-defendai-inbound / straiker-defendai-outbound Policy Fragments
//      (the canonical deployment artifact; skipped when useMonolith=true)
//   3. (optional) test API pointing at api.openai.com
//   4. Attaches the policy to the target API - a thin include-fragment policy
//      by default, or the generated single-file policy when useMonolith=true
//
// Run via deploy/deploy.sh - it sets straikerApiKey from $STRAIKER_API_KEY.

@description('Name of the existing API Management instance.')
param apimName string

@description('Straiker DefendAI API key. Stored as an APIM Named Value secret.')
@secure()
param straikerApiKey string

@description('Name of the API to attach the Straiker policy to. If createTestApi=true, this API is created pointing at api.openai.com.')
param targetApiName string = 'openai-protected'

@description('If true, create a passthrough OpenAI test API. If false, expects targetApiName to already exist.')
param createTestApi bool = true

@description('Backend service URL for the test API (only used if createTestApi=true).')
param testBackendUrl string = 'https://api.openai.com'

@description('Deploy the generated single-file policy instead of fragments. Fragments are the canonical path; the monolith exists for portal paste-in parity.')
param useMonolith bool = false

@description('Detect contract: "rich" = /api/v1/detect[?agentic] with local score>threshold blocking (shipping default); "webhook" = /api/v1/detect/webhook with server-side action blocking (Bridge convergence). Both fragment pairs are registered either way; this picks which pair the thin policy includes.')
@allowed(['rich', 'webhook'])
param contract string = 'rich'

@description('Inline policy XML. Only used when useMonolith=true; the deploy script substitutes policy/straiker-policy.xml here.')
param policyXml string = ''

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' existing = {
  name: apimName
}

// 1. Named Value for the Straiker API key.
//    Production: switch the `value` to a Key Vault reference via keyVault block.
resource straikerKeyNV 'Microsoft.ApiManagement/service/namedValues@2023-05-01-preview' = {
  parent: apim
  name: 'straiker-api-key'
  properties: {
    displayName: 'straiker-api-key'
    secret: true
    value: straikerApiKey
  }
}

// 2. Policy Fragments - registered instance-wide so any API can pull them in
//    with <include-fragment>. APIM validates the {{straiker-api-key}} reference
//    at save time, hence dependsOn the Named Value.
resource inboundFragment 'Microsoft.ApiManagement/service/policyFragments@2023-05-01-preview' = if (!useMonolith) {
  parent: apim
  name: 'straiker-defendai-inbound'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policy/fragments/straiker-defendai-inbound.xml')
    description: 'Straiker DefendAI pre-call detection (blocks HTTP 403 when score > threshold)'
  }
  dependsOn: [
    straikerKeyNV
  ]
}

resource outboundFragment 'Microsoft.ApiManagement/service/policyFragments@2023-05-01-preview' = if (!useMonolith) {
  parent: apim
  name: 'straiker-defendai-outbound'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policy/fragments/straiker-defendai-outbound.xml')
    description: 'Straiker DefendAI post-call detection (observability; blocks when straikerBlockOnPostCall=true)'
  }
  dependsOn: [
    straikerKeyNV
  ]
}

resource webhookInboundFragment 'Microsoft.ApiManagement/service/policyFragments@2023-05-01-preview' = if (!useMonolith) {
  parent: apim
  name: 'straiker-webhook-inbound'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policy/fragments/straiker-webhook-inbound.xml')
    description: 'Straiker DefendAI pre-call detection via /detect/webhook (action-based blocking, Bridge convergence)'
  }
  dependsOn: [
    straikerKeyNV
  ]
}

resource webhookOutboundFragment 'Microsoft.ApiManagement/service/policyFragments@2023-05-01-preview' = if (!useMonolith) {
  parent: apim
  name: 'straiker-webhook-outbound'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policy/fragments/straiker-webhook-outbound.xml')
    description: 'Straiker DefendAI post-call detection via /detect/webhook (enforce/observe/off; server-side turn dedup)'
  }
  dependsOn: [
    straikerKeyNV
  ]
}

// Thin per-API policy used in fragment mode. Per-API knobs (straikerSource,
// straikerAgentic, ...) belong in the consuming API's own policy before the
// include-fragment lines - see policy/examples/.
var inboundFragmentId = contract == 'webhook' ? 'straiker-webhook-inbound' : 'straiker-defendai-inbound'
var outboundFragmentId = contract == 'webhook' ? 'straiker-webhook-outbound' : 'straiker-defendai-outbound'

var fragmentPolicyXml = '''
<policies>
  <inbound>
    <base />
    <include-fragment fragment-id="__INBOUND__" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
    <include-fragment fragment-id="__OUTBOUND__" />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''

var effectivePolicyXml = useMonolith
  ? policyXml
  : replace(replace(fragmentPolicyXml, '__INBOUND__', inboundFragmentId), '__OUTBOUND__', outboundFragmentId)

// 3. Optional passthrough test API (mirrors kong-plugin-demo openai-standard route).
resource testApi 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = if (createTestApi) {
  parent: apim
  name: targetApiName
  properties: {
    displayName: targetApiName
    path: 'protected'
    protocols: ['https']
    serviceUrl: testBackendUrl
    subscriptionRequired: true
  }
}

resource testOp 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = if (createTestApi) {
  parent: testApi
  name: 'chat-completions'
  properties: {
    displayName: 'POST chat/completions'
    method: 'POST'
    urlTemplate: '/v1/chat/completions'
  }
}

// 4. Apply the Straiker policy to the API. dependsOn the named value + fragments
//    so {{straiker-api-key}} and <include-fragment> references resolve at save.
resource attachExisting 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = if (!createTestApi) {
  name: '${apimName}/${targetApiName}/policy'
  properties: {
    format: 'rawxml'
    value: effectivePolicyXml
  }
  dependsOn: [
    straikerKeyNV
    inboundFragment
    outboundFragment
    webhookInboundFragment
    webhookOutboundFragment
  ]
}

resource attachNew 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = if (createTestApi) {
  parent: testApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: effectivePolicyXml
  }
  dependsOn: [
    straikerKeyNV
    inboundFragment
    outboundFragment
    webhookInboundFragment
    webhookOutboundFragment
  ]
}

output apiName string = targetApiName
output gatewayUrl string = apim.properties.gatewayUrl
output testEndpoint string = createTestApi ? '${apim.properties.gatewayUrl}/protected/v1/chat/completions' : ''
output deployedMode string = useMonolith ? 'monolith' : 'fragments'
