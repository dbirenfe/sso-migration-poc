# Offline Deployment Guide

## Deploying the SSO Migration Solution to an Air-Gapped Customer Environment

**Scenario:** You are on-site at a customer with an **offline (air-gapped) OpenShift cluster** that already has **RH-SSO 7.6.5** and **RHBK 26.4** running. You need to deploy the token exchange solution (IdP Gateway Mode) so they can begin gradual application migration.

---

## Table of Contents

- [Pre-Visit Preparation (Online)](#pre-visit-preparation-online)
- [Step 1: Gather Customer Environment Info](#step-1-gather-customer-environment-info)
- [Step 2: Load Container Images into the Customer Registry](#step-2-load-container-images-into-the-customer-registry)
- [Step 3: Enable Token Exchange Features on RH-SSO](#step-3-enable-token-exchange-features-on-rh-sso)
- [Step 4: Enable Token Exchange Features on RHBK](#step-4-enable-token-exchange-features-on-rhbk)
- [Step 5: Create the Token Exchange Client on Both IdPs](#step-5-create-the-token-exchange-client-on-both-idps)
- [Step 6: Establish TLS Trust Between IdPs](#step-6-establish-tls-trust-between-idps)
- [Step 7: Register Identity Providers (Bidirectional Trust)](#step-7-register-identity-providers-bidirectional-trust)
- [Step 8: Configure Fine-Grained Authorization (Permissions)](#step-8-configure-fine-grained-authorization-permissions)
- [Step 9: Verify Token Exchange Works](#step-9-verify-token-exchange-works)
- [Step 10: Deploy the Token Exchange Proxy](#step-10-deploy-the-token-exchange-proxy)
- [Step 11: Integrate with the Customer's First Application](#step-11-integrate-with-the-customers-first-application)
- [Step 12: Deploy the Demo App (Optional)](#step-12-deploy-the-demo-app-optional)
- [Quick Reference — What to Bring](#quick-reference--what-to-bring)

---

## Pre-Visit Preparation (Online)

Do this **before** going to the customer, while you still have internet access.

### 1. Save container images to tarballs

```bash
# On your laptop (with internet access and podman/docker installed):

# Pull and save the proxy image
podman pull quay.io/dbirenfe/sso-token-exchange-proxy:latest
podman save -o sso-token-exchange-proxy.tar quay.io/dbirenfe/sso-token-exchange-proxy:latest

# Pull and save the demo app image
podman pull quay.io/dbirenfe/sso-migration-demo:latest
podman save -o sso-migration-demo.tar quay.io/dbirenfe/sso-migration-demo:latest
```

### 2. Clone the repo to a USB drive

```bash
git clone https://github.com/dbirenfe/sso-migration-poc.git
# Copy the entire folder + the two .tar files to a USB drive
```

### 3. What to bring to the customer site

| Item | Purpose |
|------|---------|
| USB drive with `sso-migration-poc/` repo | All YAML manifests, docs, scripts |
| `sso-token-exchange-proxy.tar` | Proxy container image |
| `sso-migration-demo.tar` (optional) | Demo app container image |
| This guide (printed or on laptop) | Step-by-step reference |
| Laptop with `oc` CLI installed | To run commands against the cluster |

---

## Step 1: Gather Customer Environment Info

Before touching anything, collect these details. You'll need them for every step that follows.

```bash
# Log in to the customer's OCP cluster
oc login https://<customer-api-server>:6443 -u <admin-user>

# Find the cluster's apps domain
oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}'
# Example output: apps.customer-cluster.example.com
```

Fill in this table — it will be referenced throughout:

| Variable | How to Find | Your Value |
|----------|-------------|------------|
| `CLUSTER_DOMAIN` | `oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}'` | |
| `RHSSO_NAMESPACE` | Ask customer (usually `rhsso` or `keycloak`) | |
| `RHBK_NAMESPACE` | Ask customer (usually `rhbk`) | |
| `RHSSO_ROUTE` | `oc -n <rhsso-ns> get route -o jsonpath='{.items[0].spec.host}'` | |
| `RHBK_ROUTE` | `oc -n <rhbk-ns> get route -o jsonpath='{.items[0].spec.host}'` | |
| `RHSSO_CR_NAME` | `oc -n <rhsso-ns> get keycloak -o jsonpath='{.items[0].metadata.name}'` | |
| `RHBK_CR_NAME` | `oc -n <rhbk-ns> get keycloak -o jsonpath='{.items[0].metadata.name}'` | |
| `RHSSO_INTERNAL_SVC` | `oc -n <rhsso-ns> get svc -o name` (look for the keycloak service) | |
| `RHBK_INTERNAL_SVC` | `oc -n <rhbk-ns> get svc -o name` (look for the keycloak service) | |
| `REALM` | Ask customer which realm to use (or create a new one) | |
| `REGISTRY` | The customer's internal container registry URL | |
| `RHSSO_ADMIN_USER` | `oc -n <rhsso-ns> get secret credential-<CR_NAME> -o jsonpath='{.data.ADMIN_USERNAME}' \| base64 -d` | |
| `RHSSO_ADMIN_PASS` | `oc -n <rhsso-ns> get secret credential-<CR_NAME> -o jsonpath='{.data.ADMIN_PASSWORD}' \| base64 -d` | |
| `RHBK_ADMIN_USER` | Usually `admin` | |
| `RHBK_ADMIN_PASS` | `oc -n <rhbk-ns> get secret <CR_NAME>-initial-admin -o jsonpath='{.data.password}' \| base64 -d` | |

> **Tip:** The RH-SSO Keycloak CR name is often `rhsso` or `keycloak`. The RHBK one is often `rhbk`. Run `oc get keycloak -A` to see all instances.

Also check the internal service names and ports:

```bash
# RH-SSO internal service (typically "keycloak" in the rhsso namespace)
oc -n <rhsso-ns> get svc
# Look for the ClusterIP service on port 8443 — note its name

# RHBK internal service (typically "rhbk-service" in the rhbk namespace)
oc -n <rhbk-ns> get svc
# Look for the ClusterIP service on port 8443 — note its name
```

---

## Step 2: Load Container Images into the Customer Registry

The customer's cluster can't pull from the internet. You need to load images into their **internal registry** (either the OCP internal registry or a private registry like Nexus, Artifactory, Harbor, etc.).

### Option A: Using the OCP Internal Registry

```bash
# Check if the internal registry is exposed
oc get route -n openshift-image-registry

# If no route, expose it:
oc patch configs.imageregistry.operator.openshift.io/cluster \
  --type=merge -p '{"spec":{"defaultRoute":true}}'

# Get the registry URL
REGISTRY=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')

# Log in to the registry
podman login -u $(oc whoami) -p $(oc whoami -t) ${REGISTRY} --tls-verify=false
```

### Option B: Using a Customer Private Registry

```bash
# The customer will tell you the registry URL and credentials
REGISTRY="registry.customer.example.com"
podman login ${REGISTRY}
```

### Load and push the images

```bash
# Load images from tarballs
podman load -i sso-token-exchange-proxy.tar
podman load -i sso-migration-demo.tar

# Tag for the customer's registry
# Create a project/namespace in the registry (if OCP internal registry,
# the namespace "sso-gateway" will be used automatically)
podman tag quay.io/dbirenfe/sso-token-exchange-proxy:latest \
  ${REGISTRY}/sso-gateway/sso-token-exchange-proxy:latest

podman tag quay.io/dbirenfe/sso-migration-demo:latest \
  ${REGISTRY}/sso-gateway/sso-migration-demo:latest

# Push to customer registry
podman push ${REGISTRY}/sso-gateway/sso-token-exchange-proxy:latest --tls-verify=false
podman push ${REGISTRY}/sso-gateway/sso-migration-demo:latest --tls-verify=false
```

> **Note:** If using the OCP internal registry, the image references in the deployment YAMLs should use `image-registry.openshift-image-registry.svc:5000/sso-gateway/<image>:latest`. If using an external registry, use the full registry URL.

---

## Step 3: Enable Token Exchange Features on RH-SSO

Token exchange is a **tech-preview** feature in RH-SSO 7.6.5 and is disabled by default.

```bash
RHSSO_NS=<rhsso-namespace>
RHSSO_CR=<rhsso-cr-name>

# Check if features are already enabled
oc -n ${RHSSO_NS} get keycloak ${RHSSO_CR} -o jsonpath='{.spec.keycloakDeploymentSpec.experimental.env}' | python3 -m json.tool

# If token_exchange is NOT already enabled, patch the CR:
oc -n ${RHSSO_NS} patch keycloak ${RHSSO_CR} --type=merge -p '{
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

# Wait for the pod to restart
oc -n ${RHSSO_NS} rollout status statefulset/keycloak

# Verify features are active
oc -n ${RHSSO_NS} logs keycloak-0 | grep -i "token.exchange"
```

> **Important:** This will restart the RH-SSO pod. Coordinate with the customer — existing sessions will be interrupted briefly. If they have HA (2+ replicas), the restart is rolling and there's no downtime.

---

## Step 4: Enable Token Exchange Features on RHBK

RHBK 26.4 needs `admin-fine-grained-authz:v1` enabled and `token-exchange-external-internal` **disabled**.

```bash
RHBK_NS=<rhbk-namespace>
RHBK_CR=<rhbk-cr-name>

# Check current feature state
oc -n ${RHBK_NS} get keycloak ${RHBK_CR} -o jsonpath='{.spec.features}'

# Patch the CR
oc -n ${RHBK_NS} patch keycloak ${RHBK_CR} --type=merge -p '{
  "spec": {
    "startOptimized": false,
    "features": {
      "enabled": ["preview", "admin-fine-grained-authz:v1"],
      "disabled": ["token-exchange-external-internal"]
    }
  }
}'

# Wait for the pod to restart
oc -n ${RHBK_NS} rollout status statefulset/rhbk
```

> **Why `startOptimized: false`?** `admin-fine-grained-authz` is a build-time feature. Without a custom image that bakes it in, RHBK must run in non-optimized mode to enable it at runtime. For production, build a custom image (see the implementation guide, Section 11.2).

> **Why disable `token-exchange-external-internal`?** When enabled (v2), it bypasses fine-grained authorization entirely. We need fine-grained authorization to work properly to control which clients can perform exchanges.

---

## Step 5: Create the Token Exchange Client on Both IdPs

The customer's existing clients remain untouched. You create a **new** dedicated client (`token-exchange-client`) used exclusively by the proxy for performing exchanges.

### 5A. On RH-SSO

```bash
RHSSO_URL="https://${RHSSO_ROUTE}"
REALM="<realm-name>"

# Get admin token
ADMIN_TOKEN=$(curl -sk "${RHSSO_URL}/auth/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${RHSSO_ADMIN_USER}" \
  -d "password=${RHSSO_ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create the client
curl -sk -X POST "${RHSSO_URL}/auth/admin/realms/${REALM}/clients" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "token-exchange-client",
    "enabled": true,
    "publicClient": false,
    "secret": "<GENERATE-A-STRONG-SECRET>",
    "directAccessGrantsEnabled": true,
    "serviceAccountsEnabled": true,
    "standardFlowEnabled": false,
    "protocol": "openid-connect"
  }'

# Get the client UUID (needed for later steps)
RHSSO_CLIENT_UUID=$(curl -sk "${RHSSO_URL}/auth/admin/realms/${REALM}/clients?clientId=token-exchange-client" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
echo "RH-SSO client UUID: ${RHSSO_CLIENT_UUID}"

# Add audience mapper (required for chained exchange to work)
curl -sk -X POST "${RHSSO_URL}/auth/admin/realms/${REALM}/clients/${RHSSO_CLIENT_UUID}/protocol-mappers/models" \
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

### 5B. On RHBK

```bash
RHBK_URL="https://${RHBK_ROUTE}"

# Get admin token
RHBK_ADMIN_TOKEN=$(curl -sk "${RHBK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" \
  -d "username=${RHBK_ADMIN_USER}" \
  -d "password=${RHBK_ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create the client (same secret as RH-SSO)
curl -sk -X POST "${RHBK_URL}/admin/realms/${REALM}/clients" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "token-exchange-client",
    "enabled": true,
    "publicClient": false,
    "secret": "<SAME-STRONG-SECRET>",
    "directAccessGrantsEnabled": true,
    "serviceAccountsEnabled": true,
    "standardFlowEnabled": false,
    "protocol": "openid-connect"
  }'

# Get the client UUID
RHBK_CLIENT_UUID=$(curl -sk "${RHBK_URL}/admin/realms/${REALM}/clients?clientId=token-exchange-client" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
echo "RHBK client UUID: ${RHBK_CLIENT_UUID}"

# Add audience mapper
curl -sk -X POST "${RHBK_URL}/admin/realms/${REALM}/clients/${RHBK_CLIENT_UUID}/protocol-mappers/models" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
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

> **Secret management:** Use a strong, randomly generated secret (e.g., `openssl rand -base64 32`). Use the **same** secret on both IdPs for simplicity, but different secrets are fine — the proxy is configured per-direction.

---

## Step 6: Establish TLS Trust Between IdPs

Each IdP needs to trust the other's TLS certificate. In an offline environment, these are likely internal CA-signed certs.

### 6A. Get RHBK's certificate and make RH-SSO trust it

```bash
# Method 1: From the RHBK TLS secret
oc -n ${RHBK_NS} get secret <rhbk-tls-secret-name> -o jsonpath='{.data.tls\.crt}' | base64 -d > /tmp/rhbk.crt

# Method 2: From the route directly (if Method 1 doesn't work)
echo | openssl s_client -connect ${RHBK_ROUTE}:443 -servername ${RHBK_ROUTE} 2>/dev/null | \
  openssl x509 > /tmp/rhbk.crt

# Verify you got a valid certificate
openssl x509 -in /tmp/rhbk.crt -text -noout | head -10

# Create ConfigMap in RH-SSO namespace
oc -n ${RHSSO_NS} create configmap rhbk-ca-cert --from-file=rhbk.crt=/tmp/rhbk.crt

# Patch RH-SSO CR to mount the cert and add to X509_CA_BUNDLE
# This makes it PERSISTENT across pod restarts
oc -n ${RHSSO_NS} patch keycloak ${RHSSO_CR} --type=merge -p '{
  "spec": {
    "keycloakDeploymentSpec": {
      "experimental": {
        "env": [
          {
            "name": "JAVA_OPTS_APPEND",
            "value": "-Dkeycloak.profile.feature.token_exchange=enabled -Dkeycloak.profile.feature.admin_fine_grained_authz=enabled"
          },
          {
            "name": "X509_CA_BUNDLE",
            "value": "/etc/x509/custom/rhbk.crt"
          }
        ],
        "volumes": {
          "defaultMode": 420,
          "items": [
            {
              "name": "rhbk-ca-cert",
              "configMap": {
                "name": "rhbk-ca-cert"
              },
              "mountPath": "/etc/x509/custom"
            }
          ]
        }
      }
    }
  }
}'
```

> **Why X509_CA_BUNDLE?** The JBoss EAP startup scripts read this variable and import the listed certificates into the Java truststore **on every boot**. This survives pod restarts without manual `keytool` commands.

### 6B. Get RH-SSO's certificate and make RHBK trust it

```bash
# Extract RH-SSO cert
echo | openssl s_client -connect ${RHSSO_ROUTE}:443 -servername ${RHSSO_ROUTE} 2>/dev/null | \
  openssl x509 > /tmp/rhsso.crt

# Create ConfigMap in RHBK namespace
oc -n ${RHBK_NS} create configmap rhsso-ca-cert --from-file=rhsso.crt=/tmp/rhsso.crt

# Patch RHBK CR to mount the cert and configure truststore-paths
oc -n ${RHBK_NS} patch keycloak ${RHBK_CR} --type=merge -p '{
  "spec": {
    "additionalOptions": [
      {"name": "truststore-paths", "value": "/opt/keycloak/certs/rhsso.crt"},
      {"name": "log-level", "value": "INFO"}
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

> **If the customer uses a shared internal CA:** You may only need to import the CA root certificate (not each IdP's individual cert). Ask the customer's PKI team for the root CA cert.

---

## Step 7: Register Identity Providers (Bidirectional Trust)

### 7A. Register RHBK as an IdP in RH-SSO

```bash
# Refresh admin token (they expire quickly)
ADMIN_TOKEN=$(curl -sk "${RHSSO_URL}/auth/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" -d "username=${RHSSO_ADMIN_USER}" -d "password=${RHSSO_ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -X POST "${RHSSO_URL}/auth/admin/realms/${REALM}/identity-provider/instances" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "rhbk",
    "providerId": "oidc",
    "enabled": true,
    "trustEmail": true,
    "config": {
      "clientId": "token-exchange-client",
      "clientSecret": "<SAME-STRONG-SECRET>",
      "authorizationUrl": "'"${RHBK_URL}"'/realms/'"${REALM}"'/protocol/openid-connect/auth",
      "tokenUrl": "'"${RHBK_URL}"'/realms/'"${REALM}"'/protocol/openid-connect/token",
      "userInfoUrl": "'"${RHBK_URL}"'/realms/'"${REALM}"'/protocol/openid-connect/userinfo",
      "jwksUrl": "'"${RHBK_URL}"'/realms/'"${REALM}"'/protocol/openid-connect/certs",
      "issuer": "'"${RHBK_URL}"'/realms/'"${REALM}"'",
      "validateSignature": "true",
      "useJwksUrl": "true"
    }
  }'
```

### 7B. Register RH-SSO as an IdP in RHBK

```bash
# Refresh RHBK admin token
RHBK_ADMIN_TOKEN=$(curl -sk "${RHBK_URL}/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" -d "username=${RHBK_ADMIN_USER}" -d "password=${RHBK_ADMIN_PASS}" \
  -d "grant_type=password" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -X POST "${RHBK_URL}/admin/realms/${REALM}/identity-provider/instances" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "alias": "rhsso",
    "providerId": "oidc",
    "enabled": true,
    "trustEmail": true,
    "config": {
      "clientId": "token-exchange-client",
      "clientSecret": "<SAME-STRONG-SECRET>",
      "authorizationUrl": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'/protocol/openid-connect/auth",
      "tokenUrl": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'/protocol/openid-connect/token",
      "userInfoUrl": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'/protocol/openid-connect/userinfo",
      "jwksUrl": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'/protocol/openid-connect/certs",
      "issuer": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'",
      "introspectionUrl": "'"${RHSSO_URL}"'/auth/realms/'"${REALM}"'/protocol/openid-connect/token/introspect",
      "validateSignature": "true",
      "useJwksUrl": "true"
    }
  }'
```

> **Critical:** The `introspectionUrl` is mandatory for RHBK. Without it, token exchange fails with `"Introspection endpoint not configured for IDP"`.

> **URL note:** RH-SSO URLs include `/auth` in the path. RHBK URLs do **not**. Double-check you're using the correct format for each IdP.

---

## Step 8: Configure Fine-Grained Authorization (Permissions)

Both IdPs need to explicitly permit the `token-exchange-client` to perform token exchanges.

### 8A. On RH-SSO (Easier via Admin Console)

1. Open RH-SSO Admin Console: `https://${RHSSO_ROUTE}/auth/admin/`
2. Select the target realm
3. Go to **Clients** → `token-exchange-client` → **Permissions** tab → Toggle **ON**
4. Click the `token-exchange` scope
5. Create a **Client** policy → select `token-exchange-client` → Save
6. Go to **Identity Providers** → `rhbk` → **Permissions** tab → Toggle **ON**
7. Click `token-exchange` scope → Apply the same client policy

Or via REST API:

```bash
# Enable management permissions on the client
curl -sk -X PUT "${RHSSO_URL}/auth/admin/realms/${REALM}/clients/${RHSSO_CLIENT_UUID}/management/permissions" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# The rest is easier in the admin console (see above)
```

### 8B. On RHBK (Easier via Admin Console)

1. Open RHBK Admin Console: `https://${RHBK_ROUTE}/admin/`
2. Select the target realm
3. Go to **Clients** → `token-exchange-client` → **Permissions** tab → Toggle **ON**
4. Go to **Identity Providers** → `rhsso` → **Permissions** tab → Toggle **ON**
5. Go to **Clients** → `realm-management` → **Authorization** tab → **Policies**
6. Create a **Client** policy named `allow-token-exchange-client` → select `token-exchange-client`
7. Create a **User** policy named `allow-token-exchange-sa`:
   - Find the service account user: go to **Users**, search for `service-account-token-exchange-client`
   - Select that user in the policy
8. Go to **Permissions** tab → find `token-exchange.permission.client.<uuid>` → add both policies, set decision strategy to **Affirmative**
9. Find `token-exchange.permission.idp.rhsso` → add both policies, set decision strategy to **Affirmative**

Or via REST API:

```bash
# Enable management permissions on the client
curl -sk -X PUT "${RHBK_URL}/admin/realms/${REALM}/clients/${RHBK_CLIENT_UUID}/management/permissions" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Enable management permissions on the rhsso IdP
curl -sk -X PUT "${RHBK_URL}/admin/realms/${REALM}/identity-provider/instances/rhsso/management/permissions" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Get realm-management client UUID
REALM_MGMT_UUID=$(curl -sk "${RHBK_URL}/admin/realms/${REALM}/clients?clientId=realm-management" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Create client policy
curl -sk -X POST "${RHBK_URL}/admin/realms/${REALM}/clients/${REALM_MGMT_UUID}/authz/resource-server/policy/client" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-token-exchange-client",
    "clients": ["'"${RHBK_CLIENT_UUID}"'"],
    "logic": "POSITIVE"
  }'

# Create user policy for the service account
SA_USER_ID=$(curl -sk "${RHBK_URL}/admin/realms/${REALM}/clients/${RHBK_CLIENT_UUID}/service-account-user" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -sk -X POST "${RHBK_URL}/admin/realms/${REALM}/clients/${REALM_MGMT_UUID}/authz/resource-server/policy/user" \
  -H "Authorization: Bearer ${RHBK_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-token-exchange-service-account",
    "users": ["'"${SA_USER_ID}"'"],
    "logic": "POSITIVE"
  }'

# Now link the policies to permissions — this is the hardest part via API.
# Use the admin console for this step (see instructions above).
```

---

## Step 9: Verify Token Exchange Works

Before deploying any proxy, verify that bidirectional exchange works directly between the IdPs.

```bash
# Get a token from RH-SSO
RHSSO_TOKEN=$(curl -sk "${RHSSO_URL}/auth/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<YOUR-SECRET>" \
  -d "username=testuser" \
  -d "password=testpass" \
  -d "grant_type=password" \
  -d "scope=openid" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Got RH-SSO token: ${RHSSO_TOKEN:0:50}..."

# Exchange it at RHBK (Direction: RH-SSO → RHBK)
curl -sk "${RHBK_URL}/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<YOUR-SECRET>" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${RHSSO_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhsso" \
  -d "scope=openid" | python3 -m json.tool

# You should see an access_token in the response.
# If you see an error, refer to the Troubleshooting Guide.
```

Repeat in the other direction (RHBK → RH-SSO):

```bash
RHBK_TOKEN=$(curl -sk "${RHBK_URL}/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<YOUR-SECRET>" \
  -d "username=testuser" \
  -d "password=testpass" \
  -d "grant_type=password" \
  -d "scope=openid" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk "${RHSSO_URL}/auth/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" \
  -d "client_secret=<YOUR-SECRET>" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${RHBK_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhbk" \
  -d "scope=openid" | python3 -m json.tool
```

> **Both directions must work** before proceeding to Step 10. If either fails, check the Troubleshooting Guide in the implementation doc.

> **No test user?** If the customer doesn't have a test user, you can use a service account token instead (client_credentials grant) or create a temporary test user in the realm.

---

## Step 10: Deploy the Token Exchange Proxy (IdP Gateway Mode)

The proxy is deployed **in front of each IdP** (not in front of applications). Two instances: `idp-proxy-rhsso` in front of RH-SSO and `idp-proxy-rhbk` in front of RHBK. The proxy uses a "try-first, exchange-on-failure" approach — it forwards requests to the IdP as-is, and only exchanges the token if the IdP rejects it with 401/403.

### 10.1 Create the namespace

```bash
oc create namespace sso-gateway
oc label namespace ${RHSSO_NS} sso-migration/idp=true
oc label namespace ${RHBK_NS}  sso-migration/idp=true
oc label namespace sso-gateway  sso-migration/access=true
```

### 10.2 Disable RHBK operator ingress

The RHBK operator automatically creates a Route for the RHBK hostname. This competes with the proxy route and must be disabled:

```bash
oc -n ${RHBK_NS} patch keycloak ${RHBK_CR} --type=merge -p '{
  "spec": {
    "ingress": {
      "enabled": false
    }
  }
}'
```

### 10.3 Create the ConfigMaps

You need to customize these for the customer's environment. The `TARGET_URL` points to the IdP's internal service (not an application).

```bash
# ── Proxy in front of RH-SSO ──
# Forwards all traffic to RH-SSO. Exchanges RHBK tokens → RH-SSO tokens on 401/403.

RHSSO_SVC_NAME="<service-name>"    # from Step 1, e.g. "keycloak"
RHSSO_SVC_PORT="8443"              # typically 8443

cat <<EOF | oc apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: idp-proxy-rhsso-config
  namespace: sso-gateway
data:
  TARGET_URL: "https://${RHSSO_SVC_NAME}.${RHSSO_NS}.svc.cluster.local:${RHSSO_SVC_PORT}"
  TOKEN_ENDPOINT: "https://${RHSSO_SVC_NAME}.${RHSSO_NS}.svc.cluster.local:${RHSSO_SVC_PORT}/auth/realms/${REALM}/protocol/openid-connect/token"
  IDP_EXTERNAL_HOST: "${RHSSO_ROUTE}"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhbk"
  VERIFY_UPSTREAM_TLS: "false"
  LISTEN_PORT: "8080"
  RETRY_STATUS_CODES: "401,403"
  REQUEST_TIMEOUT: "30"
EOF

# ── Proxy in front of RHBK ──
# Forwards all traffic to RHBK. Exchanges RH-SSO tokens → RHBK tokens on 401/403.

RHBK_SVC_NAME="<service-name>"    # from Step 1, e.g. "rhbk-service"
RHBK_SVC_PORT="8443"

cat <<EOF | oc apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: idp-proxy-rhbk-config
  namespace: sso-gateway
data:
  TARGET_URL: "https://${RHBK_SVC_NAME}.${RHBK_NS}.svc.cluster.local:${RHBK_SVC_PORT}"
  TOKEN_ENDPOINT: "https://${RHBK_SVC_NAME}.${RHBK_NS}.svc.cluster.local:${RHBK_SVC_PORT}/realms/${REALM}/protocol/openid-connect/token"
  IDP_EXTERNAL_HOST: "${RHBK_ROUTE}"
  GRANT_TYPE: "token-exchange"
  IDP_ALIAS: "rhsso"
  VERIFY_UPSTREAM_TLS: "false"
  LISTEN_PORT: "8080"
  RETRY_STATUS_CODES: "401,403"
  REQUEST_TIMEOUT: "30"
EOF
```

> **`IDP_EXTERNAL_HOST`:** Set to the IdP's external route hostname (no `https://`, no port). The proxy uses this as the `Host` header on exchange calls so that exchanged tokens have the correct issuer URL (without `:8443`).

> **`VERIFY_UPSTREAM_TLS: "false"`:** For POC/initial setup this is fine. For production, mount the IdP CA cert into the proxy pod and set this to `"true"`.

> **`PASSTHROUGH_PREFIXES`:** Default is `/admin,/js,/resources,/robots.txt`. These paths skip the exchange logic entirely. Adjust if the customer has specific paths that should never trigger exchange.

### 10.4 Create the Secrets

```bash
cat <<EOF | oc apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: idp-proxy-rhsso-credentials
  namespace: sso-gateway
type: Opaque
stringData:
  EXCHANGE_CLIENT_ID: "token-exchange-client"
  EXCHANGE_CLIENT_SECRET: "<YOUR-SECRET>"
---
apiVersion: v1
kind: Secret
metadata:
  name: idp-proxy-rhbk-credentials
  namespace: sso-gateway
type: Opaque
stringData:
  EXCHANGE_CLIENT_ID: "token-exchange-client"
  EXCHANGE_CLIENT_SECRET: "<YOUR-SECRET>"
EOF
```

### 10.5 Deploy the proxy

Update the image reference in the deployment YAML to match the customer's registry, then apply:

```bash
# If using OCP internal registry:
IMAGE="image-registry.openshift-image-registry.svc:5000/sso-gateway/sso-token-exchange-proxy:latest"

# If using customer's private registry:
IMAGE="${REGISTRY}/sso-gateway/sso-token-exchange-proxy:latest"

# Apply the deployment (edit the image field first, or use sed)
oc apply -f token-exchange-proxy/04-deployment.yaml
oc apply -f token-exchange-proxy/05-service.yaml

# If the image reference doesn't match, patch it:
oc -n sso-gateway set image deployment/idp-proxy-rhsso proxy=${IMAGE}
oc -n sso-gateway set image deployment/idp-proxy-rhbk proxy=${IMAGE}

# Verify pods are running
oc -n sso-gateway get pods
```

### 10.6 Create Routes

The OCP Routes for each IdP hostname must point to the proxy services:

```bash
oc apply -f token-exchange-proxy/06-route-rhsso.yaml
oc apply -f token-exchange-proxy/07-route-rhbk.yaml
```

> **Important:** Edit the route YAMLs to use the customer's IdP hostnames. The routes use `edge` TLS termination. After applying, verify that the IdP admin consoles are accessible through the proxy routes.

### 10.7 Apply Network Policies

```bash
oc apply -f network-policy/network-policy.yaml
```

---

## Step 11: Integrate with the Customer's First Application

With the IdP Gateway Mode, the proxies are already deployed in front of each IdP — **no per-application proxy setup is needed**. This step is about migrating the first application and verifying the exchange works end-to-end.

### Scenario: System A migrates to RHBK, System B stays on RH-SSO

**Before migration:**
```
System B → (RH-SSO token) → System A (validates against RH-SSO) ✅
```

**After migration without proxy:**
```
System B → (RH-SSO token) → System A (now validates against RHBK) ❌ invalid_token
```

**After migration with IdP Gateway Proxy:**

```
┌───────────┐                                                 ┌───────────┐
│  System B │  ① Auth (through idp-proxy-rhsso → RH-SSO)     │  System A │
│ (on RHSSO)│──────────────────────────────────────────       │ (on RHBK) │
│           │  Gets an RH-SSO token (pass-through,            │           │
│           │  no exchange — no Bearer token involved)         │           │
│           │                                                  │           │
│           │  ② API call with RH-SSO token (direct)          │           │
│           │─────────────────────────────────────────────────►│           │
│           │                                                  │           │
│           │      System A validates at RHBK (through idp-proxy-rhbk):   │
│           │      ③ RHBK rejects RH-SSO token → 401                     │
│           │      ④ Proxy exchanges → RHBK token                        │
│           │      ⑤ Proxy retries → RHBK returns 200                    │
│           │                                                  │           │
│           │◄─────────────────── ⑥ Response ─────────────────│           │
└───────────┘                                                 └───────────┘
```

1. **System B authenticates against RH-SSO** (through `idp-proxy-rhsso`). The proxy passes the login/token request through unchanged — no Bearer token, so no exchange logic triggers. System B gets an RH-SSO token.
2. **System B calls System A's API** with the RH-SSO token. This is a direct call — the proxy is not in the app-to-app path.
3. **System A validates the token at RHBK** (through `idp-proxy-rhbk`). RHBK rejects the foreign RH-SSO token with 401.
4. **The proxy detects 401 + Bearer token**, exchanges the RH-SSO token for a RHBK token, and retries.
5. **RHBK accepts the exchanged token** and returns the validation response.
6. **System A responds** to System B. System B is completely unaware that a proxy was involved.

### What to do:

1. **Verify the proxies are running** (from Step 10):
```bash
oc -n sso-gateway get pods
# Should see idp-proxy-rhsso and idp-proxy-rhbk pods running
```

2. **Verify IdP access through the proxies:**
```bash
# RH-SSO admin console should work through the proxy
curl -sk "https://${RHSSO_ROUTE}/auth/admin/" -o /dev/null -w "%{http_code}"
# Should return 200 (or 302 redirect to login)

# RHBK admin console
curl -sk "https://${RHBK_ROUTE}/admin/" -o /dev/null -w "%{http_code}"
```

3. **Migrate System A** to validate against RHBK (the application team does this)

4. **Verify the exchange** by sending an RH-SSO token to RHBK through the proxy:

```bash
# Get an RH-SSO token
RHSSO_TOKEN=$(curl -sk "${RHSSO_URL}/auth/realms/${REALM}/protocol/openid-connect/token" \
  -d "client_id=token-exchange-client" -d "client_secret=<YOUR-SECRET>" \
  -d "username=testuser" -d "password=testpass" \
  -d "grant_type=password" -d "scope=openid" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Send it to RHBK's userinfo endpoint (through the proxy)
curl -sk "https://${RHBK_ROUTE}/realms/${REALM}/protocol/openid-connect/userinfo" \
  -H "Authorization: Bearer ${RHSSO_TOKEN}"
# Should return user info (the proxy exchanged the token automatically)

# Check the proxy logs
oc -n sso-gateway logs deploy/idp-proxy-rhbk --tail=10
# Should see: "IdP returned 401 ... Attempting token exchange" → "Token exchanged successfully — retrying request."
```

> **Key advantage of IdP Gateway Mode:** No application-level routing changes (Routes, ExternalName services) are needed. The proxy is transparent at the IdP level — all applications that validate tokens against the IdP automatically benefit.

---

## Step 12: Deploy the Demo App (Optional)

If you want to demonstrate the solution interactively:

```bash
# Update the image reference in deploy.yaml
IMAGE="${REGISTRY}/sso-gateway/sso-migration-demo:latest"

oc apply -f demo-app/k8s/deploy.yaml
oc -n sso-gateway set image deployment/sso-migration-demo demo=${IMAGE}

# Wait for it to come up
oc -n sso-gateway rollout status deployment/sso-migration-demo

# Get the demo app URL
echo "https://$(oc -n sso-gateway get route sso-migration-demo -o jsonpath='{.spec.host}')"
```

> **Note:** The demo app auto-discovers RH-SSO and RHBK URLs from OCP Routes. It needs the `ClusterRole` and `ClusterRoleBinding` defined in `deploy.yaml` to have permission to read routes and services across namespaces. Also, the demo uses `token-exchange-client` / `testuser` — make sure those exist in the customer's realm.

---

## Quick Reference — What to Bring

### USB Drive Contents

```
usb-drive/
├── sso-migration-poc/          # Git repo with all YAMLs and code
│   ├── docs/
│   │   ├── SSO-Migration-Implementation-Guide.md
│   │   └── Offline-Deployment-Guide.md  ← (this file)
│   ├── token-exchange-proxy/
│   │   ├── 02-configmap.yaml   ← CUSTOMIZE per customer
│   │   ├── 03-secret.yaml      ← CUSTOMIZE per customer
│   │   ├── 04-deployment.yaml  ← UPDATE image reference
│   │   └── 05-service.yaml
│   ├── network-policy/
│   │   └── network-policy.yaml
│   └── demo-app/
│       └── k8s/deploy.yaml     ← UPDATE image reference
├── sso-token-exchange-proxy.tar    # Container image tarball
└── sso-migration-demo.tar          # Container image tarball (optional)
```

### Files You Must Customize

| File | What to Change |
|------|----------------|
| `token-exchange-proxy/02-configmap.yaml` | `TARGET_URL`, `TOKEN_ENDPOINT`, `IDP_EXTERNAL_HOST` — all environment-specific |
| `token-exchange-proxy/03-secret.yaml` | `EXCHANGE_CLIENT_SECRET` — use customer's generated secret |
| `token-exchange-proxy/04-deployment.yaml` | `image` — point to customer's registry |
| `token-exchange-proxy/06-route-rhsso.yaml` | `spec.host` — set to customer's RH-SSO hostname |
| `token-exchange-proxy/07-route-rhbk.yaml` | `spec.host` — set to customer's RHBK hostname |
| `demo-app/k8s/deploy.yaml` | `image`, `CLIENT_SECRET`, `REALM` — match customer setup |

### Execution Order Cheat Sheet

| # | Step | Time | Risk |
|---|------|------|------|
| 1 | Gather env info | 10 min | None |
| 2 | Load images | 15 min | None |
| 3 | Enable features on RH-SSO | 5 min | **Pod restart** |
| 4 | Enable features on RHBK | 5 min | **Pod restart** |
| 5 | Create exchange clients | 10 min | None (new clients only) |
| 6 | TLS trust between IdPs | 15 min | **Pod restarts** on both |
| 7 | Register IdPs | 10 min | None |
| 8 | Fine-grained auth | 20 min | None |
| 9 | **Verify exchange works** | 10 min | None (stop if this fails) |
| 10 | Deploy proxy | 10 min | None (new namespace) |
| 11 | Integrate first app | varies | Config change to System B |
| 12 | Demo app (optional) | 5 min | None |

**Total: ~2 hours** for Steps 1–10, assuming no issues.

---

### Common Gotchas in Offline Environments

1. **DNS resolution inside pods** — make sure CoreDNS is healthy: `oc -n openshift-dns get pods`
2. **Image pull failures** — if using the internal registry, ensure the `sso-gateway` namespace has `system:image-puller` bound
3. **Certificate chain issues** — if the customer uses an internal CA, you may need the entire cert chain (root + intermediates), not just the leaf cert
4. **RH-SSO URL path** — always includes `/auth` (e.g., `/auth/realms/myrealm`). RHBK does **not** (e.g., `/realms/myrealm`)
5. **Admin tokens expire fast** — re-acquire them before each batch of API calls
6. **`scope=openid`** — always include in exchange requests, or chained exchanges will fail with "Missing openid scope"

---

*Refer to `docs/SSO-Migration-Implementation-Guide.md` for the full architecture overview, troubleshooting guide, and production hardening recommendations.*
