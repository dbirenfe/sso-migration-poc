# SSO Migration — Parallel RH-SSO / RHBK with Zero Downtime

Gradual migration from **RH-SSO 7.6.5** to **RHBK 26.4** with no application
code changes.  Cross-domain tokens are transparently exchanged by an
infrastructure-level proxy so that every application always receives tokens
from its own identity provider.

---

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │              DNS                     │
                        │  rhsso.apps.cluster.domain.com ──┐   │
                        │  rhbk.apps.cluster.domain.com  ──┤   │
                        └──────────────────────────────────┼───┘
                                                           │
                                                           ▼
              ┌─────────────────── OpenShift Cluster ───────────────────┐
              │                                                        │
              │  ┌──────────────────────────────────────┐              │
              │  │      API Gateway (NGINX)             │              │
              │  │      namespace: sso-gateway          │              │
              │  │                                      │              │
              │  │  ┌──────────┐      ┌──────────┐     │              │
              │  │  │ vhost:   │      │ vhost:   │     │              │
              │  │  │ rhsso.*  │      │ rhbk.*   │     │              │
              │  │  └────┬─────┘      └────┬─────┘     │              │
              │  └───────┼─────────────────┼────────────┘              │
              │          │                 │                           │
              │          │  (in-cluster    │  (in-cluster              │
              │          │   Service)      │   Service)                │
              │          ▼                 ▼                           │
              │  ┌────────────────┐  ┌────────────────┐               │
              │  │ RH-SSO 7.6.5  │  │ RHBK 26.4      │               │
              │  │ ns: rhsso     │  │ ns: rhbk       │               │
              │  │               │◄─── trust ───►│               │    │
              │  │ IdP: rhbk     │  │ IdP: rhsso     │               │
              │  │ Token Exchange│  │ JWT Auth Grant  │               │
              │  └────────────────┘  └────────────────┘               │
              └────────────────────────────────────────────────────────┘


         Cross-domain service-to-service calls:

        ┌───────────┐   RHBK     ┌─────────────────────┐  RH-SSO   ┌───────────┐
        │ System B  │  token     │ Token Exchange Proxy │  token    │ System A  │
        │ (backend) │ ────────►  │  (transparent)       │ ────────► │ (backend) │
        └───────────┘            │                      │           └───────────┘
                                 │  1. Detect issuer    │
                                 │  2. Exchange token   │
                                 │  3. Forward request  │
                                 └─────────────────────┘
```

### Components

| # | Component | Purpose | Location |
|---|-----------|---------|----------|
| 1 | **API Gateway** | Routes identity traffic by hostname to the correct IdP | `gateway/` |
| 2 | **Token Exchange Proxy** | Transparent reverse proxy that swaps cross-domain tokens | `token-exchange-proxy/` |
| 3 | **Keycloak Config** | IdP trust + exchange clients on both RH-SSO and RHBK | `keycloak-config/` |
| 4 | **Network Policies** | Lock down namespace traffic | `network-policy/` |

### How "Zero Code Changes" Works

The original problem: when System A migrates from RH-SSO to RHBK, System B
still holds RH-SSO tokens that System A no longer accepts.

The solution deploys a **Token Exchange Proxy** in front of System A.
The proxy inspects the `iss` claim in every incoming Bearer token:

- **Issuer matches** → forward the request untouched
- **Issuer mismatch** → exchange the token at the correct IdP's token endpoint,
  replace the `Authorization` header, then forward

The applications never see foreign tokens. No code changes required.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| OpenShift 4.12+ | Both RH-SSO and RHBK run on the same OCP cluster |
| `oc` CLI | Logged in with cluster-admin or project-admin |
| RH-SSO 7.6.5 | Deployed on OpenShift (e.g., namespace `rhsso`) |
| RHBK 26.4 | Deployed on OpenShift (e.g., namespace `rhbk`) |
| DNS control | Ability to create / modify A/CNAME records |
| TLS certificates | Wildcard or per-hostname certs for both identity hostnames |

---

## Deployment Steps

### Step 0 — Clone and review

```bash
# Review the files, replace all <PLACEHOLDER> values
grep -rn '<.*>' gateway/ token-exchange-proxy/ keycloak-config/
```

### Step 1 — Deploy the API Gateway

```bash
# Create the namespace
oc apply -f gateway/00-namespace.yaml

# Create TLS secrets (edit 01-tls-secret.yaml first with real certs)
oc apply -f gateway/01-tls-secret.yaml

# Deploy NGINX
oc apply -f gateway/02-configmap.yaml
oc apply -f gateway/03-deployment.yaml
oc apply -f gateway/04-service.yaml

# Create the OpenShift Routes
oc apply -f gateway/05-route-rhsso.yaml
oc apply -f gateway/06-route-rhbk.yaml

# Verify
oc -n sso-gateway get pods,svc,routes
```

### Step 2 — Update DNS

Point both hostnames to the OpenShift Router's external IP/CNAME:

```
rhsso.apps.cluster.domain.com  →  <OpenShift Router IP or CNAME>
rhbk.apps.cluster.domain.com   →  <OpenShift Router IP or CNAME>
```

Verify routing:
```bash
curl -sk https://rhsso.apps.cluster.domain.com/auth/realms/master
curl -sk https://rhbk.apps.cluster.domain.com/realms/master
```

### Step 3 — Configure Keycloak trust (both directions)

Follow the detailed guide in `keycloak-config/SETUP.md`.

Summary:
1. **RH-SSO**: Add RHBK as OIDC IdP, create `token-exchange-client`, enable Token Exchange
2. **RHBK**: Add RH-SSO as OIDC IdP, create `token-exchange-client`, enable JWT Authorization Grant

Verify with the curl commands in `SETUP.md`.

### Step 4 — Build and deploy the Token Exchange Proxy

```bash
# Create the BuildConfig and ImageStream
oc apply -f token-exchange-proxy/04-buildconfig.yaml

