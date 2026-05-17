#!/usr/bin/env bash
##############################################################################
# End-to-end test for the Token Exchange flow.
#
# Prerequisites:
#   - Both RH-SSO and RHBK are running and configured per SETUP.md
#   - The API Gateway is deployed and DNS is configured
#   - curl and jq are available
#
# Usage:
#   export REALM="myrealm"
#   export RHSSO_HOST="rhsso.apps.cluster.domain.com"
#   export RHBK_HOST="rhbk.apps.cluster.domain.com"
#   export CLIENT_ID="token-exchange-client"
#   export CLIENT_SECRET="your-secret"
#   export TEST_USER="testuser"
#   export TEST_PASSWORD="testpassword"
#   ./test-token-exchange.sh
##############################################################################
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[PASS]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

: "${REALM:?Set REALM}"
: "${RHSSO_HOST:?Set RHSSO_HOST}"
: "${RHBK_HOST:?Set RHBK_HOST}"
: "${CLIENT_ID:?Set CLIENT_ID}"
: "${CLIENT_SECRET:?Set CLIENT_SECRET}"
: "${TEST_USER:?Set TEST_USER}"
: "${TEST_PASSWORD:?Set TEST_PASSWORD}"

RHSSO_TOKEN_URL="https://${RHSSO_HOST}/auth/realms/${REALM}/protocol/openid-connect/token"
RHBK_TOKEN_URL="https://${RHBK_HOST}/realms/${REALM}/protocol/openid-connect/token"

echo "============================================"
echo "  SSO Migration Token Exchange — E2E Test"
echo "============================================"
echo ""

# ─── Test 1: Obtain a token from RHBK ────────────────────────────────
log_info "Test 1: Obtaining a token from RHBK..."

RHBK_RESPONSE=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${TEST_USER}" \
  -d "password=${TEST_PASSWORD}" \
  -d "scope=openid")

RHBK_TOKEN=$(echo "${RHBK_RESPONSE}" | jq -r '.access_token // empty')

if [ -z "${RHBK_TOKEN}" ]; then
  log_fail "Could not obtain token from RHBK"
  echo "  Response: ${RHBK_RESPONSE}"
  exit 1
fi
log_ok "Got RHBK token"

RHBK_ISS=$(echo "${RHBK_TOKEN}" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.iss // empty')
log_info "  Issuer: ${RHBK_ISS}"

# ─── Test 2: Exchange RHBK token at RH-SSO (Token Exchange) ─────────
log_info "Test 2: Exchanging RHBK token at RH-SSO (Token Exchange grant)..."

EXCHANGE_RESPONSE=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${RHBK_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "subject_issuer=rhbk" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}")

RHSSO_TOKEN=$(echo "${EXCHANGE_RESPONSE}" | jq -r '.access_token // empty')

if [ -z "${RHSSO_TOKEN}" ]; then
  log_fail "Token Exchange at RH-SSO failed"
  echo "  Response: ${EXCHANGE_RESPONSE}"
else
  RHSSO_ISS=$(echo "${RHSSO_TOKEN}" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.iss // empty')
  log_ok "Got RH-SSO token via Token Exchange"
  log_info "  Issuer: ${RHSSO_ISS}"
fi

# ─── Test 3: Obtain a token from RH-SSO ─────────────────────────────
log_info "Test 3: Obtaining a token from RH-SSO..."

RHSSO_DIRECT_RESPONSE=$(curl -sk -X POST "${RHSSO_TOKEN_URL}" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${TEST_USER}" \
  -d "password=${TEST_PASSWORD}" \
  -d "scope=openid")

RHSSO_DIRECT_TOKEN=$(echo "${RHSSO_DIRECT_RESPONSE}" | jq -r '.access_token // empty')

if [ -z "${RHSSO_DIRECT_TOKEN}" ]; then
  log_fail "Could not obtain token from RH-SSO"
  echo "  Response: ${RHSSO_DIRECT_RESPONSE}"
  exit 1
fi
log_ok "Got RH-SSO token"

# ─── Test 4: Exchange RH-SSO token at RHBK (JWT Authorization Grant) ─
log_info "Test 4: Exchanging RH-SSO token at RHBK (JWT Authorization Grant)..."

JWT_BEARER_RESPONSE=$(curl -sk -X POST "${RHBK_TOKEN_URL}" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  -d "assertion=${RHSSO_DIRECT_TOKEN}" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}")

RHBK_EXCHANGED=$(echo "${JWT_BEARER_RESPONSE}" | jq -r '.access_token // empty')

if [ -z "${RHBK_EXCHANGED}" ]; then
  log_fail "JWT Authorization Grant at RHBK failed"
  echo "  Response: ${JWT_BEARER_RESPONSE}"
else
  RHBK_EX_ISS=$(echo "${RHBK_EXCHANGED}" | cut -d. -f2 | base64 -d 2>/dev/null | jq -r '.iss // empty')
  log_ok "Got RHBK token via JWT Authorization Grant"
  log_info "  Issuer: ${RHBK_EX_ISS}"
fi

# ─── Summary ─────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Test Summary"
echo "============================================"
echo "  RHBK token obtained:           $([ -n "${RHBK_TOKEN}" ] && echo 'YES' || echo 'NO')"
echo "  RHBK→RH-SSO exchange:          $([ -n "${RHSSO_TOKEN}" ] && echo 'YES' || echo 'NO')"
echo "  RH-SSO token obtained:         $([ -n "${RHSSO_DIRECT_TOKEN}" ] && echo 'YES' || echo 'NO')"
echo "  RH-SSO→RHBK exchange:          $([ -n "${RHBK_EXCHANGED}" ] && echo 'YES' || echo 'NO')"
echo "============================================"
