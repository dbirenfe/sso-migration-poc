# On-Site Deployment Checklist

A concise, step-by-step guide for deploying the SSO Migration Token Exchange solution on a customer's OpenShift cluster. Designed to be followed in order during an on-site meeting.

**Total estimated time: ~75 minutes**

---

## Before the Meeting (Prepare Offline)

### Bring the container images

```bash
# On a connected machine:
podman pull quay.io/dbirenfe/sso-token-exchange-proxy:latest
podman save -o sso-token-exchange-proxy.tar quay.io/dbirenfe/sso-token-exchange-proxy:latest

podman pull quay.io/dbirenfe/sso-migration-demo:latest
podman save -o sso-migration-demo.tar quay.io/dbirenfe/sso-migration-demo:latest
```

### Bring the repo

```bash
git clone https://github.com/dbirenfe/sso-migration-poc.git
```

Copy the repo folder + the two `.tar` files to a USB drive.

### If the cluster is air-gapped, also bring Python packages

```bash
pip download -d ./offline-packages flask==3.1.* requests==2.32.* gunicorn==23.*
```

---

## On-Site — Step 0: Gather Info (10 min)

Collect these from the customer before touching anything:

| Info needed | Command to find it | Used in |
|---|---|---|
| RH-SSO namespace | `oc get ns \| grep -i sso` | All steps |
| RHBK namespace | `oc get ns \| grep -i rhbk\|keycloak` | All steps |
| RH-SSO service name + port | `oc get svc -n <rhsso-ns>` | ConfigMap `TARGET_URL` |
| RHBK service name + port | `oc get svc -n <rhbk-ns>` | ConfigMap `TARGET_URL` |
| RH-SSO external route hostname | `oc get route -n <rhsso-ns>` | ConfigMap `IDP_EXTERNAL_HOST`, Routes |
| RHBK external route hostname | `oc get route -n <rhbk-ns>` | ConfigMap `IDP_EXTERNAL_HOST`, Routes |
| Realm name | Ask the customer | ConfigMap `TOKEN_ENDPOINT` path |
| RH-SSO admin credentials | `oc get secret credential-rhsso -n <rhsso-ns> -o jsonpath='{.data}'` | Keycloak config |
| RHBK admin credentials | `oc get secret <keycloak-initial-admin> -n <rhbk-ns> -o jsonpath='{.data}'` | Keycloak config |
| Customer's app + how to test it | Ask the customer | Final validation |
| Customer's container registry URL | Ask the customer | Image push |

---

## Phase 1: Keycloak Configuration (~30 min)

### Step 1.1 — Enable features on RH-SSO (5 min)

Edit the RH-SSO Keycloak CR:

```bash
oc edit keycloak <name> -n <rhsso-ns>
```

Add under `spec.keycloakDeploymentSpec.experimental.env`:

```yaml
- name: JAVA_OPTS_APPEND
  value: "-Dkeycloak.profile.feature.token_exchange=enabled -Dkeycloak.profile.feature.admin_fine_grained_authz=enabled"
```

Wait for pod restart. Verify:

```bash
oc get pods -n <rhsso-ns> -w
```

### Step 1.2 — Enable features on RHBK (5 min)

Edit the RHBK Keycloak CR:

```bash
oc edit keycloak.k8s.keycloak.org <name> -n <rhbk-ns>
```

Add/set:

```yaml
spec:
  startOptimized: false
  features:
    enabled:
      - preview
      - admin-fine-grained-authz:v1
    disabled:
      - token-exchange-external-internal
```

Wait for pod restart (~30s with `startOptimized: false`).

### Step 1.3 — Exchange TLS certificates (10 min)

```bash
# Extract RHBK cert → create ConfigMap in RH-SSO namespace
openssl s_client -connect <RHBK_ROUTE>:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > rhbk.crt
oc create configmap rhbk-ca-cert --from-file=rhbk.crt -n <rhsso-ns>

# Extract RH-SSO cert → create ConfigMap in RHBK namespace
openssl s_client -connect <RHSSO_ROUTE>:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > rhsso.crt
oc create configmap rhsso-ca-cert --from-file=rhsso.crt -n <rhbk-ns>
```

Mount in the CRs:

**RH-SSO** — add to `keycloakDeploymentSpec.experimental`:

```yaml
env:
  - name: X509_CA_BUNDLE
    value: "/var/run/secrets/kubernetes.io/serviceaccount/*.crt /etc/x509/custom/rhbk.crt"
volumes:
  items:
    - configMaps:
        - rhbk-ca-cert
      mountPath: /etc/x509/custom
      name: rhbk-ca
```

