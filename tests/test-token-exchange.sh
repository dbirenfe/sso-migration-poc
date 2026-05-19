#!/usr/bin/env bash
##############################################################################
# End-to-end test for the SSO Migration Token Exchange solution.
#
# Tests all flows through the IdP Gateway Proxy:
#   1-2: Direct token acquisition (pass-through)
#   3-4: Explicit token exchange in both directions (pass-through)
#   5-6: IdP Proxy cross-domain exchange (Bearer + 401 → exchange → retry)
#   7-8: Multi-hop chained exchanges
#     9: Full customer migration scenario
#
# All requests go through the proxy routes (external URLs).
#
# Prerequisites:
#   - RH-SSO and RHBK running with bidirectional IdP trust configured
#   - IdP Gateway Proxies deployed (idp-proxy-rhsso, idp-proxy-rhbk)
#   - curl and jq available
#
# Usage:
#   export REALM="myrealm"
#   export RHSSO_HOST="keycloak-rhsso.apps.cluster.domain.com"
#   export RHBK_HOST="rhbk-rhbk.apps.cluster.domain.com"
#   export CLIENT_ID="token-exchange-client"
#   export CLIENT_SECRET="token-exchange-secret-12345"
#   export TEST_USER="testuser"
#   export TEST_PASSWORD="testpass"
#   ./test-token-exchange.sh
##############################################################################
set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_ok()   { echo -e "  ${GREEN}[PASS]${NC} $1"; PASS_COUNT=$((PASS_COUNT+1)); }
log_fail() { echo -e "  ${RED}[FAIL]${NC} $1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
log_info() { echo -e "  ${YELLOW}[INFO]${NC} $1"; }
log_head() { echo -e "\n${CYAN}── $1 ──${NC}"; }

: "${REALM:?Set REALM}"
: "${RHSSO_HOST:?Set RHSSO_HOST}"
: "${RHBK_HOST:?Set RHBK_HOST}"
: "${CLIENT_ID:?Set CLIENT_ID}"
: "${CLIENT_SECRET:?Set CLIENT_SECRET}"
: "${TEST_USER:?Set TEST_USER}"
: "${TEST_PASSWORD:?Set TEST_PASSWORD}"

RHSSO="https://${RHSSO_HOST}"
RHBK="https://${RHBK_HOST}"
RHSSO_TOKEN_URL="${RHSSO}/auth/realms/${REALM}/protocol/openid-connect/token"
RHBK_TOKEN_URL="${RHBK}/realms/${REALM}/protocol/openid-connect/token"
RHSSO_USERINFO="${RHSSO}/auth/realms/${REALM}/protocol/openid-connect/userinfo"
RHBK_USERINFO="${RHBK}/realms/${REALM}/protocol/openid-connect/userinfo"

PASS_COUNT=0
FAIL_COUNT=0

decode_iss() {
  echo "$1" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.iss // "unknown"' 2>/dev/null
}

echo "============================================================"
echo "  SSO Migration Token Exchange — End-to-End Test Suite"
echo "============================================================"
echo "  RH-SSO : ${RHSSO_HOST}"
echo "  RHBK   : ${RHBK_HOST}"
echo "  Realm  : ${REALM}"
echo "  Client : ${CLIENT_ID}"
echo "  User   : ${TEST_USER}"
echo "  All requests go through the IdP Gateway Proxies."
echo "============================================================"

# ═══════════════════════════════════════════════════════════════════
log_head "Test 1: Direct RH-SSO token acquisition (via proxy — pass-through)"
# The POST has no Bearer token, so the proxy forwards it as-is.
# ═══════════════════════════════════════════════════════════════════

RHSSO_RESPONSE=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${TEST_USER}" \
  -d "password=${TEST_PASSWORD}" \
  -d "scope=openid")

RHSSO_TOKEN=$(echo "${RHSSO_RESPONSE}" | jq -r '.access_token // empty')

if [ -n "${RHSSO_TOKEN}" ]; then
  log_ok "Got RH-SSO token (iss: $(decode_iss "${RHSSO_TOKEN}"))"
else
  log_fail "Could not obtain RH-SSO token"
  echo "    Response: $(echo "${RHSSO_RESPONSE}" | jq -c . 2>/dev/null || echo "${RHSSO_RESPONSE}")"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 2: Direct RHBK token acquisition (via proxy — pass-through)"
# ═══════════════════════════════════════════════════════════════════

RHBK_RESPONSE=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${TEST_USER}" \
  -d "password=${TEST_PASSWORD}" \
  -d "scope=openid")

RHBK_TOKEN=$(echo "${RHBK_RESPONSE}" | jq -r '.access_token // empty')

if [ -n "${RHBK_TOKEN}" ]; then
  log_ok "Got RHBK token (iss: $(decode_iss "${RHBK_TOKEN}"))"
else
  log_fail "Could not obtain RHBK token"
  echo "    Response: $(echo "${RHBK_RESPONSE}" | jq -c . 2>/dev/null || echo "${RHBK_RESPONSE}")"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 3: Exchange RHBK token → RH-SSO (via proxy — pass-through)"
# The exchange POST has no Bearer token (foreign token is in form body),
# so the proxy forwards it as-is to RH-SSO.
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHBK_TOKEN}" ]; then
  EX_RESP_3=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
    -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
    -d "subject_token=${RHBK_TOKEN}" \
    -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
    -d "subject_issuer=rhbk" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "scope=openid")

  TOKEN_3=$(echo "${EX_RESP_3}" | jq -r '.access_token // empty')

  if [ -n "${TOKEN_3}" ]; then
    log_ok "RHBK→RH-SSO exchange succeeded (iss: $(decode_iss "${TOKEN_3}"))"
  else
    log_fail "RHBK→RH-SSO exchange failed"
    echo "    Response: $(echo "${EX_RESP_3}" | jq -c . 2>/dev/null || echo "${EX_RESP_3}")"
  fi
