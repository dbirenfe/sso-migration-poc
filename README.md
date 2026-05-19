# SSO Migration — Parallel RH-SSO / RHBK with Zero Downtime

Gradual migration from **RH-SSO 7.6.5** to **RHBK 26.4** with no application
code changes.  **IdP Gateway Proxies** sit in front of each identity provider
and use a "try-first, exchange-on-failure" approach to transparently swap
cross-domain tokens — every application always receives tokens from its own
IdP, without any per-application configuration.

---

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │              DNS / OCP Routes         │
                        │  rhsso.apps.cluster.domain.com ──┐   │
                        │  rhbk.apps.cluster.domain.com  ──┤   │
                        └──────────────────────────────────┼───┘
                                                           │
                                                           ▼
              ┌─────────────────── OpenShift Cluster ───────────────────┐
              │                                                        │
              │  ┌──────────────────────────────────────┐              │
              │  │      sso-gateway namespace           │              │
              │  │                                      │              │
              │  │  ┌────────────────┐  ┌─────────────────┐           │
              │  │  │ idp-proxy-rhsso│  │ idp-proxy-rhbk  │           │
              │  │  │ (IdP Gateway)  │  │ (IdP Gateway)   │           │
              │  │  │ route: rhsso.* │  │ route: rhbk.*   │           │
              │  │  └────────┬───────┘  └────────┬────────┘           │
              │  └───────────┼───────────────────┼────────────────────┘│
              │              │                   │                     │
              │              ▼                   ▼                     │
              │  ┌────────────────┐  ┌────────────────┐               │
              │  │ RH-SSO 7.6.5  │  │ RHBK 26.4      │               │
              │  │ ns: rhsso     │  │ ns: rhbk       │               │
              │  │               │◄─── trust ───►│               │    │
              │  │ IdP: rhbk     │  │ IdP: rhsso     │               │
              │  │ Token Exchange│  │ Token Exchange  │               │
              │  └────────────────┘  └────────────────┘               │
              └────────────────────────────────────────────────────────┘


         How cross-domain tokens are handled:

        ┌───────────┐                ┌─────────────────┐          ┌────────────┐
        │ Any caller │  Bearer token │ idp-proxy-rhbk  │  native  │   RHBK     │
        │ (app, CLI, │ ────────────► │ (in front of    │  token   │            │
        │  browser)  │               │  RHBK)          │ ───────► │            │
        └───────────┘               │                  │          └────────────┘
                                    │ 1. Forward as-is │
                                    │ 2. If 401+Bearer │
                                    │    → exchange    │
                                    │    → retry       │
                                    └──────────────────┘
```

### Components

| # | Component | Purpose | Location |
|---|-----------|---------|----------|
| 1 | **IdP Gateway Proxies** | `idp-proxy-rhsso` and `idp-proxy-rhbk` — transparent reverse proxies in front of each IdP that swap cross-domain tokens on failure | `token-exchange-proxy/` |
| 2 | **Proxy Routes** | OCP Routes that direct IdP hostnames to the proxy services (edge TLS) | `token-exchange-proxy/` |
| 3 | **Keycloak Config** | IdP trust + exchange clients on both RH-SSO and RHBK | `keycloak-config/` |
| 4 | **Network Policies** | Lock down namespace traffic | `network-policy/` |

### How "Zero Code Changes" Works

The original problem: when System A migrates from RH-SSO to RHBK, System B
still holds RH-SSO tokens that System A no longer accepts.

The solution deploys **IdP Gateway Proxies** in front of each Identity Provider
(`idp-proxy-rhsso` in front of RH-SSO, `idp-proxy-rhbk` in front of RHBK).
All external traffic to each IdP flows through its proxy. The proxy uses a
"try-first, exchange-on-failure" approach:

- Forward every request to the IdP **as-is**
- If the IdP returns **401/403** AND the request has a Bearer token →
  **exchange the token** and **retry** the request
- If the retry also fails → the token is genuinely invalid
- Non-Bearer requests (login pages, token grants, admin console) pass through untouched

No per-application proxy setup is needed. The applications never see foreign
tokens. No code changes required.

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
grep -rn '<.*>' token-exchange-proxy/ keycloak-config/
```

### Step 1 — Deploy the Gateway Namespace and Routes