**RHBK** — add to `spec.additionalOptions` and mount:

```yaml
additionalOptions:
  - name: truststore-paths
    value: /opt/keycloak/certs/rhsso.crt
```

Mount the ConfigMap via `spec.unsupported.podTemplate`. Wait for pod restarts.

### Step 1.4 — Create broker clients (5 min)

**On RH-SSO:** Create client `rhbk-broker` (confidential, redirect URI: `https://<RHBK_ROUTE>/realms/<REALM>/broker/rhsso/endpoint/*`)

**On RHBK:** Create client `rhsso-broker` (confidential, redirect URI: `https://<RHSSO_ROUTE>/auth/realms/<REALM>/broker/rhbk/endpoint/*`)

Note the client IDs and secrets for the next step.

### Step 1.5 — Add Identity Providers (5 min)

**On RH-SSO:** Identity Providers → Add → OpenID Connect v1.0

| Field | Value |
|---|---|
| Alias | `rhbk` |
| Authorization URL | `https://<RHBK_ROUTE>/realms/<REALM>/protocol/openid-connect/auth` |
| Token URL | `https://<RHBK_ROUTE>/realms/<REALM>/protocol/openid-connect/token` |
| Client ID | `rhsso-broker` (from RHBK) |
| Client Secret | (from RHBK) |
| Validate Signatures | ON |
| Use JWKS URL | ON |
| JWKS URL | `https://<RHBK_ROUTE>/realms/<REALM>/protocol/openid-connect/certs` |

**On RHBK:** Same but reversed — alias `rhsso`, RH-SSO URLs (with `/auth/` prefix), using `rhbk-broker` client from RH-SSO.

### Step 1.6 — Create `token-exchange-client` on both (5 min)

On **both** IdPs:

1. Create client: `token-exchange-client`, confidential, service accounts enabled, standard flow OFF
2. Credentials tab: note the secret
3. Add protocol mapper: `oidc-audience-mapper`, included client audience = `token-exchange-client`, access token = ON
4. Permissions tab: toggle Permissions Enabled → ON
5. Click `token-exchange` permission → create a Client policy allowing `token-exchange-client`
6. Also enable Permissions on the **Identity Provider** (go to IdP → Permissions → ON) and link the same policy

---

## Phase 1 Checkpoint: Verify Exchange Works (5 min)

**⚠️ STOP HERE IF THIS FAILS. Debug Keycloak config before proceeding.**

```bash
# Get an RH-SSO token
RHSSO_TOKEN=$(curl -sk -X POST \
  "https://<RHSSO_ROUTE>/auth/realms/<REALM>/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<SECRET>" \
  -d "username=<USER>" -d "password=<PASS>" \
  -d "grant_type=password" -d "scope=openid" | jq -r .access_token)

# Exchange at RHBK — should return a new token
curl -sk -X POST \
  "https://<RHBK_ROUTE>/realms/<REALM>/protocol/openid-connect/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$RHSSO_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhsso" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<SECRET>" \
  -d "scope=openid" | jq .

# Test reverse direction too (RHBK → RH-SSO)
```

If both directions return an `access_token` → **proceed to Phase 2**.

---

## Phase 2: Deploy the Proxy (~10 min)

### Step 2.1 — Load images into customer's registry

```bash
podman load -i sso-token-exchange-proxy.tar
podman tag quay.io/dbirenfe/sso-token-exchange-proxy:latest <REGISTRY>/sso-token-exchange-proxy:latest
podman push <REGISTRY>/sso-token-exchange-proxy:latest
```

### Step 2.2 — Create namespace

```bash
oc apply -f token-exchange-proxy/00-namespace.yaml
```

### Step 2.3 — Customize ConfigMap

Edit `token-exchange-proxy/02-configmap.yaml` — replace these values:

| Field | Replace with |
|---|---|
| `TARGET_URL` (rhsso) | `https://<RHSSO_SVC>.<RHSSO_NS>.svc.cluster.local:<PORT>` |
| `TARGET_URL` (rhbk) | `https://<RHBK_SVC>.<RHBK_NS>.svc.cluster.local:<PORT>` |
| `TOKEN_ENDPOINT` (rhsso) | `https://<RHSSO_SVC>.../<REALM>/protocol/openid-connect/token` |
| `TOKEN_ENDPOINT` (rhbk) | `https://<RHBK_SVC>.../<REALM>/protocol/openid-connect/token` |
| `IDP_EXTERNAL_HOST` (rhsso) | The RH-SSO external route hostname (no `https://`) |
| `IDP_EXTERNAL_HOST` (rhbk) | The RHBK external route hostname (no `https://`) |