# Build the image from local source
oc -n sso-gateway start-build token-exchange-proxy \
  --from-dir=./token-exchange-proxy --follow

# Edit the ConfigMaps and Secrets with real values
# (see token-exchange-proxy/00-configmap.yaml and 01-secret.yaml)
oc apply -f token-exchange-proxy/00-configmap.yaml
oc apply -f token-exchange-proxy/01-secret.yaml

# Deploy
oc apply -f token-exchange-proxy/02-deployment.yaml
oc apply -f token-exchange-proxy/03-service.yaml

# Verify
oc -n sso-gateway get pods -l app.kubernetes.io/component=token-exchange-proxy
```

### Step 5 — Wire the proxy into the service mesh

For each service that needs cross-domain token support, update the
calling service's configuration to point to the Token Exchange Proxy
**instead of** the target service directly.

**Example:** System B currently calls `http://system-a-service:8080`.
Update its service endpoint (environment variable, ConfigMap, etc.) to
`http://token-proxy-legacy.sso-gateway.svc.cluster.local:8080`.

> This is an **infrastructure/config change**, not a code change.
> The application code stays the same.

Alternative: use an OpenShift Service + ExternalName to transparently
redirect at the DNS level:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: system-a-service          # same name the consumer already uses
  namespace: app-b-namespace      # in System B's namespace
spec:
  type: ExternalName
  externalName: token-proxy-legacy.sso-gateway.svc.cluster.local
```

### Step 6 — Apply network policies

```bash
oc apply -f network-policy/network-policy.yaml

# Label the IdP namespaces so the gateway and proxy can reach them
oc label namespace rhsso sso-migration/idp=true
oc label namespace rhbk  sso-migration/idp=true

# Label application namespaces that should be allowed to reach the proxy
oc label namespace app-b-namespace sso-migration/access=true
```

### Step 7 — Run the E2E test

```bash
chmod +x tests/test-token-exchange.sh

export REALM="myrealm"
export RHSSO_HOST="rhsso.apps.cluster.domain.com"
export RHBK_HOST="rhbk.apps.cluster.domain.com"
export CLIENT_ID="token-exchange-client"
export CLIENT_SECRET="<your-secret>"
export TEST_USER="testuser"
export TEST_PASSWORD="testpassword"

./tests/test-token-exchange.sh
```

---

## Gradual Migration Workflow

Once the infrastructure is in place, migrate clients one at a time:

1. **Export** the client configuration from RH-SSO
2. **Import** it into RHBK
3. **Update** the application's OIDC configuration to point to RHBK
4. **Deploy** a Token Exchange Proxy instance in front of the service
   (if other services call it with old tokens)
5. **Test** the flow end-to-end
6. **Remove** the client from RH-SSO once all consumers have migrated

Repeat until all clients are on RHBK, then decommission RH-SSO and the
Token Exchange Proxies.

---

## File Structure

```
.
├── README.md                              ← You are here
├── gateway/
│   ├── 00-namespace.yaml                  Namespace definition
│   ├── 01-tls-secret.yaml                 TLS certificate secrets (template)
│   ├── 02-configmap.yaml                  NGINX routing configuration
│   ├── 03-deployment.yaml                 NGINX Deployment
│   ├── 04-service.yaml                    ClusterIP Service
│   ├── 05-route-rhsso.yaml               OpenShift Route for rhsso.*
│   └── 06-route-rhbk.yaml                OpenShift Route for rhbk.*
├── token-exchange-proxy/
│   ├── app/
│   │   ├── proxy.py                       Python reverse proxy with token exchange
│   │   └── requirements.txt               Python dependencies
│   ├── Dockerfile                         Container image build
│   ├── 00-configmap.yaml                  Proxy configuration (two examples)
│   ├── 01-secret.yaml                     Client credentials
│   ├── 02-deployment.yaml                 Proxy Deployments (legacy + migrated)
│   ├── 03-service.yaml                    Proxy Services
│   └── 04-buildconfig.yaml                OpenShift BuildConfig + ImageStream
├── keycloak-config/
│   ├── SETUP.md                           Step-by-step Keycloak configuration
│   ├── rhsso-add-rhbk-idp.json           Partial realm import for RH-SSO
│   └── rhbk-add-rhsso-idp.json           Partial realm import for RHBK
├── network-policy/
│   └── network-policy.yaml                NetworkPolicy resources
└── tests/
    └── test-token-exchange.sh             End-to-end verification script
```

---

## Security Considerations

| Risk | Mitigation |
|------|-----------|
| CVE-2026-1486 (RHBK 26.4) — disabled IdP still accepted for JWT Auth Grant | Rotate signing keys instead of disabling IdP; monitor exchange logs |
| Token Exchange is tech-preview in RH-SSO 7.6 | Accepted risk for migration period; feature is functional |
| Proxy sees plaintext tokens | Deploy proxy in the same namespace / network segment; use mTLS if available |
| Token cache in proxy memory | Cache is keyed by token hash; tokens expire naturally; restart clears cache |

---

## References

- [Keycloak JWT Authorization Grant](https://www.keycloak.org/securing-apps/jwt-authorization-grant)
- [Keycloak Identity & Authorization Chaining Across Domains](https://www.keycloak.org/securing-apps/oauth-identity-authorization-chaining-across-domains)
- [RFC 7523 — JWT Profile for OAuth 2.0 Client Authentication and Authorization Grants](https://datatracker.ietf.org/doc/html/rfc7523)
- [Keycloak Token Exchange](https://www.keycloak.org/docs/latest/securing_apps/#_token-exchange)