else
  log_fail "Skipped — no RHBK token from Test 2"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 4: Exchange RH-SSO token → RHBK (via proxy — pass-through)"
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHSSO_TOKEN}" ]; then
  EX_RESP_4=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
    -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
    -d "subject_token=${RHSSO_TOKEN}" \
    -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
    -d "subject_issuer=rhsso" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "scope=openid")

  TOKEN_4=$(echo "${EX_RESP_4}" | jq -r '.access_token // empty')

  if [ -n "${TOKEN_4}" ]; then
    log_ok "RH-SSO→RHBK exchange succeeded (iss: $(decode_iss "${TOKEN_4}"))"
  else
    log_fail "RH-SSO→RHBK exchange failed"
    echo "    Response: $(echo "${EX_RESP_4}" | jq -c . 2>/dev/null || echo "${EX_RESP_4}")"
  fi
else
  log_fail "Skipped — no RH-SSO token from Test 1"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 5: IdP Proxy — send RH-SSO token to RHBK userinfo"
# The Bearer token is from RH-SSO. The proxy forwards it to RHBK,
# RHBK returns 401, the proxy exchanges and retries → should get 200.
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHSSO_TOKEN}" ]; then
  HTTP_5=$(curl -sk -o /tmp/test5_body.json -w "%{http_code}" \
    -H "Authorization: Bearer ${RHSSO_TOKEN}" \
    "${RHBK_USERINFO}")

  if [ "${HTTP_5}" = "200" ]; then
    USER_5=$(jq -r '.preferred_username // "?"' /tmp/test5_body.json 2>/dev/null)
    log_ok "RH-SSO token accepted by RHBK via proxy (user: ${USER_5})"
  else
    log_fail "RHBK rejected the RH-SSO token even through proxy (HTTP ${HTTP_5})"
    echo "    Body: $(cat /tmp/test5_body.json 2>/dev/null)"
  fi
else
  log_fail "Skipped — no RH-SSO token from Test 1"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 6: IdP Proxy — send RHBK token to RH-SSO userinfo"
# Reverse direction: Bearer from RHBK → RH-SSO proxy → exchange → retry.
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHBK_TOKEN}" ]; then
  HTTP_6=$(curl -sk -o /tmp/test6_body.json -w "%{http_code}" \
    -H "Authorization: Bearer ${RHBK_TOKEN}" \
    "${RHSSO_USERINFO}")

  if [ "${HTTP_6}" = "200" ]; then
    USER_6=$(jq -r '.preferred_username // "?"' /tmp/test6_body.json 2>/dev/null)
    log_ok "RHBK token accepted by RH-SSO via proxy (user: ${USER_6})"
  else
    log_fail "RH-SSO rejected the RHBK token even through proxy (HTTP ${HTTP_6})"
    echo "    Body: $(cat /tmp/test6_body.json 2>/dev/null)"
  fi
else
  log_fail "Skipped — no RHBK token from Test 2"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 7: Multi-hop — RH-SSO → RHBK → RH-SSO (chained exchange)"
