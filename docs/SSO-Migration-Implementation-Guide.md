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
12. [Troubleshooting Guide](#12-troubleshooting-guide)
13. [Summary of Key Configuration](#13-summary-of-key-configuration)

---

## 1. Executive Summary

This document describes a solution for running **RH-SSO 7.6.5** (Keycloak 18.x) and **RHBK 26.4** (Keycloak 26.x) in parallel on OpenShift, enabling gradual migration of applications from one identity provider to the other **without any application code changes and without downtime**.

The solution uses:

- **Bidirectional Token Exchange** (RFC 8693) between both IdPs
- **Transparent Reverse Proxy** that intercepts cross-domain tokens and swaps them automatically
- **Network Policies** to secure internal traffic

### What Was Proven

| Capability | Status |
|---|---|
| Direct authentication against RH-SSO | Working |
| Direct authentication against RHBK | Working |
| RHBK token → RH-SSO exchange (Direction B) | Working |
| RH-SSO token → RHBK exchange (Direction D) | Working |
| Transparent proxy: RHBK token → Legacy app | Working |
| Transparent proxy: RH-SSO token → Migrated app | Working |
| Chained double-hop: RH-SSO → RHBK → RH-SSO | Working |
| Chained double-hop: RHBK → RH-SSO → RHBK | Working |
| Full customer scenario (System B → migrated System A) | Working |
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
┌─────────────────────────────┐         ┌─────────────────────────────┐
│       rhsso namespace       │         │       rhbk namespace        │
│                             │         │                             │
│  ┌───────────────────────┐  │  Token  │  ┌───────────────────────┐  │
│  │    RH-SSO 7.6.5       │◄─┼─Exchange─┼─►│     RHBK 26.4         │  │
│  │    (Keycloak 18.x)    │  │ (RFC8693)│  │    (Keycloak 26.x)    │  │
│  └──────────┬────────────┘  │         │  └──────────┬────────────┘  │
│             │               │         │             │               │
│  ┌──────────▼────────────┐  │         │  ┌──────────▼────────────┐  │
│  │     PostgreSQL        │  │         │  │     PostgreSQL        │  │
│  └───────────────────────┘  │         │  └───────────────────────┘  │
└─────────────────────────────┘         └─────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                      sso-gateway namespace                          │
│                                                                     │
│  ┌──────────────────────┐       ┌──────────────────────┐           │
│  │   Legacy Proxy        │       │   Migrated Proxy      │           │
│  │ (RHBK tok→RH-SSO tok)│       │ (RH-SSO tok→RHBK tok) │           │
│  └──────────┬───────────┘       └──────────┬───────────┘           │
│             │                              │                        │
│  ┌──────────▼───────────┐       ┌──────────▼───────────┐           │
│  │   Legacy App          │       │   Migrated App        │           │
│  │  (validates RH-SSO)   │       │  (validates RHBK)     │           │
│  └──────────────────────┘       └──────────────────────┘           │
└─────────────────────────────────────────────────────────────────────┘
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
| `token-proxy-legacy` | 1 | **Token Exchange Proxy for legacy apps** that still validate against RH-SSO. Sits in front of `echo-legacy`. When it receives a request with a RHBK token (issuer mismatch), it transparently exchanges it for an RH-SSO token at RH-SSO's token endpoint. If the token is already from RH-SSO, it passes through unchanged. |
| `token-proxy-migrated` | 1 | **Token Exchange Proxy for migrated apps** that now validate against RHBK. Sits in front of `echo-migrated`. Exchanges RH-SSO tokens → RHBK tokens when needed. |
| `echo-legacy` | 1 | **Simulated legacy backend** (httpd). Represents a real application that validates tokens against RH-SSO. In a real deployment, this would be the customer's actual application. |
| `echo-migrated` | 1 | **Simulated migrated backend** (httpd). Represents a real application that has been migrated to validate against RHBK. |
| `sso-migration-demo` | 1 | **Interactive demo web application** (Flask/Python). Provides a browser-based UI with live architecture diagrams, 9 test scenarios with visual component flow diagrams, and command references. Auto-discovers IdP URLs from OCP Routes. |

### How the Proxy Works

1. Request arrives with a Bearer token
2. Proxy decodes the JWT (no cryptographic verification — just reads the `iss` claim)
3. If `iss` matches the expected issuer → **pass-through** (no exchange)
4. If `iss` doesn't match → **token exchange** at the target IdP's token endpoint
5. Replace the `Authorization` header with the new token
6. Forward the request to the backend application

### Traffic Flow — Where the Proxy Sits

The proxy sits **in front of the backend application**, not in front of the identity providers. The calling system always talks directly to its own IdP to acquire a token — the proxy is never in that path.

#### Example: System A migrated to RHBK, System B still on RH-SSO

```
                        ┌──────────────┐
                        │   RH-SSO     │
                        │   (IdP)      │
                        └──────┬───────┘
                               │
                    ① Direct   │  Token response
                    auth call  │  (RH-SSO token)
                               │
┌───────────┐                  │         ┌─────────┐        ┌───────────┐
│  System B │──────────────────┘         │ Migrated │───────►│ System A  │
│ (on RHSSO)│                            │  Proxy   │        │ (backend) │
│           │──── ② API call ───────────►│          │        │ validates │
│           │     (with RH-SSO token)    │ exchanges│        │ RHBK      │
│           │                            │ to RHBK  │        │ tokens    │
│           │◄─── ④ Response ────────────│ token    │◄── ③ ──│           │
└───────────┘                            └──────────┘        └───────────┘
```

1. **System B authenticates directly against RH-SSO** — gets an RH-SSO token. The proxy is not involved.
2. **System B calls System A's API** with the RH-SSO token. Networking is configured so this request hits the proxy (via a Kubernetes Service, Route, or ExternalName).
3. **The proxy sees the RH-SSO token**, detects the issuer mismatch (`iss` ≠ RHBK), exchanges it for a RHBK token, and forwards the request to System A.
4. **System A responds** to the proxy, which passes the response back to System B unchanged. System B never knows the proxy exists.

#### Reverse Direction: System C on RHBK, System D still on RH-SSO

```
                        ┌──────────────┐
                        │    RHBK      │
                        │   (IdP)      │
                        └──────┬───────┘
                               │
                    ① Direct   │  Token response
                    auth call  │  (RHBK token)
                               │
┌───────────┐                  │         ┌─────────┐        ┌───────────┐
│  System C │──────────────────┘         │  Legacy  │───────►│ System D  │
│ (on RHBK) │                            │  Proxy   │        │ (backend) │
│           │──── ② API call ───────────►│          │        │ validates │
│           │     (with RHBK token)      │ exchanges│        │ RH-SSO   │
│           │                            │ to RHSSO │        │ tokens    │
│           │◄─── ④ Response ────────────│ token    │◄── ③ ──│           │
└───────────┘                            └──────────┘        └───────────┘
```

The pattern is identical — only the direction is reversed. The **legacy proxy** exchanges RHBK tokens into RH-SSO tokens for backends that haven't migrated yet.

#### Key Point: The Proxy is a Reverse Proxy, Not a Redirect

The proxy does **not** send an HTTP redirect (302) to the caller. It holds the caller's connection open, makes a second HTTP call to the real backend (the `TARGET_URL`), and streams the backend's response back to the caller. The caller is completely unaware that a proxy was involved — it looks like a direct call to the backend.

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

**Required on BOTH RH-SSO and RHBK.** Tokens must include `token-exchange-client` in the `aud` claim so the other IdP can validate them during exchange. Without this, chained (double-hop) exchanges fail with `invalid_token`.

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
oc apply -f token-exchange-proxy/04-buildconfig.yaml

# Build from source
oc -n sso-gateway start-build token-exchange-proxy \
  --from-dir=./token-exchange-proxy --follow
```

### 8.3 Deploy Proxy Instances

For each application that may receive cross-domain tokens, deploy a proxy instance with the appropriate configuration:

**Legacy Proxy** (protects apps that validate against RH-SSO):
```yaml
data:
  TARGET_URL: "http://<legacy-app-service>.<namespace>.svc.cluster.local:<port>"
  EXPECTED_ISSUER: "https://<rhsso-route>/auth/realms/<realm>"
  TOKEN_ENDPOINT: "https://keycloak.rhsso.svc.cluster.local:8443/auth/realms/<realm>/protocol/openid-connect/token"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhbk"
  VERIFY_UPSTREAM_TLS: "false"
```

**Migrated Proxy** (protects apps that validate against RHBK):
```yaml
data:
  TARGET_URL: "http://<migrated-app-service>.<namespace>.svc.cluster.local:<port>"
  EXPECTED_ISSUER: "https://<rhbk-route>/realms/<realm>"
  TOKEN_ENDPOINT: "https://rhbk-service.rhbk.svc.cluster.local:8443/realms/<realm>/protocol/openid-connect/token"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhsso"
  VERIFY_UPSTREAM_TLS: "false"
```

### 8.4 Apply Secrets and Deploy

```bash
oc apply -f token-exchange-proxy/00-configmap.yaml
oc apply -f token-exchange-proxy/01-secret.yaml
oc apply -f token-exchange-proxy/02-deployment.yaml
oc apply -f token-exchange-proxy/03-service.yaml
```

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
# From inside the cluster:
oc -n sso-gateway exec deploy/token-proxy-migrated -- curl -s http://localhost:8080/ \
  -H "Authorization: Bearer <rhsso-token>"
# Check proxy logs:
oc -n sso-gateway logs deploy/token-proxy-migrated --tail=5
# Should see: "Issuer mismatch... Exchanging token" + "Token exchanged successfully"
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
| Token Proxy | 1 replica | 2+ replicas with PodDisruptionBudget |

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

### 11.9 Deployment as Sidecar

Instead of a standalone proxy deployment, consider running the proxy as a **sidecar container** alongside each application pod. This eliminates network hops and ties the proxy lifecycle to the application.

### 11.10 Practical Notes for Real-World Deployment

#### The proxy uses a dedicated exchange client, not your application's client

In the POC, we use a generic `token-exchange-client` on both IdPs. In a real deployment, your application (e.g., System A) has its own client (`client_A`) registered on RH-SSO. When System A migrates to RHBK, `client_A` is recreated on RHBK.

The proxy does **not** use `client_A` to perform the exchange. It uses its own separate `token-exchange-client` (a service-account client with exchange permissions). This means:

- `client_A`'s configuration does not need to change at all
- The proxy is a separate concern — deploy one in front of any service that might receive cross-domain tokens
- You can have multiple application clients (`client_A`, `client_B`, etc.) all protected by the same proxy exchange mechanism

#### The proxy only looks at the `iss` claim — it is client-agnostic

The proxy decides whether to exchange by checking the **issuer** (`iss`) in the JWT. It does not inspect or care about the `azp` (authorized party), `aud` (audience), or which client issued the token. This means:

- Tokens from **any** client on the foreign IdP will be exchanged
- The exchanged token is issued by the target IdP's `token-exchange-client`, not by the original application client
- The backend application must accept tokens from `token-exchange-client` (or you configure audience mappers to include the original client in the exchanged token's `aud`)

#### System B does not need any changes — ever

In the customer scenario (System B → RH-SSO → System A), System B continues to:

1. Authenticate against `client_A` on RH-SSO (same as before)
2. Call System A's API with the RH-SSO token (same endpoint as before)

System B is **completely unaware** that System A migrated to RHBK. The proxy, sitting in front of System A, handles everything. When System B is eventually ready to migrate too, you simply point it to RHBK and remove the proxy — no breaking changes at any point.

#### Migration is reversible

If something goes wrong after migrating System A to RHBK, you can move it back to RH-SSO. The bidirectional trust means the proxy works in both directions. Simply switch the proxy's `EXPECTED_ISSUER` and `TOKEN_ENDPOINT` to reverse the exchange direction.

#### One proxy per protected service, not one global proxy

Deploy a proxy instance in front of **each** service that might receive cross-domain tokens, not as a single centralized gateway. This keeps the blast radius small, allows per-service configuration, and can be deployed as a sidecar container (see Section 11.9).

#### The exchange adds latency — but only for cross-domain tokens

When a token's issuer matches the expected issuer (no exchange needed), the proxy is a simple pass-through with sub-millisecond overhead. The token exchange HTTP call only happens for cross-domain tokens and typically adds 20–100ms. The proxy includes an in-memory cache to avoid re-exchanging the same token.

#### TLS trust is persistent across pod restarts

The RHBK certificate is stored in a Kubernetes ConfigMap (`rhbk-ca-cert`) and mounted into the RH-SSO pod via the Keycloak CR's `spec.keycloakDeploymentSpec.experimental.volumes`. The `X509_CA_BUNDLE` environment variable tells the JBoss EAP startup scripts to import these certificates into the Java truststore **on every pod boot**. No manual `keytool` commands are needed after the initial setup.

Similarly, the RH-SSO certificate is stored in a ConfigMap (`rhsso-ca-cert`) and configured in RHBK's `truststore-paths`. Both survive pod restarts.

---

## 12. Troubleshooting Guide

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
**Fix:** Use the external URL for all token exchange operations (not the internal service URL), or configure `hostname-strict: false` and `hostname-port: -1` in the Keycloak CR. The token exchange proxy is not affected because it uses the internal URLs for its own token exchange calls, which always go through the correct endpoint.

### Proxy: "NameResolutionError"

**Source:** Token Exchange Proxy  
**Cause:** DNS resolution blocked by egress network policies  
**Fix:** Either remove egress policies or add DNS port 5353 to the egress rules

---

## 13. Summary of Key Configuration

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

### Proxy Configuration Per Service

| Setting | Legacy App (validates RH-SSO) | Migrated App (validates RHBK) |
|---|---|---|
| `EXPECTED_ISSUER` | RH-SSO issuer URL | RHBK issuer URL |
| `TOKEN_ENDPOINT` | RH-SSO internal token endpoint | RHBK internal token endpoint |
| `IDP_ALIAS` | `rhbk` | `rhsso` |
| `GRANT_TYPE` | `token-exchange` | `token-exchange` |

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
