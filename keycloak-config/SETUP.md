# Keycloak / RH-SSO Configuration Guide

Step-by-step instructions for establishing bidirectional trust between
RH-SSO 7.6.5 and RHBK 26.4.

---

## Prerequisites

| Item | Detail |
|------|--------|
| RH-SSO version | 7.6.5 (Keycloak 18.x) on OpenShift (e.g., namespace `rhsso`) |
| RHBK version | 26.4 on OpenShift (e.g., namespace `rhbk`) |
| Cluster | Both RH-SSO and RHBK run on the **same** OCP cluster |
| Network | Both reachable via the API Gateway hostnames and via in-cluster Services |
| Admin access | Realm admin on both instances |

---

## Part 1 — Configure RH-SSO to Trust RHBK

This allows RH-SSO to exchange tokens that were issued by RHBK.

### 1.1 Enable Token Exchange (Tech-Preview)

RH-SSO 7.6 ships Token Exchange as a **tech-preview** feature. Enable it
on **every RH-SSO node** before proceeding.

**Option A — JVM argument (standalone.sh / standalone-ha.sh):**
```bash
-Dkeycloak.profile.feature.token_exchange=enabled
```

**Option B — standalone.xml:**
```xml
<subsystem xmlns="urn:jboss:domain:keycloak-server:1.1">
    ...
    <spi name="preview">
        <provider name="token-exchange" enabled="true"/>
    </spi>
</subsystem>
```

Restart all RH-SSO nodes after the change.

### 1.2 Add RHBK as an Identity Provider

1. Open the **RH-SSO Admin Console**
2. Select your realm
3. Navigate to **Identity Providers** → **Add provider** → **OpenID Connect v1.0**
4. Fill in:

| Field | Value |
|-------|-------|
| Alias | `rhbk` |
| Display Name | `RHBK 26.4 (New Cluster)` |
| Authorization URL | `https://rhbk.apps.cluster.domain.com/realms/<REALM>/protocol/openid-connect/auth` |
| Token URL | `https://rhbk.apps.cluster.domain.com/realms/<REALM>/protocol/openid-connect/token` |
| Client ID | A client you created on RHBK for this broker relationship |
| Client Secret | The corresponding secret |
| Validate Signatures | `ON` |
| Use JWKS URL | `ON` |
| JWKS URL | `https://rhbk.apps.cluster.domain.com/realms/<REALM>/protocol/openid-connect/certs` |

5. Save

> **URL Note:** RHBK 26+ uses `/realms/...` (no `/auth` prefix). RH-SSO 7.6 uses `/auth/realms/...`.

### 1.3 Create the Token Exchange Client

1. Go to **Clients** → **Create**
2. Client ID: `token-exchange-client`
3. Settings:
   - Access Type: `confidential`
   - Service Accounts Enabled: `ON`
   - Standard Flow Enabled: `OFF`
   - Direct Access Grants: `OFF`
4. Save, go to the **Credentials** tab, and note the secret
5. Go to the **Permissions** tab:
   - Toggle **Permissions Enabled** to `ON`
   - Click on the `token-exchange` permission
   - Add a **Client Policy** that allows exchanges with the `rhbk` identity provider

### 1.4 Verify

```bash
# Exchange an RHBK token for an RH-SSO token
curl -X POST \
  "https://rhsso.apps.cluster.domain.com/auth/realms/<REALM>/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=<RHBK_ACCESS_TOKEN>" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhbk" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<SECRET>"
```

Expected: HTTP 200 with a new `access_token` issued by RH-SSO.

---

## Part 2 — Configure RHBK to Trust RH-SSO

This allows RHBK to exchange tokens that were issued by RH-SSO.
Uses the **JWT Authorization Grant** (RFC 7523), natively supported in RHBK 26.4.

### 2.1 Add RH-SSO as an Identity Provider

1. Open the **RHBK Admin Console**
2. Select your realm
3. Navigate to **Identity Providers** → **Add provider** → **OpenID Connect v1.0**
4. Fill in:

| Field | Value |
|-------|-------|
| Alias | `rhsso` |
| Display Name | `RH-SSO 7.6.5 (Legacy Cluster)` |
| Authorization URL | `https://rhsso.apps.cluster.domain.com/auth/realms/<REALM>/protocol/openid-connect/auth` |
| Token URL | `https://rhsso.apps.cluster.domain.com/auth/realms/<REALM>/protocol/openid-connect/token` |
| Client ID | A client you created on RH-SSO for this broker relationship |
| Client Secret | The corresponding secret |
| Validate Signatures | `ON` |
| Use JWKS URL | `ON` |
| JWKS URL | `https://rhsso.apps.cluster.domain.com/auth/realms/<REALM>/protocol/openid-connect/certs` |

5. Save

### 2.2 Create the Token Exchange Client with JWT Authorization Grant

1. Go to **Clients** → **Create client**
2. Client ID: `token-exchange-client`
3. Client authentication: `ON` (confidential)
4. Under **Capability config**:
   - Enable **JWT Authorization Grant**
5. Save, then go back to the client settings
6. In **Allowed Identity Providers for JWT Authorization Grant**, select `rhsso`
7. Go to the **Credentials** tab and note the secret

### 2.3 Verify

```bash
# Exchange an RH-SSO token for an RHBK token
curl -X POST \
  "https://rhbk.apps.cluster.domain.com/realms/<REALM>/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  -d "assertion=<RHSSO_ACCESS_TOKEN>" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<SECRET>"
```

Expected: HTTP 200 with a new `access_token` issued by RHBK.

---

## Part 3 — Broker Clients (on each side)

Each IdP configuration references a "broker client" on the other cluster.
This client is what the IdP uses to validate tokens.

### On RHBK — Create a broker client for RH-SSO

1. **Clients** → **Create client**
2. Client ID: e.g. `rhsso-broker`
3. Client authentication: `ON`
4. Valid Redirect URIs: `https://rhsso.apps.cluster.domain.com/auth/realms/<REALM>/broker/rhbk/endpoint/*`
5. Save. Use this client's ID/secret in the RH-SSO IdP configuration (step 1.2).

### On RH-SSO — Create a broker client for RHBK

1. **Clients** → **Create**
2. Client ID: e.g. `rhbk-broker`
3. Access Type: `confidential`
4. Valid Redirect URIs: `https://rhbk.apps.cluster.domain.com/realms/<REALM>/broker/rhsso/endpoint/*`
5. Save. Use this client's ID/secret in the RHBK IdP configuration (step 2.1).

---

## Security Notes

### CVE-2026-1486 (RHBK 26.4)

There is an active flaw in RHBK 26.4 regarding the `jwt-authorization-grant` flow:
the server does **not** verify whether an Identity Provider is enabled before issuing
tokens. If you disable the RH-SSO IdP in RHBK (e.g., during a security incident),
RHBK will **still** accept valid JWT assertions signed by RH-SSO.

**Mitigation:**
- Implement strict RSA key rotation policies
- Monitor token exchange logs for anomalies
- Plan to patch RHBK when a fix is released
- If a compromise is suspected, rotate the signing keys on the compromised cluster
  rather than just disabling the IdP

### Token Exchange (RH-SSO 7.6 Tech-Preview)

Token Exchange is a tech-preview feature in RH-SSO 7.6. It is functional but not
officially supported by Red Hat for production use. Evaluate your organization's
risk tolerance. The alternative is to upgrade all dependent services simultaneously
(which is what this architecture aims to avoid).
