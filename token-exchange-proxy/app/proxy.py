"""
Transparent Token Exchange Reverse Proxy — IdP Gateway Mode

Sits in front of an Identity Provider (RH-SSO or RHBK) and transparently
handles cross-domain tokens.  Instead of pre-checking the JWT issuer, it
uses a "try-first, exchange-on-failure" approach:

  1. Forward the request to the IdP as-is.
  2. If the IdP rejects it (HTTP 401/403) AND the request carried a
     Bearer token, attempt a token exchange and retry.
  3. If the retry also fails, the token is genuinely invalid — return
     the error to the caller.

This design removes the need to know which app uses which client or IdP.
Deploy one instance in front of each IdP.
"""

import os
import json
import time
import hashlib
import base64
import logging
import threading

from flask import Flask, request, Response
import requests as http_client

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("token-exchange-proxy")

# ─── Configuration ────────────────────────────────────────────────────
TARGET_URL          = os.environ["TARGET_URL"]
TOKEN_ENDPOINT      = os.environ["TOKEN_ENDPOINT"]
GRANT_TYPE          = os.environ.get("GRANT_TYPE", "token-exchange")
CLIENT_ID           = os.environ["EXCHANGE_CLIENT_ID"]
CLIENT_SECRET       = os.environ["EXCHANGE_CLIENT_SECRET"]
IDP_ALIAS           = os.environ.get("IDP_ALIAS", "")
CACHE_TTL_BUFFER    = int(os.environ.get("CACHE_TTL_BUFFER_SEC", "30"))
VERIFY_UPSTREAM_TLS = os.environ.get("VERIFY_UPSTREAM_TLS", "true").lower() == "true"
LISTEN_PORT         = int(os.environ.get("LISTEN_PORT", "8080"))
RETRY_STATUS_CODES  = {
    int(c.strip())
    for c in os.environ.get("RETRY_STATUS_CODES", "401,403").split(",")
    if c.strip()
}
REQUEST_TIMEOUT     = int(os.environ.get("REQUEST_TIMEOUT", "30"))
IDP_EXTERNAL_HOST   = os.environ.get("IDP_EXTERNAL_HOST", "")
PASSTHROUGH_PREFIXES = tuple(
    p.strip() for p in os.environ.get(
        "PASSTHROUGH_PREFIXES", "/admin,/js,/resources,/robots.txt"
    ).split(",") if p.strip()
)

# ─── In-memory token cache ────────────────────────────────────────────
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _decode_jwt_claims(token: str) -> dict | None:
    """Decode JWT payload WITHOUT cryptographic verification."""
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


# ─── Token exchange implementations ──────────────────────────────────

def _exchange_headers() -> dict:
    """Build headers for exchange calls, including Host to ensure correct issuer."""
    headers = {}
    if IDP_EXTERNAL_HOST:
        headers["Host"] = IDP_EXTERNAL_HOST
    return headers


def _exchange_via_token_exchange(assertion: str) -> dict | None:
    data = {
        "grant_type":         "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token":      assertion,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer":     IDP_ALIAS,
        "client_id":          CLIENT_ID,
        "client_secret":      CLIENT_SECRET,
        "scope":              "openid",
    }
    resp = http_client.post(TOKEN_ENDPOINT, data=data, headers=_exchange_headers(),
                            verify=VERIFY_UPSTREAM_TLS, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        return resp.json()
    log.error("Token Exchange failed (%s): %s", resp.status_code, resp.text)
    return None


def _exchange_via_jwt_bearer(assertion: str) -> dict | None:
    data = {
        "grant_type":     "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":      assertion,
        "client_id":      CLIENT_ID,
        "client_secret":  CLIENT_SECRET,
    }
    resp = http_client.post(TOKEN_ENDPOINT, data=data, headers=_exchange_headers(),
                            verify=VERIFY_UPSTREAM_TLS, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        return resp.json()
    log.error("JWT Bearer exchange failed (%s): %s", resp.status_code, resp.text)
    return None


def exchange_token(original_token: str) -> str | None:
    """Return a native token for the target IdP, using cache when possible."""
    cached = _get_cached(original_token)
    if cached:
        log.debug("Cache hit — returning previously exchanged token")
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


def _build_target_url(path: str) -> str:
    target = f"{TARGET_URL.rstrip('/')}/{path}"
    qs = request.query_string.decode()
    if qs:
        target += f"?{qs}"
    return target


def _forward(target: str, headers: dict, data: bytes):
    """Send the request to the upstream IdP and return the response."""
    return http_client.request(
        method=request.method,
        url=target,
        headers=headers,
        data=data,
        allow_redirects=False,
        verify=VERIFY_UPSTREAM_TLS,
        stream=True,
        timeout=REQUEST_TIMEOUT,
    )


def _make_response(upstream_resp) -> Response:
    """Convert an upstream response into a Flask Response.

    Uses urllib3's raw headers to preserve duplicate headers (critically,
    multiple Set-Cookie headers that the requests library would merge).
    """
    resp_headers = [
        (k, v) for k, v in upstream_resp.raw.headers.items()
        if k.lower() not in EXCLUDED_RESPONSE_HEADERS
    ]
    return Response(upstream_resp.content, upstream_resp.status_code, resp_headers)


@app.route("/healthz")
def healthz():
    return "OK", 200


@app.route("/readyz")
def readyz():
    return "OK", 200


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def proxy_handler(path):
    request_path = f"/{path}"
    body = request.get_data()
    target = _build_target_url(path)

    # ── Build forwarded headers: keep all original headers as-is ────
    auth_header = request.headers.get("Authorization", "")
    forwarded_headers = dict(request.headers)

    has_bearer = auth_header.lower().startswith("bearer ")
    original_token = auth_header[7:] if has_bearer else None

    # ── Step 1: forward the request as-is ────────────────────────────
    try:
        upstream_resp = _forward(target, forwarded_headers, body)
    except http_client.exceptions.RequestException as exc:
        log.error("Upstream request failed: %s", exc)
        return Response(
            json.dumps({"error": "upstream_error", "error_description": str(exc)}),
            status=502,
            content_type="application/json",
        )

    # ── Step 2: if the IdP rejected it and we have a Bearer token,
    #            try exchanging the token and retrying ─────────────────
    if upstream_resp.status_code in RETRY_STATUS_CODES and has_bearer:
        claims = _decode_jwt_claims(original_token)
        log.info(
            "IdP returned %s for token with iss='%s'. Attempting token exchange.",
            upstream_resp.status_code,
            claims.get("iss", "unknown") if claims else "non-jwt",
        )

        new_token = exchange_token(original_token)
        if new_token:
            log.info("Token exchanged successfully — retrying request.")
            retry_headers = dict(forwarded_headers)
            retry_headers["Authorization"] = f"Bearer {new_token}"

            try:
                retry_resp = _forward(target, retry_headers, body)
            except http_client.exceptions.RequestException as exc:
                log.error("Retry request failed: %s", exc)
                return _make_response(upstream_resp)

            return _make_response(retry_resp)

        log.warning(
            "Token exchange failed — returning original %s response. "
            "Token is likely genuinely invalid.",
            upstream_resp.status_code,
        )

    return _make_response(upstream_resp)


if __name__ == "__main__":
    log.info(
        "Starting token-exchange-proxy (IdP gateway mode)  "
        "target=%s  grant=%s  retry_on=%s",
        TARGET_URL, GRANT_TYPE, RETRY_STATUS_CODES,
    )
    app.run(host="0.0.0.0", port=LISTEN_PORT)