### Step 2.4 — Customize Secret

Edit `token-exchange-proxy/03-secret.yaml` — set the real `EXCHANGE_CLIENT_SECRET`.

### Step 2.5 — Customize Deployment images

If using customer's registry, update the `image` field in `token-exchange-proxy/04-deployment.yaml`.

### Step 2.6 — Apply everything

```bash
oc apply -f token-exchange-proxy/02-configmap.yaml
oc apply -f token-exchange-proxy/03-secret.yaml
oc apply -f token-exchange-proxy/04-deployment.yaml
oc apply -f token-exchange-proxy/05-service.yaml
```

Verify pods are running:

```bash
oc get pods -n sso-gateway -l app.kubernetes.io/component=token-exchange-proxy
```

---

## Phase 3: Take Over IdP Routes (~5 min)

### Step 3.1 — Disable RHBK operator ingress

```bash
oc patch keycloak.k8s.keycloak.org <name> -n <rhbk-ns> \
  --type=merge -p '{"spec":{"ingress":{"enabled":false}}}'
```

### Step 3.2 — Delete existing IdP routes

```bash
oc delete route <rhsso-route-name> -n <rhsso-ns>
oc delete route <rhbk-route-name> -n <rhbk-ns>
```

### Step 3.3 — Update and apply proxy routes

Edit `token-exchange-proxy/06-route-rhsso.yaml` and `07-route-rhbk.yaml` — set `spec.host` to the customer's actual IdP hostnames.

```bash
oc apply -f token-exchange-proxy/06-route-rhsso.yaml
oc apply -f token-exchange-proxy/07-route-rhbk.yaml
```

---

## Phase 4: Test with the Customer's App (~15 min)

### Test 1 — App still works normally
Have the customer use their app as usual. Login, access resources — everything should work (valid tokens pass through the proxy untouched).

### Test 2 — The migration scenario (the demo moment)
1. Use the app that authenticates against RH-SSO → get a token
2. Use that RH-SSO token to call something on RHBK (through the proxy)
3. It should work — the proxy exchanged the token transparently

### Test 3 — Check proxy logs for proof

```bash
oc logs -l app.kubernetes.io/component=token-exchange-proxy -n sso-gateway --tail=20
```

Look for:
```
IdP returned 401 ... Attempting token exchange.
Token exchanged successfully — retrying request.
```

### Test 4 — Admin consoles still work
Log into both admin consoles through the proxy routes. No login loop.

---

## Summary

| # | Phase | Time | Risk |
|---|---|---|---|
| 0 | Gather cluster info | 10 min | None |
| 1.1 | Enable features on RH-SSO | 5 min | Pod restart |
| 1.2 | Enable features on RHBK | 5 min | Pod restart |
| 1.3 | Exchange TLS certificates | 10 min | Pod restarts |
| 1.4 | Create broker clients | 5 min | None |
| 1.5 | Add Identity Providers | 5 min | None |
| 1.6 | Create token-exchange-client + permissions | 5 min | None |
| **✓** | **Verify exchange works (curl)** | **5 min** | **⚠️ STOP if fails** |
| 2 | Deploy proxy (configmap, secret, deployment, service) | 10 min | None |
| 3 | Take over IdP routes | 5 min | Brief blip |
| 4 | Test with customer's app | 15 min | None |
| | **Total** | **~75 min** | |

---

## Troubleshooting Quick Reference

| Error | Likely cause | Fix |
|---|---|---|
| `unsupported_grant_type` | Token exchange feature not enabled | Check CR feature flags, restart pod |
| `Token not authorized` | Missing fine-grained permissions | Enable permissions on client AND IdP, create policies |
| `invalid_token` / `Key not found` | TLS trust missing | Check cert ConfigMaps are mounted, pod restarted |
| `user info call failure` | IdP can't reach the other IdP's userinfo | TLS cert not trusted, check `X509_CA_BUNDLE` / `truststore-paths` |
| Exchanged token rejected (`:8443` in issuer) | Internal port in token issuer | Set `IDP_EXTERNAL_HOST` in ConfigMap |
| Admin console login loop | Duplicate Set-Cookie headers lost | Ensure proxy image is latest (uses `raw.headers`) |
| RHBK route keeps reappearing | Operator recreates ingress | Set `spec.ingress.enabled: false` in RHBK CR |
