# SSO Migration Implementation Guide

## Gradual RH-SSO to RHBK Migration with Zero Application Code Changes

**Version:** 1.0  
**Date:** May 2026  
**Platform:** Red Hat OpenShift Container Platform

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Solution Architecture](#3-solution-architecture)
4. [Prerequisites](#4-prerequisites)
5. [Phase 1: Deploy RH-SSO 7.6.5](#5-phase-1-deploy-rh-sso-765)
6. [Phase 2: Deploy RHBK 26.4](#6-phase-2-deploy-rhbk-264)
7. [Phase 3: Configure Bidirectional Token Exchange](#7-phase-3-configure-bidirectional-token-exchange)
8. [Phase 4: Deploy Token Exchange Proxy](#8-phase-4-deploy-token-exchange-proxy)
9. [Phase 5: Network Policies](#9-phase-5-network-policies)
10. [Phase 6: Testing & Validation](#10-phase-6-testing--validation)
11. [Production Considerations](#11-production-considerations)
12. [Frequently Asked Questions](#12-frequently-asked-questions)
13. [Troubleshooting Guide](#13-troubleshooting-guide)
14. [Summary of Key Configuration](#14-summary-of-key-configuration)

---

## 1. Executive Summary

This document describes a solution for running **RH-SSO 7.6.5** (Keycloak 18.x) and **RHBK 26.4** (Keycloak 26.x) in parallel on OpenShift, enabling gradual migration of applications from one identity provider to the other **without any application code changes and without downtime**.

The solution uses:

- **Bidirectional Token Exchange** (RFC 8693) between both IdPs
- **IdP Gateway Proxies** — transparent reverse proxies deployed in front of each IdP that use a "try-first, exchange-on-failure" approach to automatically swap cross-domain tokens
- **Network Policies** to secure internal traffic

### What Was Proven

| Capability | Status |
|---|---|
| Direct authentication against RH-SSO | Working |
| Direct authentication against RHBK | Working |
| RHBK token → RH-SSO exchange (Direction B) | Working |
| RH-SSO token → RHBK exchange (Direction D) | Working |
| IdP proxy: RHBK token → RH-SSO via idp-proxy-rhsso | Working |
| IdP proxy: RH-SSO token → RHBK via idp-proxy-rhbk | Working |
| Chained double-hop: RH-SSO → RHBK → RH-SSO | Working |
| Chained double-hop: RHBK → RH-SSO → RHBK | Working |
| Full customer scenario (System B → migrated System A) | Working |
| All IdP traffic (admin, token, userinfo) through proxy | Working |
| Persistent across pod restarts (no manual TLS setup) | Working |

---

## 2. Problem Statement

A customer has two systems:

- **System A** — has its own client (`Client A`) registered in RH-SSO
- **System B** — does NOT have its own client; it uses `Client A` on RH-SSO to get tokens and call System A's API

When the SSO administrator wants to migrate to RHBK:

1. System A migrates to RHBK — users authenticating to System A now go through RHBK
2. System B still holds an RH-SSO token (from `Client A`)
3. System B calls System A with the RH-SSO token
4. System A (now on RHBK) rejects the token — **invalid token**

**Requirement:** Enable gradual migration where applications can be moved one-by-one to RHBK without breaking cross-system communication, and without requiring any code changes.

---

## 3. Solution Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      sso-gateway namespace                          │
│                                                                     │
│  ┌────────────────────────┐       ┌────────────────────────┐       │
│  │   idp-proxy-rhsso      │       │   idp-proxy-rhbk       │       │
│  │   (IdP Gateway Proxy)  │       │   (IdP Gateway Proxy)  │       │
│  │                        │       │                        │       │
│  │  All traffic to RH-SSO │       │  All traffic to RHBK   │       │
│  │  flows through here.   │       │  flows through here.   │       │
│  │  Try first → if 401 +  │       │  Try first → if 401 +  │       │
│  │  Bearer → exchange →   │       │  Bearer → exchange →   │       │
│  │  retry.                │       │  retry.                │       │
│  └────────────┬───────────┘       └────────────┬───────────┘       │
│               │                                │                    │
│  ┌────────────▼───────────┐       ┌────────────▼───────────┐       │
│  │   sso-migration-demo   │       │                        │       │
│  │   (Interactive demo)   │       │                        │       │
│  └────────────────────────┘       │                        │       │
└───────────────────────────────────┼────────────────────────┼───────┘
                                    │                        │
                ┌───────────────────┘                        └───────┐
                │                                                    │
┌───────────────▼─────────────┐         ┌────────────────────────────▼┐
│       rhsso namespace       │         │       rhbk namespace        │
│                             │  Token  │                             │
│  ┌───────────────────────┐  │ Exchange│  ┌───────────────────────┐  │
│  │    RH-SSO 7.6.5       │◄─┼─(RFC8693)─┼►│     RHBK 26.4         │  │
│  │    (Keycloak 18.x)    │  │         │  │    (Keycloak 26.x)    │  │
│  └──────────┬────────────┘  │         │  └──────────┬────────────┘  │
│  ┌──────────▼────────────┐  │         │  ┌──────────▼────────────┐  │
│  │     PostgreSQL        │  │         │  │     PostgreSQL        │  │
│  └───────────────────────┘  │         │  └───────────────────────┘  │
└─────────────────────────────┘         └─────────────────────────────┘
```

### Keycloak Clients

Each IdP has specific clients configured in the `myrealm` realm:

| Client | Where | Purpose |
|--------|-------|---------|
| `token-exchange-client` | **Both** RH-SSO and RHBK | The main confidential client used by the proxy and any application that needs to acquire or exchange tokens. Has **service accounts enabled** and **direct access grants** (password grant). An **audience mapper** is configured so tokens include `token-exchange-client` in the `aud` claim. Fine-grained permissions are set to allow this client to perform token exchanges. Shared secret: `token-exchange-secret-12345` (change in production). |
| `rhbk-broker` | RH-SSO only | OIDC broker client used by RH-SSO's `rhbk` Identity Provider. When RH-SSO needs to call RHBK's userinfo endpoint to validate a foreign token during exchange, it uses this client for the OIDC flow. |
| `rhsso-broker` | RHBK only | OIDC broker client used by RHBK's `rhsso` Identity Provider. The reverse of `rhbk-broker` — used when RHBK needs to call RH-SSO's userinfo endpoint. |

### Identity Providers (Bidirectional Trust)

| IdP Alias | Configured On | Points To | Purpose |
|-----------|---------------|-----------|---------|
| `rhbk` | RH-SSO | RHBK's OIDC endpoints | When RH-SSO receives a token exchange request with `subject_issuer=rhbk`, it uses this IdP configuration to locate RHBK's endpoints and validate the foreign token. |
| `rhsso` | RHBK | RH-SSO's OIDC endpoints | The reverse — when RHBK receives `subject_issuer=rhsso`, it uses this IdP to validate the RH-SSO token. |

### Gateway Namespace Components (`sso-gateway`)

| Deployment | Pods | Purpose |
|------------|------|---------|
| `idp-proxy-rhsso` | 2 | **IdP Gateway Proxy in front of RH-SSO.** All external traffic destined for RH-SSO flows through this proxy. It forwards requests to RH-SSO as-is; if RH-SSO returns 401/403 and the request has a Bearer token, the proxy exchanges it for an RH-SSO-native token and retries. Non-Bearer requests (login pages, token grants, admin console assets) pass through untouched. |
| `idp-proxy-rhbk` | 2 | **IdP Gateway Proxy in front of RHBK.** Same approach, reversed direction — exchanges RH-SSO tokens → RHBK tokens when RHBK rejects them. |
| `sso-migration-demo` | 1 | **Interactive demo web application** (Flask/Python). Provides a browser-based UI with live architecture diagrams, test scenarios with visual component flow diagrams, and command references. Auto-discovers IdP URLs from OCP Routes. |

### How the Proxy Works (IdP Gateway Mode)

The proxy uses a **"try-first, exchange-on-failure"** approach — it does not pre-inspect or decode the JWT. Instead:

1. Request arrives at the proxy (which sits in front of the IdP, not the application)
2. Proxy forwards the request **as-is** to the upstream IdP
3. If the IdP returns **success** → return the response to the caller (done)
4. If the IdP returns **401 or 403** AND the request had a Bearer token → **exchange the token** at the IdP's token endpoint, then **retry** the request with the new token
5. If the retry also fails → the token is genuinely invalid; return the error to the caller
6. Requests without a Bearer token (login pages, token grants, admin console assets, etc.) always pass through untouched

This eliminates the need for an `EXPECTED_ISSUER` configuration — the IdP itself decides whether a token is valid. The `PASSTHROUGH_PREFIXES` environment variable (default: `/admin,/js,/resources,/robots.txt`) allows skipping the exchange logic entirely for specific URL prefixes (e.g., static assets).

**Set-Cookie preservation:** The proxy uses `upstream_resp.raw.headers.items()` (urllib3's raw headers) instead of `upstream_resp.headers.items()` to preserve duplicate `Set-Cookie` headers that the `requests` library would otherwise merge.

### Traffic Flow — Where the Proxy Sits

The proxy sits **in front of the Identity Providers**, not in front of the backend applications. All external traffic to each IdP flows through its gateway proxy. This includes everything: admin console access, token acquisition, userinfo calls, and API calls that carry Bearer tokens.

#### Why in Front of the IdPs?

In many environments, the SSO administrator **cannot map which application uses which client or which IdP**. Placing a proxy in front of each individual application is impractical. By placing the proxy in front of each IdP instead, all traffic is covered automatically — no per-application configuration is needed.

#### Example: System A migrated to RHBK, System B still on RH-SSO

```
┌───────────┐                                                 ┌───────────┐
│  System B │  ① Auth request (user login / password grant)   │  System A │
│ (on RHSSO)│──────────────┐                          ┌──────│ (on RHBK) │
│           │              │                          │      │           │
│           │              ▼                          │      │           │
│           │     ┌─────────────────┐                 │      │           │
│           │     │ idp-proxy-rhsso │                 │      │           │
│           │     │  (in front of   │                 │      │           │
│           │     │   RH-SSO)       │                 │      │           │
│           │     └────────┬────────┘                 │      │           │
│           │              │                          │      │           │
│           │              ▼                          │      │           │
│           │     ┌─────────────────┐                 │      │           │
│           │     │    RH-SSO       │                 │      │           │
│           │     └─────────────────┘                 │      │           │
│           │                                         │      │           │
│           │  ② API call with RH-SSO token           │      │           │
│           │────────────────────────────────────────►│      │           │
│           │                                         │      │           │
│           │              System A validates the token at RHBK:         │
│           │                                         │      │           │
│           │                                ┌────────▼─────────┐       │
│           │                                │  idp-proxy-rhbk  │       │
│           │                                │  (in front of    │       │
│           │                                │   RHBK)          │       │
│           │                                └────────┬─────────┘       │
│           │                                         │                  │
│           │                            ③ Forward RH-SSO token to RHBK  │
│           │                            ④ RHBK returns 401              │
│           │                            ⑤ Proxy exchanges → RHBK token  │
│           │                            ⑥ Retry → RHBK returns 200      │
│           │                                         │                  │
│           │                                ┌────────▼─────────┐       │
│           │                                │      RHBK        │       │
│           │                                └──────────────────┘       │
│           │                                                           │
│           │◄──────────────── ⑦ Response ─────────────────────────────│
└───────────┘                                                 └───────────┘
```

1. **System B authenticates against RH-SSO** (through `idp-proxy-rhsso`). The proxy forwards the login/token request to RH-SSO as-is — no Bearer token involved, so no exchange logic triggers. System B gets an RH-SSO token.
2. **System B calls System A's API** with the RH-SSO token (direct call — the proxy is not in this path).
3. **System A validates the token against RHBK** (through `idp-proxy-rhbk`). System A sends a userinfo/introspection request to RHBK carrying the RH-SSO token.
4. **RHBK rejects the foreign RH-SSO token** with HTTP 401.
5. **The proxy detects the 401 + Bearer token**, exchanges the RH-SSO token for a RHBK token, and retries the request.
6. **RHBK accepts the exchanged token** and returns the userinfo/introspection response.
7. **System A gets the validated identity** and responds to System B. System B never knows a proxy was involved.

#### Key Point: All IdP Traffic Flows Through the Proxy

The OCP Routes for `rhsso.*` and `rhbk.*` point to the proxy services (`idp-proxy-rhsso` / `idp-proxy-rhbk`), not directly to the IdPs. This means:

- **Admin console access** goes through the proxy (passes through unchanged — no Bearer token)
- **Token acquisition** (login, password grant, client credentials) goes through the proxy (passes through unchanged)
- **Bearer-token-based calls** (userinfo, introspection, API calls) go through the proxy — and get exchanged if rejected
- The RHBK operator's built-in ingress must be **disabled** (`spec.ingress.enabled: false`) to prevent it from creating a competing Route that bypasses the proxy

#### Key Point: The Proxy is a Reverse Proxy, Not a Redirect

The proxy does **not** send an HTTP redirect (302) to the caller. It holds the caller's connection open, makes a second HTTP call to the upstream IdP (the `TARGET_URL`), and streams the IdP's response back to the caller. The caller is completely unaware that a proxy was involved — it looks like a direct call to the IdP.

#### `IDP_EXTERNAL_HOST` and Token Issuer

When the proxy exchanges a token, it calls the IdP's token endpoint via the internal cluster service URL (e.g., `rhbk-service.rhbk.svc.cluster.local:8443`). Without intervention, the exchanged token's `iss` claim would include the internal hostname and port, causing validation failures. The `IDP_EXTERNAL_HOST` environment variable is set as the `Host` header on exchange calls so that the IdP issues tokens with the correct external issuer URL (without `:8443`).

---

## 4. Prerequisites

For the customer's environment (where RH-SSO and RHBK are already installed):

- OpenShift Container Platform 4.x cluster
- RH-SSO Operator installed (RH-SSO 7.6.5)
- Keycloak Operator installed (RHBK 26.4)
- `oc` CLI with cluster-admin access
- Both RH-SSO and RHBK running with at least one realm

### Namespaces

| Namespace | Purpose |
|---|---|
| `rhsso` | RH-SSO deployment |
| `rhbk` | RHBK deployment |
| `sso-gateway` | Token exchange proxies, demo app |

---

## 5. Phase 1: Deploy RH-SSO 7.6.5

> **Note:** If RH-SSO is already deployed, skip to Section 5.3 (Feature Enablement).

### 5.1 Install the RH-SSO Operator

In the OpenShift Console:
1. Navigate to **Operators → OperatorHub**
2. Search for "Red Hat Single Sign-On"
3. Install into the `rhsso` namespace

### 5.2 Create the Keycloak Instance

```yaml
apiVersion: keycloak.org/v1alpha1
kind: Keycloak
metadata:
  name: rhsso
  namespace: rhsso
spec:
  instances: 1
  externalAccess:
    enabled: true
```

### 5.3 Enable Token Exchange (CRITICAL)

RH-SSO 7.6.5 requires two tech-preview features to be enabled:

| Feature | JVM Flag | Purpose |
|---------|----------|---------|
| `token_exchange` | `-Dkeycloak.profile.feature.token_exchange=enabled` | Adds the RFC 8693 Token Exchange grant type (`urn:ietf:params:oauth:grant-type:token-exchange`) to the token endpoint. Without it, RH-SSO rejects exchange requests with `unsupported_grant_type`. This is the foundational feature — without it, the entire solution cannot work. |
| `admin_fine_grained_authz` | `-Dkeycloak.profile.feature.admin_fine_grained_authz=enabled` | Adds a **Permissions** tab to Clients and Identity Providers in the admin console. Token exchange is a privileged operation — Keycloak requires explicit policies that define which clients are allowed to exchange tokens. Without this feature, there is no way to create those policies, and every exchange attempt fails with `"Client not allowed to exchange"`. |

Both are shipped disabled by default in RH-SSO 7.6.5 because Red Hat considers them tech-preview. Patch the Keycloak CR to enable them:

```bash
oc -n rhsso patch keycloak rhsso --type=merge -p '{
  "spec": {
    "keycloakDeploymentSpec": {
      "experimental": {
        "env": [
          {
            "name": "JAVA_OPTS_APPEND",
            "value": "-Dkeycloak.profile.feature.token_exchange=enabled -Dkeycloak.profile.feature.admin_fine_grained_authz=enabled"
          }
        ]
      }
    }
  }
}'
```

Verify the pod restarts with the features:
```bash
oc -n rhsso logs keycloak-0 | grep -i "token.exchange"
```

### 5.4 Create a Realm

```bash
# Get admin credentials
ADMIN_USER=$(oc -n rhsso get secret credential-rhsso -o jsonpath='{.data.ADMIN_USERNAME}' | base64 -d)
ADMIN_PASS=$(oc -n rhsso get secret credential-rhsso -o jsonpath='{.data.ADMIN_PASSWORD}' | base64 -d)
RHSSO_URL=$(oc -n rhsso get route keycloak -o jsonpath='{.spec.host}')

# Get admin token
ADMIN_TOKEN=$(curl -sk "https://${RHSSO_URL}/auth/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" -d "username=${ADMIN_USER}" -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create realm
curl -sk -X POST "https://${RHSSO_URL}/auth/admin/realms" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"realm": "myrealm", "enabled": true}'
```

### 5.5 Create Token Exchange Client

```bash
curl -sk -X POST "https://${RHSSO_URL}/auth/admin/realms/myrealm/clients" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "token-exchange-client",
    "enabled": true,
    "publicClient": false,
    "secret": "token-exchange-secret-12345",
    "directAccessGrantsEnabled": true,
    "serviceAccountsEnabled": true,
    "standardFlowEnabled": false,
    "protocol": "openid-connect"
  }'
```

### 5.6 Add Audience Mapper to the Client

**Required on BOTH RH-SSO and RHBK.** This mapper adds `"token-exchange-client"` to the `aud` (audience) claim of every access token the client issues.

**Why this is needed:** Consider a chained double-hop exchange (RH-SSO → RHBK → RH-SSO):

1. Start with an RH-SSO token (from a user login)
2. Exchange it at RHBK → RHBK issues a **new** token from its `token-exchange-client`
3. Take that RHBK-issued token and exchange it **back** at RH-SSO

At step 3, RH-SSO validates the incoming RHBK token and checks its `aud` claim. Without the mapper, the token has `"aud": ["account"]` (the default). RH-SSO rejects it because the audience doesn't include `token-exchange-client` — the client that's performing the exchange. With the mapper, the token has `"aud": ["account", "token-exchange-client"]`, and the validation passes.

For simple one-hop exchanges (direct A→B), the audience mapper isn't strictly required because the source token comes from a user login and follows a different validation path. But for consistency and to support chained scenarios, it must be on **both** IdPs.

```bash
# Get the client UUID
CLIENT_UUID=$(curl -sk "https://${RHSSO_URL}/auth/admin/realms/myrealm/clients?clientId=token-exchange-client" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Add audience mapper
curl -sk -X POST "https://${RHSSO_URL}/auth/admin/realms/myrealm/clients/${CLIENT_UUID}/protocol-mappers/models" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "token-exchange-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
      "included.client.audience": "token-exchange-client",
      "id.token.claim": "false",
      "access.token.claim": "true"
    }
  }'
```

### 5.7 Create Test User

```bash
curl -sk -X POST "https://${RHSSO_URL}/auth/admin/realms/myrealm/users" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "enabled": true,
    "emailVerified": true,
    "credentials": [{"type": "password", "value": "testpass", "temporary": false}]
  }'
```

---

## 6. Phase 2: Deploy RHBK 26.4

> **Note:** If RHBK is already deployed, skip to Section 6.3 (Feature Enablement).

### 6.1 Install the Keycloak Operator

In the OpenShift Console:
1. Navigate to **Operators → OperatorHub**
2. Search for "Keycloak" (by Red Hat)
3. Install into the `rhbk` namespace

### 6.2 Create the Keycloak Instance

```yaml
apiVersion: k8s.keycloak.org/v2alpha1
kind: Keycloak
metadata:
  name: rhbk
  namespace: rhbk
spec:
  instances: 1
  hostname:
    hostname: rhbk-rhbk.apps.<cluster-domain>
```

### 6.3 Enable Required Features (CRITICAL)

RHBK 26.4 (Quarkus-based) requires a specific combination of features enabled and disabled for token exchange to work correctly:

| Feature | Action | Purpose |
|---------|--------|---------|
| `preview` | **Enable** | A feature profile that unlocks all preview features in RHBK 26.x, including the Token Exchange grant type. Without it, the `urn:ietf:params:oauth:grant-type:token-exchange` grant is not recognized and you get `unsupported_grant_type`. |
| `admin-fine-grained-authz:v1` | **Enable** | Same concept as RH-SSO's `admin_fine_grained_authz` — adds the **Permissions** tab to Clients and Identity Providers so you can define policies controlling who can exchange tokens. The `:v1` version suffix is important: RHBK 26.x introduced versioned features, and `v1` is the stable implementation that works correctly with token exchange policies. Without it, every exchange fails with `"Token not authorized for token exchange"`. |
| `token-exchange-external-internal` | **Disable** | A newer, simplified token exchange flow in RHBK 26.x. When enabled, it **bypasses fine-grained authorization entirely** — it takes over the exchange flow and skips the IdP validation path we need (where RHBK calls RH-SSO's userinfo/introspection endpoint to validate the foreign token). It doesn't properly respect the `subject_issuer` parameter, and the fine-grained policies we set up get completely ignored. Disabling it forces RHBK to use the `v1` fine-grained authorization path, which correctly validates foreign tokens and checks permissions. |

Additionally, `startOptimized` must be set to `false`:

| Setting | Value | Purpose |
|---------|-------|---------|
| `startOptimized` | `false` | RHBK 26.x is built on Quarkus, which has two modes: **optimized** (features decided at build time, fast ~2-3s startup) and **non-optimized** (Quarkus build step runs at startup, slower ~15-30s). `admin-fine-grained-authz` is a **build-time feature** — the default RHBK image was built without it. With `startOptimized: true` and a stock image, the feature silently fails: the Permissions tab shows up but the authorization checks don't execute. Setting `false` forces a rebuild at startup so the feature is actually compiled in. For production, build a custom image with the feature baked in and use `startOptimized: true` (see [Section 11.2](#112-rhbk-custom-image)). |

```bash
oc -n rhbk patch keycloak rhbk --type=merge -p '{
  "spec": {
    "startOptimized": false,
    "features": {
      "enabled": ["preview", "admin-fine-grained-authz:v1"],
      "disabled": ["token-exchange-external-internal"]
    },
    "additionalOptions": [
      {"name": "log-level", "value": "INFO"}
    ]
  }
}'
```

### 6.4 Create a Matching Realm

```bash
ADMIN_USER="admin"
ADMIN_PASS=$(oc -n rhbk get secret rhbk-initial-admin -o jsonpath='{.data.password}' | base64 -d)
RHBK_URL=$(oc -n rhbk get route rhbk -o jsonpath='{.spec.host}')

ADMIN_TOKEN=$(curl -sk "https://${RHBK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" -d "username=${ADMIN_USER}" -d "password=${ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -X POST "https://${RHBK_URL}/admin/realms" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"realm": "myrealm", "enabled": true}'
```

### 6.5 Create Token Exchange Client

```bash
curl -sk -X POST "https://${RHBK_URL}/admin/realms/myrealm/clients" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "token-exchange-client",
    "enabled": true,
    "publicClient": false,
    "secret": "token-exchange-secret-12345",
    "directAccessGrantsEnabled": true,
    "serviceAccountsEnabled": true,
    "standardFlowEnabled": false,
    "protocol": "openid-connect"
  }'
```

### 6.6 Add Audience Mapper to RHBK Client

Same as Section 5.6, but on RHBK:

```bash
CLIENT_UUID=$(curl -sk "${RHBK_URL}/admin/realms/myrealm/clients?clientId=token-exchange-client" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

curl -sk -X POST "${RHBK_URL}/admin/realms/myrealm/clients/${CLIENT_UUID}/protocol-mappers/models" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "token-exchange-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
      "included.client.audience": "token-exchange-client",
      "id.token.claim": "false",
      "access.token.claim": "true"
    }
  }'
```

### 6.7 Create Test User

```bash
curl -sk -X POST "https://${RHBK_URL}/admin/realms/myrealm/users" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "enabled": true,
    "emailVerified": true,
    "credentials": [{"type": "password", "value": "testpass", "temporary": false}]
  }'
```

---

## 7. Phase 3: Configure Bidirectional Token Exchange

This is the most complex phase. Both IdPs need to trust each other.

### 7.1 Establish TLS Trust

Each IdP needs to trust the other's TLS certificate.

#### Extract RHBK's certificate and import into RH-SSO:

```bash
# Extract RHBK certificate
oc -n rhbk get secret rhbk-tls-secret -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/rhbk.crt

# Create ConfigMap in rhsso namespace
oc -n rhsso create configmap rhbk-ca-cert --from-file=rhbk.crt=/tmp/rhbk.crt

# Mount into RH-SSO (patch the Keycloak CR to mount the cert and add to X509_CA_BUNDLE)
# Also manually import into Java truststore (needed for outgoing HTTPS):
oc -n rhsso exec keycloak-0 -- keytool -import -trustcacerts \
  -alias rhbk-cert -file /etc/x509/custom/rhbk.crt \
  -keystore /opt/eap/keystores/truststore.jks \
  -storepass <truststore-password> -noprompt
```

> **Note:** The `keytool` import doesn't survive pod restarts. For production, bake the cert into a custom image or use an init container.

#### Extract RH-SSO's certificate and import into RHBK:

```bash
# Extract RH-SSO route certificate
RHSSO_HOST=$(oc -n rhsso get route keycloak -o jsonpath='{.spec.host}')
echo | openssl s_client -connect ${RHSSO_HOST}:443 -servername ${RHSSO_HOST} 2>/dev/null | \
  openssl x509 > /tmp/rhsso.crt

# Create ConfigMap in rhbk namespace
oc -n rhbk create configmap rhsso-ca-cert --from-file=rhsso.crt=/tmp/rhsso.crt

# Mount into RHBK via the Keycloak CR
oc -n rhbk patch keycloak rhbk --type=merge -p '{
  "spec": {
    "additionalOptions": [
      {"name": "truststore-paths", "value": "/opt/keycloak/certs/rhsso.crt"}
    ],
    "unsupported": {
      "podTemplate": {
        "spec": {
          "containers": [{
            "volumeMounts": [{
              "name": "rhsso-ca",
              "mountPath": "/opt/keycloak/certs"
            }]
          }],
          "volumes": [{
            "name": "rhsso-ca",
            "configMap": {"name": "rhsso-ca-cert"}
          }]
        }
      }
    }
  }
}'
```

### 7.2 Register RHBK as an Identity Provider in RH-SSO

```bash
RHSSO_URL="https://$(oc -n rhsso get route keycloak -o jsonpath='{.spec.host}')"
RHBK_URL="https://$(oc -n rhbk get route rhbk -o jsonpath='{.spec.host}')"

# Get RH-SSO admin token (same as Phase 1)
ADMIN_TOKEN=<get-admin-token>

curl -sk -X POST "${RHSSO_URL}/auth/admin/realms/myrealm/identity-provider/instances" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "rhbk",
    "providerId": "oidc",
    "enabled": true,
    "trustEmail": true,
    "config": {
      "clientId": "token-exchange-client",
      "clientSecret": "token-exchange-secret-12345",
      "authorizationUrl": "'${RHBK_URL}'/realms/myrealm/protocol/openid-connect/auth",
      "tokenUrl": "'${RHBK_URL}'/realms/myrealm/protocol/openid-connect/token",
      "userInfoUrl": "'${RHBK_URL}'/realms/myrealm/protocol/openid-connect/userinfo",
      "jwksUrl": "'${RHBK_URL}'/realms/myrealm/protocol/openid-connect/certs",
      "issuer": "'${RHBK_URL}'/realms/myrealm",
      "validateSignature": "true",
      "useJwksUrl": "true"
    }
  }'
```

### 7.3 Configure Token Exchange Permissions on RH-SSO

RH-SSO needs to explicitly allow the `token-exchange-client` to perform token exchanges:

```bash
# Get the client UUID and IdP resource
CLIENT_UUID=<token-exchange-client-uuid>

# Enable management permissions on the client
curl -sk -X PUT "${RHSSO_URL}/auth/admin/realms/myrealm/clients/${CLIENT_UUID}/management/permissions" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Create a "client" type policy
curl -sk -X POST "${RHSSO_URL}/auth/admin/realms/myrealm/clients-permissions/${CLIENT_UUID}/policies" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-token-exchange-client",
    "type": "client",
    "logic": "POSITIVE",
    "decisionStrategy": "UNANIMOUS",
    "clients": ["'${CLIENT_UUID}'"]
  }'

# Associate the policy with the client token-exchange permission
# AND with the rhbk IdP token-exchange permission
```

> **Tip:** This is easier to do through the RH-SSO admin console:
> 1. Go to **Clients → token-exchange-client → Permissions** (toggle ON)
> 2. Click the `token-exchange` scope → Create Policy → Client → select `token-exchange-client`
> 3. Go to **Identity Providers → rhbk → Permissions** (toggle ON)
> 4. Click `token-exchange` scope → use the same policy

### 7.4 Register RH-SSO as an Identity Provider in RHBK

```bash
RHBK_ADMIN_TOKEN=<get-rhbk-admin-token>

curl -sk -X POST "${RHBK_URL}/admin/realms/myrealm/identity-provider/instances" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "rhsso",
    "providerId": "oidc",
    "enabled": true,
    "trustEmail": true,
    "config": {
      "clientId": "token-exchange-client",
      "clientSecret": "token-exchange-secret-12345",
      "authorizationUrl": "'${RHSSO_URL}'/auth/realms/myrealm/protocol/openid-connect/auth",
      "tokenUrl": "'${RHSSO_URL}'/auth/realms/myrealm/protocol/openid-connect/token",
      "userInfoUrl": "'${RHSSO_URL}'/auth/realms/myrealm/protocol/openid-connect/userinfo",
      "jwksUrl": "'${RHSSO_URL}'/auth/realms/myrealm/protocol/openid-connect/certs",
      "issuer": "'${RHSSO_URL}'/auth/realms/myrealm",
      "introspectionUrl": "'${RHSSO_URL}'/auth/realms/myrealm/protocol/openid-connect/token/introspect",
      "validateSignature": "true",
      "useJwksUrl": "true"
    }
  }'
```

> **Important:** The `introspectionUrl` must be explicitly set — RHBK requires it for token exchange validation.

### 7.5 Configure Fine-Grained Authorization on RHBK (CRITICAL)

This is the most complex part. RHBK 26.4 uses fine-grained authorization to control who can perform token exchanges.

```bash
# Get token-exchange-client UUID on RHBK
CLIENT_UUID=$(curl -sk "${RHBK_URL}/admin/realms/myrealm/clients?clientId=token-exchange-client" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# 1. Enable management permissions on the client
curl -sk -X PUT "${RHBK_URL}/admin/realms/myrealm/clients/${CLIENT_UUID}/management/permissions" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# 2. Enable management permissions on the rhsso IdP
curl -sk -X PUT "${RHBK_URL}/admin/realms/myrealm/identity-provider/instances/rhsso/management/permissions" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# 3. Get the realm-management client UUID (RHBK's internal authz client)
REALM_MGMT_UUID=$(curl -sk "${RHBK_URL}/admin/realms/myrealm/clients?clientId=realm-management" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# 4. Create a "client" policy allowing token-exchange-client
curl -sk -X POST "${RHBK_URL}/admin/realms/myrealm/clients/${REALM_MGMT_UUID}/authz/resource-server/policy/client" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-token-exchange-client",
    "clients": ["'${CLIENT_UUID}'"],
    "logic": "POSITIVE"
  }'

# 5. Create a "user" policy for the service account
SA_USER_ID=$(curl -sk "${RHBK_URL}/admin/realms/myrealm/clients/${CLIENT_UUID}/service-account-user" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -sk -X POST "${RHBK_URL}/admin/realms/myrealm/clients/${REALM_MGMT_UUID}/authz/resource-server/policy/user" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-token-exchange-service-account",
    "users": ["'${SA_USER_ID}'"],
    "logic": "POSITIVE"
  }'

# 6. Link policies to the token-exchange scope permissions
#    (both client-level and IdP-level permissions)
```

> **Tip:** This is significantly easier through the RHBK admin console:
> 1. Go to **Clients → realm-management → Authorization → Policies**
> 2. Create a "Client" policy → select `token-exchange-client`
> 3. Create a "User" policy → select the service account user
> 4. Go to **Permissions** tab → find `token-exchange.permission.client.<uuid>` and `token-exchange.permission.idp.<alias>`
> 5. Add both policies to both permissions with `AFFIRMATIVE` decision strategy

### 7.6 Verify Token Exchange

Test both directions:

```bash
# Direction B: RHBK → RH-SSO
RHBK_TOKEN=$(curl -sk "${RHBK_URL}/realms/myrealm/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=token-exchange-secret-12345" \
  -d "username=testuser" -d "password=testpass" \
  -d "grant_type=password" -d "scope=openid" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk "${RHSSO_URL}/auth/realms/myrealm/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=token-exchange-secret-12345" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${RHBK_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhbk" -d "scope=openid" | python3 -m json.tool
# Should return access_token

# Direction D: RH-SSO → RHBK
RHSSO_TOKEN=$(curl -sk "${RHSSO_URL}/auth/realms/myrealm/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=token-exchange-secret-12345" \
  -d "username=testuser" -d "password=testpass" \
  -d "grant_type=password" -d "scope=openid" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk "${RHBK_URL}/realms/myrealm/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=token-exchange-secret-12345" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${RHSSO_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhsso" -d "scope=openid" | python3 -m json.tool
# Should return access_token
```

> **Important:** Always include `scope=openid` in token exchange requests. Without it, the exchanged token won't have the `openid` scope, and the other IdP's userinfo endpoint will reject it with `Missing openid scope` — causing chained exchanges to fail.

---

## 8. Phase 4: Deploy Token Exchange Proxy

### 8.1 Create the Namespace

```bash
oc create namespace sso-gateway
```

### 8.2 Build the Proxy Image

```bash
# Apply BuildConfig
oc apply -f token-exchange-proxy/01-buildconfig.yaml

# Build from source
oc -n sso-gateway start-build token-exchange-proxy \
  --from-dir=./token-exchange-proxy --follow
```

### 8.3 Deploy Proxy Instances

Deploy one proxy instance **in front of each IdP**. The `TARGET_URL` points to the IdP itself (via the internal cluster service), not to a backend application.

**idp-proxy-rhsso** (in front of RH-SSO — exchanges RHBK tokens → RH-SSO tokens):
```yaml
data:
  TARGET_URL: "https://keycloak.rhsso.svc.cluster.local:8443"
  TOKEN_ENDPOINT: "https://keycloak.rhsso.svc.cluster.local:8443/auth/realms/<realm>/protocol/openid-connect/token"
  IDP_EXTERNAL_HOST: "<rhsso-route-hostname>"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhbk"
  VERIFY_UPSTREAM_TLS: "false"
  RETRY_STATUS_CODES: "401,403"
  REQUEST_TIMEOUT: "30"
```

**idp-proxy-rhbk** (in front of RHBK — exchanges RH-SSO tokens → RHBK tokens):
```yaml
data:
  TARGET_URL: "https://rhbk-service.rhbk.svc.cluster.local:8443"
  TOKEN_ENDPOINT: "https://rhbk-service.rhbk.svc.cluster.local:8443/realms/<realm>/protocol/openid-connect/token"
  IDP_EXTERNAL_HOST: "<rhbk-route-hostname>"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhsso"
  VERIFY_UPSTREAM_TLS: "false"
  RETRY_STATUS_CODES: "401,403"
  REQUEST_TIMEOUT: "30"
```

> **Note:** `PASSTHROUGH_PREFIXES` (default: `/admin,/js,/resources,/robots.txt`) lets you skip exchange logic for specific paths. The default covers admin console static assets.

> **Important:** The RHBK operator's built-in ingress must be disabled (`spec.ingress.enabled: false` in the Keycloak CR) to prevent it from creating a Route that bypasses the proxy.

### 8.4 Create Routes and Deploy

The OCP Routes for each IdP hostname must point to the proxy services, not directly to the IdPs:

```bash
# Apply gateway routes (these take over the IdP hostnames)
oc apply -f token-exchange-proxy/06-route-rhsso.yaml
oc apply -f token-exchange-proxy/07-route-rhbk.yaml

# Apply proxy configuration and deploy
oc apply -f token-exchange-proxy/02-configmap.yaml
oc apply -f token-exchange-proxy/03-secret.yaml
oc apply -f token-exchange-proxy/04-deployment.yaml
oc apply -f token-exchange-proxy/05-service.yaml
```

> **Important:** The routes use `edge` TLS termination. The proxy communicates with the upstream IdP over HTTPS via the internal service URL.

---

## 9. Phase 5: Network Policies

### 9.1 Label Namespaces

```bash
oc label namespace rhsso   sso-migration/idp=true
oc label namespace rhbk    sso-migration/idp=true
oc label namespace sso-gateway sso-migration/access=true
# For each app namespace that needs proxy access:
oc label namespace <app-ns> sso-migration/access=true
```

### 9.2 Apply Policies

```bash
oc apply -f network-policy/network-policy.yaml
```

This creates:
- **allow-router-to-gateway** — ingress from OCP router
- **allow-apps-to-token-proxy** — ingress from labeled app namespaces
- **allow-intra-namespace** — ingress between pods in `sso-gateway`
- **default-deny-ingress** — blocks all other ingress

---

## 10. Phase 6: Testing & Validation

### 10.1 Interactive Demo Application

An interactive web application is provided at:

```
https://sso-migration-demo-sso-gateway.apps.<cluster-domain>
```

It provides:
- **Architecture tab** — Visual diagram of all components and their relationships
- **Test Scenarios tab** — 9 interactive test scenarios from basic to advanced, with live execution against the real cluster
- **Command Reference tab** — Copy-paste curl commands to test everything manually

### 10.2 Manual Testing

#### Test 1: Direct Authentication
```bash
# RH-SSO
curl -sk "https://<rhsso-route>/auth/realms/myrealm/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=token-exchange-secret-12345" \
  -d "username=testuser" -d "password=testpass" -d "grant_type=password"
```

#### Test 2: Token Exchange
```bash
# Get a source token, then exchange it (see Section 7.6)
```

#### Test 3: Proxy Transparent Exchange
```bash
# Send an RH-SSO token through the RHBK proxy (it should get exchanged):
curl -sk "https://<rhbk-route>/realms/myrealm/protocol/openid-connect/userinfo" \
  -H "Authorization: Bearer <rhsso-token>"

# Check proxy logs:
oc -n sso-gateway logs deploy/idp-proxy-rhbk --tail=10
# Should see: "IdP returned 401 for token with iss='...'. Attempting token exchange."
#             "Token exchanged successfully — retrying request."

# The reverse direction (RHBK token through the RH-SSO proxy):
curl -sk "https://<rhsso-route>/auth/realms/myrealm/protocol/openid-connect/userinfo" \
  -H "Authorization: Bearer <rhbk-token>"

oc -n sso-gateway logs deploy/idp-proxy-rhsso --tail=10
```

### 10.3 Admin Console Verification

- **RH-SSO Admin Console**: `https://<rhsso-route>/auth/admin/`
  - Check: Realms → myrealm → Identity Providers → rhbk exists
  - Check: Clients → token-exchange-client → Permissions enabled
- **RHBK Admin Console**: `https://<rhbk-route>/admin/`
  - Check: Realms → myrealm → Identity Providers → rhsso exists
  - Check: Clients → realm-management → Authorization → Policies exist

---

## 11. Production Considerations

### 11.1 TLS Certificates

| POC | Production |
|---|---|
| Self-signed certificates | CA-signed certificates (internal PKI or public CA) |
| `VERIFY_UPSTREAM_TLS: false` | `VERIFY_UPSTREAM_TLS: true` |
| Manual `keytool` import (lost on restart) | Bake certs into custom image or use init containers |
| `edge` TLS termination on proxy routes | Consider `reencrypt` with the IdP's CA cert for end-to-end encryption |

### 11.2 RHBK Custom Image

Build a custom RHBK image with `admin-fine-grained-authz` pre-built:

```dockerfile
FROM registry.redhat.io/rhbk/keycloak-rhel9:26.4
ENV KC_FEATURES="preview,admin-fine-grained-authz:v1"
RUN /opt/keycloak/bin/kc.sh build
```

Build this in CI/CD (NOT on the cluster), push to your private registry, then update the Keycloak CR:

```yaml
spec:
  image: your-registry.example.com/rhbk-custom:26.4
  startOptimized: true
```

### 11.3 High Availability

| Component | POC | Production |
|---|---|---|
| RH-SSO | 1 replica | 2+ replicas with Infinispan clustering |
| RHBK | 1 replica | 2+ replicas with JGroups |
| PostgreSQL | Single pod | HA (Crunchy PGO, Patroni, or managed RDS) |
| IdP Gateway Proxy | 2 replicas | 2+ replicas with PodDisruptionBudget |

### 11.4 Secrets Management

| POC | Production |
|---|---|
| Hardcoded `token-exchange-secret-12345` | Strong, unique secrets per environment |
| `stringData` in YAML (plaintext) | External Secrets Operator, Vault, or Sealed Secrets |
| Same secret on both IdPs | Separate secrets, rotated periodically |

### 11.5 Token Cache

| POC | Production |
|---|---|
| In-memory dict per pod | Redis cluster (shared across proxy replicas) |
| No cache size limit | LRU eviction with max size |
| Cache lost on restart | Persistent cache with TTL |

### 11.6 Observability

**Add to production:**
- Prometheus metrics endpoint (`/metrics`) on the proxy
- ServiceMonitor for scraping
- Grafana dashboard (exchange rate, latency, cache hit ratio, errors)
- Alerting on exchange failure rate > threshold
- Structured JSON logging for ELK/Splunk/Loki
- OpenTelemetry tracing header propagation

### 11.7 Egress Network Policies

The POC omits egress policies (they blocked DNS resolution). For production:

```yaml
# CoreDNS on OCP uses port 5353
egress:
  - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: openshift-dns
    ports:
      - port: 5353
        protocol: UDP
      - port: 5353
        protocol: TCP
```

### 11.8 Multiple Realms

Each realm pair needs:
- Its own `token-exchange-client` on both IdPs
- IdP registration on both sides
- Fine-grained authorization policies
- A proxy instance per protected service

### 11.9 RHBK Operator Ingress

When deploying `idp-proxy-rhbk` in front of RHBK, the RHBK Keycloak Operator may create its own Route/Ingress for the RHBK hostname. This **competes with** the proxy Route and causes some traffic to bypass the proxy entirely.

**Fix:** Disable the operator's built-in ingress in the Keycloak CR:

```yaml
apiVersion: k8s.keycloak.org/v2alpha1
kind: Keycloak
metadata:
  name: rhbk
  namespace: rhbk
spec:
  ingress:
    enabled: false
  # ... rest of spec
```

This ensures all external traffic to RHBK goes through `idp-proxy-rhbk`.

### 11.10 Practical Notes for Real-World Deployment

#### The proxy uses a dedicated exchange client, not your application's client

In the POC, we use a generic `token-exchange-client` on both IdPs. In a real deployment, your application (e.g., System A) has its own client (`client_A`) registered on RH-SSO. When System A migrates to RHBK, `client_A` is recreated on RHBK.

The proxy does **not** use `client_A` to perform the exchange. It uses its own separate `token-exchange-client` (a service-account client with exchange permissions). This means:

- `client_A`'s configuration does not need to change at all
- The proxy is a separate infrastructure concern — deploy one in front of each IdP
- All application clients (`client_A`, `client_B`, etc.) benefit from the same proxy automatically

#### The proxy does not pre-inspect tokens — it relies on the IdP's response

Unlike the earlier "issuer-check" design, the proxy does **not** decode or inspect the JWT before forwarding. It sends the request to the IdP as-is and only intervenes when the IdP returns 401/403. This means:

- The proxy is fully **client-agnostic** — it doesn't know or care about `iss`, `azp`, or `aud`
- Tokens from **any** client on the foreign IdP will be exchanged when the IdP rejects them
- The exchanged token is issued by the target IdP's `token-exchange-client`, not by the original application client
- Native tokens (already from the correct IdP) pass through without any overhead — the IdP accepts them on the first try

#### System B does not need any changes — ever

In the customer scenario (System B → RH-SSO → System A), System B continues to:

1. Authenticate against `client_A` on RH-SSO (same as before — traffic goes through `idp-proxy-rhsso` transparently)
2. Call System A's API with the RH-SSO token (same endpoint as before)

System B is **completely unaware** that System A migrated to RHBK. The IdP proxy in front of RHBK handles the token exchange when System A validates the token. When System B is eventually ready to migrate too, you simply point it to RHBK — no breaking changes at any point.

#### Migration is reversible

If something goes wrong after migrating System A to RHBK, you can move it back to RH-SSO. The bidirectional trust and the IdP proxies on both sides mean the exchange works in both directions automatically. No proxy reconfiguration is needed.

#### One proxy per IdP, not one per application

Deploy one proxy instance in front of each IdP (two total: `idp-proxy-rhsso` and `idp-proxy-rhbk`). All applications using that IdP benefit automatically. This is simpler than the per-application approach and eliminates the need to identify which apps receive cross-domain tokens.

#### The exchange adds latency — but only for cross-domain tokens

When a token is already native to the target IdP, the IdP accepts it on the first try — the proxy is a simple pass-through with minimal overhead. The token exchange HTTP call only happens when the IdP rejects a foreign token (HTTP 401/403) and typically adds 20–100ms for the exchange + retry. The proxy includes an in-memory cache to avoid re-exchanging the same token.

#### TLS trust is persistent across pod restarts

The RHBK certificate is stored in a Kubernetes ConfigMap (`rhbk-ca-cert`) and mounted into the RH-SSO pod via the Keycloak CR's `spec.keycloakDeploymentSpec.experimental.volumes`. The `X509_CA_BUNDLE` environment variable tells the JBoss EAP startup scripts to import these certificates into the Java truststore **on every pod boot**. No manual `keytool` commands are needed after the initial setup.

Similarly, the RH-SSO certificate is stored in a ConfigMap (`rhsso-ca-cert`) and configured in RHBK's `truststore-paths`. Both survive pod restarts.

---

## 12. Frequently Asked Questions

### Security

#### "The proxy doesn't verify the JWT signature — isn't that a security risk?"

No. The proxy does not decode or verify the JWT at all before forwarding — it sends requests to the IdP as-is and only intervenes when the IdP rejects them. This is intentional:

- The proxy is **not a security boundary**. It does not make access control decisions.
- The **upstream IdP** validates the token cryptographically. If the token is valid, the IdP accepts it directly. If invalid, the IdP rejects it and the proxy attempts an exchange.
- During exchange, the **target IdP** validates the foreign token via JWKS, calls the source IdP's userinfo/introspection endpoint, and checks fine-grained authorization policies.
- If an attacker sends a forged JWT, the upstream IdP rejects it (401), the exchange also fails (forged tokens can't pass IdP-level validation), and the caller gets the original error.
- Adding signature verification at the proxy would require distributing and rotating signing keys to every proxy instance — significant operational complexity for no real security gain, since the IdP already does this.

#### "What if the `token-exchange-client` secret is compromised?"

An attacker with the client secret could exchange any valid token from one IdP to the other. This is the same risk as any OAuth client secret compromise. Mitigations:

- Rotate secrets periodically
- Use HashiCorp Vault, External Secrets Operator, or Sealed Secrets instead of plaintext Kubernetes secrets
- Restrict network access to the proxy pods via Network Policies so only authorized namespaces can reach them
- Monitor exchange logs for unusual patterns (high volume, unknown source IPs)

#### "Is `token_exchange` supported by Red Hat? It's marked as tech-preview."

In RH-SSO 7.6.5, token exchange is tech-preview. In RHBK 26.x, the `preview` profile is more mature but not GA for all features. For Red Hat support:

- Open a support case and discuss the use case — many customers run tech-preview features in production with Red Hat's awareness
- The alternative (no proxy, no exchange) requires **all applications to migrate simultaneously** — this is usually a bigger risk than using a well-tested tech-preview feature with a clear rollback path

#### "The proxy has `VERIFY_UPSTREAM_TLS: false` — is that safe?"

No, this is POC-only. In production:

1. Mount the IdP CA certificate into the proxy pod as a volume
2. Set `VERIFY_UPSTREAM_TLS: "true"`
3. See Section 11.1 for the full TLS production setup

### Token Behavior

#### "The exchanged token comes from `token-exchange-client`, not the original client. Will our apps accept it?"

After the exchange, the token's `azp` (authorized party) is `token-exchange-client`, not the original application client (e.g., `client_A`). If the backend application checks `azp` or `aud` strictly, it may reject the exchanged token. Solutions:

- **Option A:** Add an audience mapper on `token-exchange-client` to include the application's client ID (e.g., `client_A`) in the `aud` claim of exchanged tokens
- **Option B:** Configure the backend application to also accept tokens from `token-exchange-client`
- **In practice:** Most applications only validate the issuer and signature, not `azp`. Check with the application team to confirm.

#### "Does the proxy handle refresh tokens?"

No. The proxy only intercepts access tokens in the `Authorization: Bearer` header. Refresh tokens are a client-side concern:

- System B refreshes its token directly against its own IdP (RH-SSO) as it always has
- When System B makes an API call with the new access token, the proxy exchanges it transparently
- The proxy's in-memory cache means that if System B uses the same access token for multiple requests, only the first request triggers an exchange

#### "Do we need to migrate user accounts between RH-SSO and RHBK?"

**Not for the token exchange to work.** When a token is exchanged for the first time, the target IdP automatically creates a "linked" user identity. The exchange mechanism handles this transparently.

However, for the **application migration itself** (when System A moves to RHBK), the application's end users need to exist in RHBK so they can log in directly. That's a separate migration topic (user federation, database import, LDAP sync, etc.) and is outside the scope of this proxy solution.

### Failure Scenarios

#### "What if the proxy pod goes down?"

- **All traffic to that IdP** is affected since the proxy sits in front of the IdP. This includes both native and cross-domain token flows.
- Kubernetes restarts the proxy pod typically within seconds.
- **For HA:** The POC already runs 2 replicas per proxy. For production, use a `PodDisruptionBudget` to ensure at least one pod is always available during rollouts.

#### "What if RH-SSO or RHBK goes down?"

- If the **source IdP** (the one that issued the token) goes down: the exchange may still work temporarily because the target IdP caches JWKS signing keys. Once the cache expires and the target IdP can't reach the source's JWKS endpoint, exchanges fail.
- If the **target IdP** (the one performing the exchange) goes down: the exchange fails immediately and the proxy returns HTTP 502 to the caller.
- This is the same impact as any IdP outage — applications that depend on it are affected regardless of the proxy.

### Performance

#### "How much latency does the proxy add?"

- **Native tokens (no exchange needed):** The IdP accepts the token on the first try. The proxy adds only the overhead of proxying the request (typically a few milliseconds for the in-cluster hop).
- **Cross-domain tokens (exchange needed, first time):** The first attempt returns 401/403, then the proxy exchanges the token (~20–100ms) and retries. Total overhead is the exchange call + one extra IdP request.
- **Cross-domain tokens (cached):** The proxy maintains an in-memory cache keyed by a SHA-256 hash of the source token. On a cache hit, the proxy substitutes the token and sends a single request — no exchange call needed.
- **Non-Bearer requests (login, token grants):** Pass through with no extra logic — just the proxying overhead.
- **Note:** The cache is per-pod. If you run 3 proxy replicas, each has its own cache. A token hitting replica 1 gets cached there; if the next request hits replica 2 (via load balancing), it exchanges again. For most workloads this is fine. For high-throughput services, consider sticky sessions or a shared Redis cache (see Section 11.5).

### Migration Strategy

#### "What's the migration sequence for multiple applications?"

With the IdP Gateway Mode, the proxies are deployed **once** in front of each IdP — they don't need to be deployed per-application. Then migrate one application at a time:

1. Ensure `idp-proxy-rhsso` and `idp-proxy-rhbk` are deployed (one-time setup)
2. Migrate the application to validate against RHBK
3. Verify cross-domain calls work (the proxy handles exchange automatically)
4. Move to the next application

No per-application proxy configuration is needed. The IdP proxies handle all traffic for all applications.

#### "When can we remove the proxy?"

- Once **all applications** are on RHBK, decommission RH-SSO, remove `idp-proxy-rhsso`, and point the RHBK route directly to RHBK (re-enable the operator's built-in ingress).
- `idp-proxy-rhbk` can also be removed at that point — there are no more cross-domain tokens to exchange.
- The proxies are transitional components — they exist only during the migration period.

#### "Can we roll back if something goes wrong?"

Yes. The bidirectional trust and the IdP proxies on both sides mean the exchange works in both directions automatically. If you migrate System A to RHBK and something breaks:

1. Move System A back to RH-SSO
2. No proxy reconfiguration is needed — `idp-proxy-rhsso` already handles RHBK-to-RH-SSO exchanges

No data is lost and no permanent changes were made to the applications.

#### "What about applications outside the OpenShift cluster?"

Since the proxy sits in front of the IdP (not the application), external applications are handled automatically. Any application — inside or outside the cluster — that authenticates or validates tokens against the IdP goes through the proxy via the IdP's external hostname. No application-side configuration changes are needed.

---

## 13. Troubleshooting Guide

### Error: "Client not allowed to exchange"

**Source:** RH-SSO  
**Cause:** Missing client policy for token exchange  
**Fix:**
1. Go to RH-SSO Admin Console → Clients → token-exchange-client → Permissions → Enable
2. Click `token-exchange` scope
3. Create a "Client" policy selecting `token-exchange-client`
4. Also add the policy to the IdP's `token-exchange` permission

### Error: "Token not authorized for token exchange"

**Source:** RHBK  
**Cause:** Fine-grained authorization not properly configured  
**Fix:**
1. Verify `admin-fine-grained-authz:v1` is enabled
2. Verify `startOptimized: false` (or custom image)
3. Verify `token-exchange-external-internal` is DISABLED
4. Check realm-management → Authorization → Policies and Permissions

### Error: "invalid_token" with "audience" in description

**Source:** RHBK  
**Cause:** Source token's `aud` claim doesn't include `token-exchange-client`  
**Fix:** Add an `oidc-audience-mapper` to the source IdP's `token-exchange-client`

### Error: "user info call failure" / TLS errors

**Source:** Either IdP  
**Cause:** Missing TLS trust between IdPs  
**Fix:**
- For RH-SSO: Import the other IdP's cert into Java truststore
- For RHBK: Set `truststore-paths` in the Keycloak CR

### Error: "unsupported_grant_type"

**Source:** RHBK  
**Cause:** Token exchange feature not enabled  
**Fix:** Enable `preview` in the Keycloak CR features

### Error: "Introspection endpoint not configured for IDP"

**Source:** RHBK  
**Cause:** The `introspectionUrl` is not set on the IdP configuration  
**Fix:** Add `introspectionUrl` to the Identity Provider config via REST API

### Error: "invalid_token" / "Missing openid scope" on chained exchange

**Source:** Either IdP (specifically the userinfo endpoint)  
**Cause:** The exchanged token doesn't include the `openid` scope. When the target IdP tries to validate the token by calling the source IdP's userinfo endpoint, it's rejected.  
**Fix:** Always include `scope=openid` in token exchange requests.

### Error: "invalid_token" due to issuer port mismatch

**Source:** Either IdP  
**Cause:** When RHBK is accessed via the internal service URL (port 8443), the token's `iss` claim includes the port number (e.g., `https://host:8443/realms/myrealm`), but the IdP configuration on the other side uses the external URL without the port (e.g., `https://host/realms/myrealm`). This mismatch causes token validation to fail.  
**Fix:** The proxy sets the `IDP_EXTERNAL_HOST` environment variable as the `Host` header on exchange calls, ensuring that exchanged tokens have the correct external issuer URL (without `:8443`). Verify that `IDP_EXTERNAL_HOST` is set correctly in the proxy ConfigMap. Alternatively, configure `hostname-strict: false` and `hostname-port: -1` in the Keycloak CR.

### Proxy: "NameResolutionError"

**Source:** Token Exchange Proxy  
**Cause:** DNS resolution blocked by egress network policies  
**Fix:** Either remove egress policies or add DNS port 5353 to the egress rules

---

## 14. Summary of Key Configuration

### Feature Flags Reference

Every feature flag is required. Removing any one breaks a different part of the exchange chain.

| Feature | IdP | Enabled/Disabled | Why It's Needed |
|---------|-----|-----------------|-----------------|
| `token_exchange` | RH-SSO | **Enabled** | Adds the RFC 8693 token exchange grant type to the token endpoint |
| `admin_fine_grained_authz` | RH-SSO | **Enabled** | Adds the Permissions tab so you can authorize which clients may exchange tokens |
| `preview` | RHBK | **Enabled** | Unlocks preview features including the token exchange grant type |
| `admin-fine-grained-authz:v1` | RHBK | **Enabled** | Adds the Permissions tab (`:v1` is the version that works correctly with exchange) |
| `token-exchange-external-internal` | RHBK | **Disabled** | Bypasses fine-grained auth and breaks the IdP validation flow we need |
| `startOptimized` | RHBK | **`false`** | Required so `admin-fine-grained-authz` is compiled in at startup (build-time feature) |

### RH-SSO Configuration Checklist

- [ ] Features enabled: `token_exchange`, `admin_fine_grained_authz` (via `JAVA_OPTS_APPEND`)
- [ ] Realm created: `myrealm`
- [ ] Client created: `token-exchange-client` (confidential, service accounts enabled)
- [ ] Audience mapper added to `token-exchange-client`
- [ ] RHBK registered as IdP (alias: `rhbk`)
- [ ] Management permissions enabled on `token-exchange-client`
- [ ] Management permissions enabled on `rhbk` IdP
- [ ] Client policy created and linked to `token-exchange` scope permissions
- [ ] RHBK certificate trusted (imported into Java truststore)
- [ ] Test user created: `testuser`

### RHBK Configuration Checklist

- [ ] Features enabled: `preview`, `admin-fine-grained-authz:v1`
- [ ] Feature disabled: `token-exchange-external-internal`
- [ ] `startOptimized: false` (or custom image)
- [ ] Realm created: `myrealm`
- [ ] Client created: `token-exchange-client` (confidential, service accounts enabled)
- [ ] RH-SSO registered as IdP (alias: `rhsso`) with `introspectionUrl`
- [ ] Management permissions enabled on `token-exchange-client`
- [ ] Management permissions enabled on `rhsso` IdP
- [ ] Client policy + User policy created in `realm-management` authorization
- [ ] Policies linked to both client and IdP `token-exchange` scope permissions
- [ ] RH-SSO certificate trusted (via `truststore-paths`)
- [ ] Audience mapper added to `token-exchange-client`
- [ ] Test user created: `testuser`

### Token Exchange Request Requirements

- Always include `scope=openid` in token exchange requests
- Always include `subject_token_type=urn:ietf:params:oauth:token-type:access_token`

### Proxy Configuration (IdP Gateway Mode)

| Setting | `idp-proxy-rhsso` (in front of RH-SSO) | `idp-proxy-rhbk` (in front of RHBK) |
|---|---|---|
| `TARGET_URL` | RH-SSO internal service URL | RHBK internal service URL |
| `TOKEN_ENDPOINT` | RH-SSO internal token endpoint | RHBK internal token endpoint |
| `IDP_EXTERNAL_HOST` | RH-SSO external route hostname | RHBK external route hostname |
| `IDP_ALIAS` | `rhbk` | `rhsso` |
| `GRANT_TYPE` | `token-exchange` | `token-exchange` |
| `RETRY_STATUS_CODES` | `401,403` (default) | `401,403` (default) |
| `REQUEST_TIMEOUT` | `30` (default) | `30` (default) |
| `PASSTHROUGH_PREFIXES` | `/admin,/js,/resources,/robots.txt` (default) | `/admin,/js,/resources,/robots.txt` (default) |

---

### Pre-Built Container Images

The following images are available on Quay.io for easier deployment:

| Image | Description |
|-------|-------------|
| `quay.io/dbirenfe/sso-migration-demo:latest` | Interactive demo web application with 9 test scenarios |
| `quay.io/dbirenfe/sso-token-exchange-proxy:latest` | Token Exchange Proxy (Python/Flask) |

To deploy the demo app using the pre-built image, replace the `image` field in the Deployment manifest with the Quay URL instead of the internal OCP registry reference.

---

*This document was generated from a working POC deployment. All commands and configurations have been tested and verified on OpenShift Container Platform with RH-SSO 7.6.5 and RHBK 26.4.*
