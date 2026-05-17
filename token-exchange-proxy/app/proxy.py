"""
Transparent Token Exchange Reverse Proxy

Sits in front of a protected service and transparently exchanges
cross-domain JWT tokens so that the backend always receives a token
from its *own* identity provider.  This enables zero application code
changes during the RH-SSO -> RHBK migration.

Both directions use the standard Token Exchange grant (RFC 8693):
    grant_type = urn:ietf:params:oauth:grant-type:token-exchange

Deploy one instance in front of every service that may receive tokens
from the *other* identity domain.  Configure via environment variables
(see below) or the Kubernetes ConfigMap.
"""

import os
import json
import time
import hashlib
import base64
import logging
import threading
from urllib.parse import urljoin

from flask import Flask, request, Response
import requests as http_client

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("token-exchange-proxy")

# ─── Configuration ────────────────────────────────────────────────────
TARGET_URL         = os.environ["TARGET_URL"]
EXPECTED_ISSUER    = os.environ["EXPECTED_ISSUER"]
TOKEN_ENDPOINT     = os.environ["TOKEN_ENDPOINT"]
GRANT_TYPE         = os.environ.get("GRANT_TYPE", "token-exchange")   # "token-exchange" | "jwt-bearer"
CLIENT_ID          = os.environ["EXCHANGE_CLIENT_ID"]
CLIENT_SECRET      = os.environ["EXCHANGE_CLIENT_SECRET"]
IDP_ALIAS          = os.environ.get("IDP_ALIAS", "")                  # required for token-exchange grant
CACHE_TTL_BUFFER   = int(os.environ.get("CACHE_TTL_BUFFER_SEC", "30"))
VERIFY_UPSTREAM_TLS = os.environ.get("VERIFY_UPSTREAM_TLS", "true").lower() == "true"
LISTEN_PORT        = int(os.environ.get("LISTEN_PORT", "8080"))

# ─── In-memory token cache ────────────────────────────────────────────
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _decode_jwt_claims(token: str) -> dict | None:
    """Decode JWT payload WITHOUT cryptographic verification.

    We only need the `iss` and `exp` claims to decide whether an
    exchange is necessary.  Signature validation is the job of the
    downstream identity provider, not this proxy.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _get_cached(token: str) -> str | None:
    key = _cache_key(token)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        new_token, expires_at = entry
        if time.time() >= expires_at:
            del _cache[key]
            return None
        return new_token


def _put_cache(original_token: str, new_token: str, expires_in: int):
    key = _cache_key(original_token)
    expires_at = time.time() + expires_in - CACHE_TTL_BUFFER
    with _cache_lock:
        _cache[key] = (new_token, expires_at)


def _exchange_via_token_exchange(assertion: str) -> dict | None:
    """RH-SSO 7.6 Token Exchange (tech-preview).

    POST /auth/realms/{realm}/protocol/openid-connect/token
      grant_type            = urn:ietf:params:oauth:grant-type:token-exchange
      subject_token         = <foreign JWT>
      subject_token_type    = urn:ietf:params:oauth:token-type:access_token
      subject_issuer        = <IdP alias in Keycloak>
      client_id / client_secret
    """
    data = {
        "grant_type":         "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token":      assertion,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer":     IDP_ALIAS,
        "client_id":          CLIENT_ID,
        "client_secret":      CLIENT_SECRET,
        "scope":              "openid",
    }
    resp = http_client.post(TOKEN_ENDPOINT, data=data, verify=VERIFY_UPSTREAM_TLS)
    if resp.status_code == 200:
        return resp.json()
    log.error("Token Exchange failed (%s): %s", resp.status_code, resp.text)
    return None


def _exchange_via_jwt_bearer(assertion: str) -> dict | None:
    """RHBK 26.4 JWT Authorization Grant (RFC 7523).

    POST /realms/{realm}/protocol/openid-connect/token
      grant_type      = urn:ietf:params:oauth:grant-type:jwt-bearer
      assertion       = <foreign JWT>
      client_id / client_secret
    """
    data = {
        "grant_type":     "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":      assertion,
        "client_id":      CLIENT_ID,
        "client_secret":  CLIENT_SECRET,
    }
    resp = http_client.post(TOKEN_ENDPOINT, data=data, verify=VERIFY_UPSTREAM_TLS)
    if resp.status_code == 200:
        return resp.json()
    log.error("JWT Bearer exchange failed (%s): %s", resp.status_code, resp.text)
    return None


def exchange_token(original_token: str) -> str | None:
    """Return a native token for the target IdP, using cache when possible."""
    cached = _get_cached(original_token)
    if cached:
        log.debug("Cache hit")
        return cached

    if GRANT_TYPE == "jwt-bearer":
        result = _exchange_via_jwt_bearer(original_token)
    else:
        result = _exchange_via_token_exchange(original_token)

    if result is None:
        return None

    new_token = result["access_token"]
    expires_in = result.get("expires_in", 300)
    _put_cache(original_token, new_token, expires_in)
    return new_token


# ─── Reverse-proxy handler ───────────────────────────────────────────

EXCLUDED_RESPONSE_HEADERS = frozenset([
    "content-encoding", "content-length", "transfer-encoding", "connection",
])


@app.route("/healthz")
def healthz():
    return "OK", 200


@app.route("/readyz")
def readyz():
    return "OK", 200


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def proxy_handler(path):
    auth_header = request.headers.get("Authorization", "")
    forwarded_headers = {
        k: v for k, v in request.headers if k.lower() not in ("host",)
    }

    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        claims = _decode_jwt_claims(token)

        if claims and claims.get("iss") != EXPECTED_ISSUER:
            log.info(
                "Issuer mismatch — got '%s', expected '%s'. Exchanging token.",
                claims.get("iss"), EXPECTED_ISSUER,
            )
            new_token = exchange_token(token)
            if new_token:
                forwarded_headers["Authorization"] = f"Bearer {new_token}"
                log.info("Token exchanged successfully")
            else:
                return Response(
                    json.dumps({"error": "token_exchange_failed",
                                "error_description": "Could not exchange cross-domain token"}),
                    status=502,
                    content_type="application/json",
                )

    target = f"{TARGET_URL.rstrip('/')}/{path}"
    qs = request.query_string.decode()
    if qs:
        target += f"?{qs}"

    try:
        upstream_resp = http_client.request(
            method=request.method,
            url=target,
            headers=forwarded_headers,
            data=request.get_data(),
            allow_redirects=False,
            verify=VERIFY_UPSTREAM_TLS,
            stream=True,
            timeout=30,
        )
    except http_client.exceptions.RequestException as exc:
        log.error("Upstream request failed: %s", exc)
        return Response(
            json.dumps({"error": "upstream_error", "error_description": str(exc)}),
            status=502,
            content_type="application/json",
        )

    response_headers = [
        (k, v) for k, v in upstream_resp.headers.items()
        if k.lower() not in EXCLUDED_RESPONSE_HEADERS
    ]
    return Response(upstream_resp.content, upstream_resp.status_code, response_headers)


if __name__ == "__main__":
    log.info(
        "Starting token-exchange-proxy  target=%s  issuer=%s  grant=%s",
        TARGET_URL, EXPECTED_ISSUER, GRANT_TYPE,
    )
    app.run(host="0.0.0.0", port=LISTEN_PORT)