```bash
# Create the namespace
oc apply -f token-exchange-proxy/00-namespace.yaml

# Create the OpenShift Routes (these point IdP hostnames to the proxy services)
oc apply -f token-exchange-proxy/06-route-rhsso.yaml
oc apply -f token-exchange-proxy/07-route-rhbk.yaml

# Verify
oc -n sso-gateway get routes
```

> **Important:** Disable the RHBK operator's built-in ingress to prevent a competing Route:
> `oc -n rhbk patch keycloak rhbk --type=merge -p '{"spec":{"ingress":{"enabled":false}}}'`

### Step 2 — Update DNS

Point both hostnames to the OpenShift Router's external IP/CNAME:

```
rhsso.apps.cluster.domain.com  →  <OpenShift Router IP or CNAME>
rhbk.apps.cluster.domain.com   →  <OpenShift Router IP or CNAME>
```

Verify routing (after deploying the proxies in Step 4):
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

### Step 4 — Build and deploy the IdP Gateway Proxies

```bash
# Create the BuildConfig and ImageStream
oc apply -f token-exchange-proxy/01-buildconfig.yaml

# Build the image from local source
oc -n sso-gateway start-build token-exchange-proxy \
  --from-dir=./token-exchange-proxy --follow

# Edit the ConfigMaps and Secrets with real values
# (see token-exchange-proxy/02-configmap.yaml and 03-secret.yaml)
# Key settings: TARGET_URL, TOKEN_ENDPOINT, IDP_EXTERNAL_HOST
oc apply -f token-exchange-proxy/02-configmap.yaml
oc apply -f token-exchange-proxy/03-secret.yaml

# Deploy (creates idp-proxy-rhsso and idp-proxy-rhbk)
oc apply -f token-exchange-proxy/04-deployment.yaml
oc apply -f token-exchange-proxy/05-service.yaml

# Verify
oc -n sso-gateway get pods -l app.kubernetes.io/component=token-exchange-proxy
```

### Step 5 — Verify IdP access through the proxies

With IdP Gateway Mode, no per-application wiring is needed. The OCP Routes
already direct all IdP traffic through the proxies. Verify:

```bash
# RH-SSO admin console (through idp-proxy-rhsso)
curl -sk https://rhsso.apps.cluster.domain.com/auth/realms/master | head -1

# RHBK admin console (through idp-proxy-rhbk)
curl -sk https://rhbk.apps.cluster.domain.com/realms/master | head -1

# Test cross-domain exchange: send an RH-SSO token to RHBK's userinfo
curl -sk https://rhbk.apps.cluster.domain.com/realms/myrealm/protocol/openid-connect/userinfo \
  -H "Authorization: Bearer <rhsso-token>"
# Check idp-proxy-rhbk logs for exchange activity
```

> All applications that authenticate or validate tokens against the IdPs
> automatically go through the proxies. No service rewiring needed.

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

Once the IdP Gateway Proxies are deployed (one-time setup), migrate clients
one at a time:

1. **Export** the client configuration from RH-SSO
2. **Import** it into RHBK
3. **Update** the application's OIDC configuration to point to RHBK
4. **Test** the flow end-to-end — cross-domain tokens are handled automatically
   by the IdP proxies, no per-application proxy setup needed
5. **Remove** the client from RH-SSO once all consumers have migrated

Repeat until all clients are on RHBK, then decommission RH-SSO and remove
the IdP Gateway Proxies.

---

## File Structure

```
.
├── README.md                              ← You are here
├── token-exchange-proxy/    # All proxy files including routes and namespace
│   ├── 00-namespace.yaml                  Namespace definition
│   ├── 06-route-rhsso.yaml               OCP Route: rhsso.* → idp-proxy-rhsso (edge TLS)
│   └── 07-route-rhbk.yaml                OCP Route: rhbk.*  → idp-proxy-rhbk  (edge TLS)
├── token-exchange-proxy/
│   ├── app/
│   │   ├── proxy.py                       Python reverse proxy — IdP Gateway Mode
│   │   └── requirements.txt               Python dependencies
│   ├── Dockerfile                         Container image build
│   ├── 02-configmap.yaml                  Proxy configuration (idp-proxy-rhsso + idp-proxy-rhbk)
│   ├── 03-secret.yaml                     Client credentials (per proxy instance)
│   ├── 04-deployment.yaml                 Proxy Deployments (idp-proxy-rhsso + idp-proxy-rhbk)
│   ├── 05-service.yaml                    Proxy Services
│   └── 01-buildconfig.yaml                OpenShift BuildConfig + ImageStream
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