# Get RH-SSO token, exchange at RHBK, then exchange back at RH-SSO.
# All requests go through the proxy routes.
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHSSO_TOKEN}" ]; then
  # Hop 1: RH-SSO → RHBK
  HOP1_RESP=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
    -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
    -d "subject_token=${RHSSO_TOKEN}" \
    -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
    -d "subject_issuer=rhsso" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "scope=openid")

  HOP1_TOKEN=$(echo "${HOP1_RESP}" | jq -r '.access_token // empty')

  if [ -n "${HOP1_TOKEN}" ]; then
    log_info "Hop 1 OK: RH-SSO → RHBK (iss: $(decode_iss "${HOP1_TOKEN}"))"

    # Hop 2: RHBK → RH-SSO
    HOP2_RESP=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
      -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
      -d "subject_token=${HOP1_TOKEN}" \
      -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
      -d "subject_issuer=rhbk" \
      -d "client_id=${CLIENT_ID}" \
      -d "client_secret=${CLIENT_SECRET}" \
      -d "scope=openid")

    HOP2_TOKEN=$(echo "${HOP2_RESP}" | jq -r '.access_token // empty')

    if [ -n "${HOP2_TOKEN}" ]; then
      log_ok "Multi-hop RH-SSO→RHBK→RH-SSO succeeded (final iss: $(decode_iss "${HOP2_TOKEN}"))"
    else
      log_fail "Hop 2 failed: RHBK→RH-SSO"
      echo "    Response: $(echo "${HOP2_RESP}" | jq -c . 2>/dev/null)"
    fi
  else
    log_fail "Hop 1 failed: RH-SSO→RHBK"
    echo "    Response: $(echo "${HOP1_RESP}" | jq -c . 2>/dev/null)"
  fi
else
  log_fail "Skipped — no RH-SSO token from Test 1"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 8: Multi-hop — RHBK → RH-SSO → RHBK (chained exchange)"
# Reverse chain: RHBK token → exchange at RH-SSO → exchange back at RHBK.
# ═══════════════════════════════════════════════════════════════════

if [ -n "${RHBK_TOKEN}" ]; then
  # Hop 1: RHBK → RH-SSO
  HOP1B_RESP=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
    -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
    -d "subject_token=${RHBK_TOKEN}" \
    -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
    -d "subject_issuer=rhbk" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "scope=openid")

  HOP1B_TOKEN=$(echo "${HOP1B_RESP}" | jq -r '.access_token // empty')

  if [ -n "${HOP1B_TOKEN}" ]; then
    log_info "Hop 1 OK: RHBK → RH-SSO (iss: $(decode_iss "${HOP1B_TOKEN}"))"

    # Hop 2: RH-SSO → RHBK
    HOP2B_RESP=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
      -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
      -d "subject_token=${HOP1B_TOKEN}" \
      -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
      -d "subject_issuer=rhsso" \
      -d "client_id=${CLIENT_ID}" \
      -d "client_secret=${CLIENT_SECRET}" \
      -d "scope=openid")

    HOP2B_TOKEN=$(echo "${HOP2B_RESP}" | jq -r '.access_token // empty')

    if [ -n "${HOP2B_TOKEN}" ]; then
      log_ok "Multi-hop RHBK→RH-SSO→RHBK succeeded (final iss: $(decode_iss "${HOP2B_TOKEN}"))"
    else
      log_fail "Hop 2 failed: RH-SSO→RHBK"
      echo "    Response: $(echo "${HOP2B_RESP}" | jq -c . 2>/dev/null)"
    fi
  else
    log_fail "Hop 1 failed: RHBK→RH-SSO"
    echo "    Response: $(echo "${HOP1B_RESP}" | jq -c . 2>/dev/null)"
  fi
else
  log_fail "Skipped — no RHBK token from Test 2"
fi

# ═══════════════════════════════════════════════════════════════════
log_head "Test 9: Full customer scenario"
# System B gets an RH-SSO token, then calls RHBK userinfo through
# the proxy. The proxy forwards → RHBK rejects (401) → proxy
# exchanges the RH-SSO token for RHBK token → retries → 200.
# ═══════════════════════════════════════════════════════════════════

log_info "System B acquires token from RH-SSO (via proxy — pass-through)..."
SYS_B_RESP=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${TEST_USER}" \
  -d "password=${TEST_PASSWORD}" \
  -d "scope=openid")

SYS_B_TOKEN=$(echo "${SYS_B_RESP}" | jq -r '.access_token // empty')

if [ -n "${SYS_B_TOKEN}" ]; then
  log_info "System B got RH-SSO token. Now calling RHBK userinfo through proxy..."

  HTTP_9=$(curl -sk -o /tmp/test9_body.json -w "%{http_code}" \
    -H "Authorization: Bearer ${SYS_B_TOKEN}" \
    "${RHBK_USERINFO}")

  if [ "${HTTP_9}" = "200" ]; then
    USER_9=$(jq -r '.preferred_username // "?"' /tmp/test9_body.json 2>/dev/null)
    log_ok "Full customer scenario: System B's RH-SSO token was transparently exchanged by the proxy. RHBK returned userinfo for '${USER_9}'."
  else
    log_fail "Full customer scenario failed (HTTP ${HTTP_9})"
    echo "    Body: $(cat /tmp/test9_body.json 2>/dev/null)"
  fi
else
  log_fail "System B could not get token from RH-SSO"
fi

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo ""
echo "============================================================"
echo "  Results: ${PASS_COUNT}/${TOTAL} passed, ${FAIL_COUNT} failed"
echo "============================================================"

if [ "${FAIL_COUNT}" -gt 0 ]; then
  exit 1
fi
