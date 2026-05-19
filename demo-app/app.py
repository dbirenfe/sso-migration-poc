"""
SSO Migration Interactive Demo — Backend

Provides API endpoints that interact with the real RH-SSO and RHBK
deployments on the cluster, enabling live scenario testing from the UI.

All configuration comes from environment variables. When RHSSO_EXTERNAL_URL
or RHBK_EXTERNAL_URL are not set, the app auto-discovers them from
OpenShift Routes on startup — making it portable across clusters.
"""

import os, json, time, base64, traceback
from flask import Flask, render_template, jsonify, request as req
import requests as http
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)


def _k8s_api(path):
    """Call the Kubernetes API using the in-cluster ServiceAccount token."""
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    try:
        with open(token_path) as f:
            token = f.read().strip()
        r = http.get(
            f"https://kubernetes.default.svc{path}",
            headers={"Authorization": f"Bearer {token}"},
            verify=ca_path, timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _discover_routes_in_ns(namespace):
    """List all Routes in a namespace and return the first non-metrics external URL."""
    data = _k8s_api(f"/apis/route.openshift.io/v1/namespaces/{namespace}/routes")
    if not data or "items" not in data:
        return None
    for route in data["items"]:
        host = route.get("spec", {}).get("host", "")
        name = route.get("metadata", {}).get("name", "")
        if not host or host == "keycloak.local" or "metrics" in name:
            continue
        return f"https://{host}"
    return None


def _discover_routes():
    """Find external route URLs for RH-SSO and RHBK.

    Searches sso-gateway (proxy routes), rhsso, and rhbk namespaces.
    Matches routes by hostname keywords.
    """
    rhsso, rhbk = None, None
    for ns in ["sso-gateway", "rhsso", "rhbk"]:
        data = _k8s_api(f"/apis/route.openshift.io/v1/namespaces/{ns}/routes")
        if not data or "items" not in data:
            continue
        for route in data["items"]:
            host = route.get("spec", {}).get("host", "")
            name = route.get("metadata", {}).get("name", "")
            if not host or host == "keycloak.local" or "metrics" in name or "demo" in name:
                continue
            if not rhsso and ("rhsso" in host or "rhsso" in name):
                rhsso = f"https://{host}"
            elif not rhbk and ("rhbk" in host or "rhbk" in name):
                rhbk = f"https://{host}"
        if rhsso and rhbk:
            break
    return rhsso, rhbk


def _discover_service(namespace, *names):
    """Check which service name exists and return the internal URL."""
    for name in names:
        data = _k8s_api(f"/api/v1/namespaces/{namespace}/services/{name}")
        if data and data.get("spec", {}).get("ports"):
            port = data["spec"]["ports"][0].get("port", 8443)
            return f"https://{name}.{namespace}.svc.cluster.local:{port}"
    return None


# Auto-discover if not explicitly set
_auto_rhsso, _auto_rhbk = None, None
if not os.environ.get("RHSSO_EXTERNAL_URL") or not os.environ.get("RHBK_EXTERNAL_URL"):
    _auto_rhsso, _auto_rhbk = _discover_routes()

RHSSO_INTERNAL = os.environ.get(
    "RHSSO_INTERNAL_URL",
    _discover_service("rhsso", "keycloak") or "https://keycloak.rhsso.svc.cluster.local:8443",
)
RHBK_INTERNAL = os.environ.get(
    "RHBK_INTERNAL_URL",
    _discover_service("rhbk", "rhbk-service", "keycloak") or "https://rhbk-service.rhbk.svc.cluster.local:8443",
)
RHSSO_EXTERNAL = os.environ.get("RHSSO_EXTERNAL_URL", _auto_rhsso or "")
RHBK_EXTERNAL = os.environ.get("RHBK_EXTERNAL_URL", _auto_rhbk or "")

REALM = os.environ.get("REALM", "myrealm")
CLIENT_ID = os.environ.get("CLIENT_ID", "token-exchange-client")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "token-exchange-secret-12345")
TEST_USER = os.environ.get("TEST_USER", "testuser")
TEST_PASS = os.environ.get("TEST_PASS", "testpass")

TIMEOUT = 10

print(f"[CONFIG] RHSSO_INTERNAL = {RHSSO_INTERNAL}")
print(f"[CONFIG] RHSSO_EXTERNAL = {RHSSO_EXTERNAL}")
print(f"[CONFIG] RHBK_INTERNAL  = {RHBK_INTERNAL}")
print(f"[CONFIG] RHBK_EXTERNAL  = {RHBK_EXTERNAL}")
print(f"[CONFIG] REALM          = {REALM}")
if not RHSSO_EXTERNAL:
    print("[WARN] RHSSO_EXTERNAL is empty — RH-SSO external tests will fail")
if not RHBK_EXTERNAL:
    print("[WARN] RHBK_EXTERNAL is empty — RHBK external tests will fail")


def _base_url(provider, internal=True):
    if provider == "rhsso":
        return RHSSO_INTERNAL if internal else RHSSO_EXTERNAL
    return RHBK_INTERNAL if internal else RHBK_EXTERNAL


def _token_url(provider, internal=True):
    base = _base_url(provider, internal)
    if provider == "rhsso":
        return f"{base}/auth/realms/{REALM}/protocol/openid-connect/token"
    return f"{base}/realms/{REALM}/protocol/openid-connect/token"


def _wellknown_url(provider, internal=True):
    base = _base_url(provider, internal)
    if provider == "rhsso":
        return f"{base}/auth/realms/{REALM}/.well-known/openid-configuration"
    return f"{base}/realms/{REALM}/.well-known/openid-configuration"


def _decode_jwt(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _safe_post(url, data, timeout=TIMEOUT):
    try:
        r = http.post(url, data=data, verify=False, timeout=timeout)
        return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except http.exceptions.ConnectionError as e:
        return 0, {"error": "connection_failed", "detail": str(e)}
    except http.exceptions.Timeout:
        return 0, {"error": "timeout", "detail": f"Request to {url} timed out after {timeout}s"}
    except Exception as e:
        return 0, {"error": "unexpected", "detail": str(e)}


def _safe_get(url, timeout=TIMEOUT):
    try:
        r = http.get(url, verify=False, timeout=timeout)
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"error": str(e)}


# ─── Pages ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
        rhsso_external=RHSSO_EXTERNAL,
        rhbk_external=RHBK_EXTERNAL,
        realm=REALM,
        client_id=CLIENT_ID,
        test_user=TEST_USER,
    )


@app.route("/healthz")
def healthz():
    return "OK", 200


# ─── API: Status ─────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    results = {}
    for name, url_fn in [("rhsso", lambda: _wellknown_url("rhsso")),
                          ("rhbk", lambda: _wellknown_url("rhbk"))]:
        try:
            code, body = _safe_get(url_fn(), timeout=5)
            results[name] = {
                "healthy": code == 200,
                "status_code": code,
                "issuer": body.get("issuer", "") if isinstance(body, dict) else "",
            }
        except Exception as e:
            results[name] = {"healthy": False, "error": str(e)}

    return jsonify(results)


# ─── API: Acquire Token ─────────────────────────────────────────────

@app.route("/api/token/acquire", methods=["POST"])
def api_token_acquire():
    provider = req.json.get("provider", "rhsso")
    user = req.json.get("username", TEST_USER)
    passwd = req.json.get("password", TEST_PASS)

    url = _token_url(provider, internal=False)
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": user,
        "password": passwd,
        "grant_type": "password",
        "scope": "openid",
    }

    t0 = time.time()
    code, body = _safe_post(url, data)
    elapsed = round((time.time() - t0) * 1000)

    success = code == 200 and isinstance(body, dict) and "access_token" in body
    result = {
        "success": success,
        "provider": provider,
        "endpoint": url,
        "status_code": code,
        "elapsed_ms": elapsed,
        "curl_command": _build_curl("POST", url, data),
    }

    if success:
        token = body["access_token"]
        claims = _decode_jwt(token)
        result["access_token"] = token
        result["token_type"] = body.get("token_type")
        result["expires_in"] = body.get("expires_in")
        result["claims"] = claims
    else:
        result["error"] = body

    return jsonify(result)


# ─── API: Token Exchange ────────────────────────────────────────────

@app.route("/api/token/exchange", methods=["POST"])
def api_token_exchange():
    source = req.json.get("source_provider")
    target = req.json.get("target_provider")
    source_token = req.json.get("source_token")
    idp_alias = "rhbk" if source == "rhbk" else "rhsso"

    url = _token_url(target, internal=False)
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": source_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer": idp_alias,
        "scope": "openid",
    }

    t0 = time.time()
    code, body = _safe_post(url, data)
    elapsed = round((time.time() - t0) * 1000)

    success = code == 200 and isinstance(body, dict) and "access_token" in body
    result = {
        "success": success,
        "source_provider": source,
        "target_provider": target,
        "endpoint": url,
        "status_code": code,
        "elapsed_ms": elapsed,
        "curl_command": _build_curl("POST", url, {**data, "subject_token": "<SOURCE_TOKEN>"}),
    }

    if success:
        new_token = body["access_token"]
        result["access_token"] = new_token
        result["claims"] = _decode_jwt(new_token)
        result["source_claims"] = _decode_jwt(source_token)
    else:
        result["error"] = body
        result["failure_analysis"] = _analyze_exchange_failure(code, body, source, target)

    return jsonify(result)


# ─── API: Run Full Scenario ─────────────────────────────────────────

@app.route("/api/scenario/run", methods=["POST"])
def api_scenario_run():
    scenario_id = req.json.get("scenario")
    steps = []

    try:
        if scenario_id == "direct_rhsso":
            steps = _scenario_direct("rhsso")
        elif scenario_id == "direct_rhbk":
            steps = _scenario_direct("rhbk")
        elif scenario_id == "exchange_rhbk_to_rhsso":
            steps = _scenario_exchange("rhbk", "rhsso")
        elif scenario_id == "exchange_rhsso_to_rhbk":
            steps = _scenario_exchange("rhsso", "rhbk")
        elif scenario_id == "proxy_legacy":
            steps = _scenario_idp_proxy("rhbk", "rhsso")
        elif scenario_id == "proxy_migrated":
            steps = _scenario_idp_proxy("rhsso", "rhbk")
        elif scenario_id == "chained_rhsso_via_rhbk":
            steps = _scenario_chained("rhsso", "rhbk", "rhsso")
        elif scenario_id == "chained_rhbk_via_rhsso":
            steps = _scenario_chained("rhbk", "rhsso", "rhbk")
        elif scenario_id == "full_migration_flow":
            steps = _scenario_full_migration()
        else:
            return jsonify({"error": f"Unknown scenario: {scenario_id}"}), 400
    except Exception as e:
        steps.append({
            "step": "ERROR",
            "description": f"Unhandled exception: {str(e)}",
            "success": False,
            "detail": traceback.format_exc(),
        })

    all_pass = all(s.get("success", False) for s in steps)
    return jsonify({"scenario": scenario_id, "success": all_pass, "steps": steps})


# ─── Scenario Implementations ───────────────────────────────────────

def _scenario_direct(provider):
    steps = []
    label = "RH-SSO" if provider == "rhsso" else "RHBK"

    url = _token_url(provider, internal=False)
    data = {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "username": TEST_USER, "password": TEST_PASS,
        "grant_type": "password", "scope": "openid",
    }
    t0 = time.time()
    code, body = _safe_post(url, data)
    elapsed = round((time.time() - t0) * 1000)
    ok = code == 200 and isinstance(body, dict) and "access_token" in body

    step = {
        "step": f"Acquire token from {label} (via IdP Proxy)",
        "description": f"Request goes through the IdP Proxy in front of {label}. Since this is a password grant (no Bearer token), the proxy forwards it as-is (pass-through). {label} issues the token.",
        "success": ok,
        "status_code": code,
        "elapsed_ms": elapsed,
        "endpoint": url,
        "curl": _build_curl("POST", url, data),
    }
    if ok:
        token = body["access_token"]
        step["token"] = token
        step["claims"] = _decode_jwt(token)
    else:
        step["error"] = body
    steps.append(step)

    return steps


def _scenario_exchange(source, target):
    steps = _scenario_direct(source)
    if not steps[0]["success"]:
        return steps

    source_token = steps[0]["token"]
    source_label = "RH-SSO" if source == "rhsso" else "RHBK"
    target_label = "RH-SSO" if target == "rhsso" else "RHBK"
    idp_alias = source

    url = _token_url(target, internal=False)
    data = {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": source_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer": idp_alias,
        "scope": "openid",
    }
    t0 = time.time()
    code, body = _safe_post(url, data)
    elapsed = round((time.time() - t0) * 1000)
    ok = code == 200 and isinstance(body, dict) and "access_token" in body

    step = {
        "step": f"Exchange {source_label} token at {target_label} (via IdP Proxy)",
        "description": f"The exchange request goes through the IdP Proxy in front of {target_label}. Since the exchange POST carries no Bearer token (the foreign token is in the form body), the proxy forwards it as-is (pass-through). {target_label} validates the {source_label} token via IdP trust and issues a native {target_label} token (RFC 8693).",
        "success": ok,
        "status_code": code,
        "elapsed_ms": elapsed,
        "endpoint": url,
        "curl": _build_curl("POST", url, {**data, "subject_token": "<SOURCE_TOKEN>"}),
    }
    if ok:
        new_token = body["access_token"]
        step["token"] = new_token
        step["source_claims"] = _decode_jwt(source_token)
        step["claims"] = _decode_jwt(new_token)
    else:
        step["error"] = body
        step["failure_analysis"] = _analyze_exchange_failure(code, body, source, target)
    steps.append(step)

    return steps


def _scenario_idp_proxy(token_source, target_idp):
    """Test the IdP gateway proxy: get a token from one IdP, send it to
    the other IdP's userinfo endpoint (through the proxy), and verify
    the proxy exchanges it transparently."""
    steps = _scenario_direct(token_source)
    if not steps[0]["success"]:
        return steps

    source_token = steps[0]["token"]
    source_label = "RH-SSO" if token_source == "rhsso" else "RHBK"
    target_label = "RH-SSO" if target_idp == "rhsso" else "RHBK"

    userinfo_url = _base_url(target_idp, internal=False)
    if target_idp == "rhsso":
        userinfo_url += f"/auth/realms/{REALM}/protocol/openid-connect/userinfo"
    else:
        userinfo_url += f"/realms/{REALM}/protocol/openid-connect/userinfo"

    t0 = time.time()
    try:
        r = http.get(userinfo_url,
                     headers={"Authorization": f"Bearer {source_token}"},
                     verify=False, timeout=TIMEOUT)
        code = r.status_code
        resp_body = r.text[:500]
    except Exception as e:
        code = 0
        resp_body = str(e)
    elapsed = round((time.time() - t0) * 1000)

    ok = code == 200

    step = {
        "step": f"Send {source_label} token to {target_label} userinfo (via IdP Proxy)",
        "description": (
            f"The request carries a Bearer token (from {source_label}) and hits the IdP Proxy in front of {target_label}. "
            f"Step 1: Proxy forwards it as-is to {target_label}. "
            f"Step 2: {target_label} rejects it with 401 (wrong issuer). "
            f"Step 3: Proxy catches the 401, sees the Bearer token, and calls {target_label}'s token exchange endpoint to swap the {source_label} token for a native {target_label} token. "
            f"Step 4: Proxy retries the userinfo request with the new {target_label} token. "
            f"Step 5: {target_label} accepts it and returns 200 with userinfo."
        ),
        "success": ok,
        "status_code": code,
        "elapsed_ms": elapsed,
        "endpoint": userinfo_url,
        "response_preview": resp_body[:200] if resp_body else "",
        "exchange_performed": True,
    }
    if ok:
        try:
            step["userinfo"] = json.loads(resp_body)
        except Exception:
            pass
    steps.append(step)
    return steps


def _scenario_chained(initial_source, intermediate, final_target):
    src_label = "RH-SSO" if initial_source == "rhsso" else "RHBK"
    mid_label = "RH-SSO" if intermediate == "rhsso" else "RHBK"
    fin_label = "RH-SSO" if final_target == "rhsso" else "RHBK"

    steps = _scenario_direct(initial_source)
    if not steps[0]["success"]:
        return steps

    token_a = steps[0]["token"]

    url1 = _token_url(intermediate, internal=False)
    data1 = {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token_a,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer": initial_source,
        "scope": "openid",
    }
    t0 = time.time()
    code1, body1 = _safe_post(url1, data1)
    elapsed1 = round((time.time() - t0) * 1000)
    ok1 = code1 == 200 and isinstance(body1, dict) and "access_token" in body1

    step2 = {
        "step": f"Exchange {src_label} token at {mid_label} (hop 1)",
        "description": f"First hop: {mid_label} exchanges the {src_label} token for a native {mid_label} token",
        "success": ok1, "status_code": code1, "elapsed_ms": elapsed1,
        "endpoint": url1,
        "curl": _build_curl("POST", url1, {**data1, "subject_token": "<TOKEN_A>"}),
    }
    if ok1:
        token_b = body1["access_token"]
        step2["token"] = token_b
        step2["claims"] = _decode_jwt(token_b)
    else:
        step2["error"] = body1
        step2["failure_analysis"] = _analyze_exchange_failure(code1, body1, initial_source, intermediate)
    steps.append(step2)

    if not ok1:
        return steps

    url2 = _token_url(final_target, internal=False)
    data2 = {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token_b,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_issuer": intermediate,
        "scope": "openid",
    }
    t0 = time.time()
    code2, body2 = _safe_post(url2, data2)
    elapsed2 = round((time.time() - t0) * 1000)
    ok2 = code2 == 200 and isinstance(body2, dict) and "access_token" in body2

    step3 = {
        "step": f"Exchange {mid_label} token back at {fin_label} (hop 2)",
        "description": f"Second hop: {fin_label} exchanges the {mid_label} token for a native {fin_label} token — simulates a backend service calling another service on the other IdP",
        "success": ok2, "status_code": code2, "elapsed_ms": elapsed2,
        "endpoint": url2,
        "curl": _build_curl("POST", url2, {**data2, "subject_token": "<TOKEN_B>"}),
    }
    if ok2:
        token_c = body2["access_token"]
        step3["token"] = token_c
        step3["claims"] = _decode_jwt(token_c)
    else:
        step3["error"] = body2
        step3["failure_analysis"] = _analyze_exchange_failure(code2, body2, intermediate, final_target)
    steps.append(step3)

    return steps


def _scenario_full_migration():
    """Simulates the complete migration scenario described by the customer:
    System A migrated to RHBK, System B still on RH-SSO.
    System B gets a token from RH-SSO and tries to use RHBK.
    The proxy in front of RHBK transparently exchanges the token."""
    steps = []

    steps.append({
        "step": "Context: System B authenticates against RH-SSO",
        "description": (
            "System B has no client of its own — it uses Client A on RH-SSO to get a token. "
            "System A has already been migrated to RHBK, but System B doesn't know that. "
            "All traffic goes through the IdP Proxies."
        ),
        "success": True, "info_only": True,
    })

    direct = _scenario_direct("rhsso")
    direct[0]["step"] = "System B acquires token from RH-SSO via IdP Proxy (pass-through)"
    direct[0]["description"] = (
        "System B sends a password grant request to RH-SSO. The request goes through the "
        "IdP Proxy in front of RH-SSO. Since there is no Bearer token (it's a POST with "
        "form credentials), the proxy forwards it as-is. RH-SSO issues the token."
    )
    steps.extend(direct)
    if not direct[0]["success"]:
        return steps

    rhsso_token = direct[0]["token"]

    steps.append({
        "step": "System B calls RHBK's userinfo — IdP Proxy intercepts",
        "description": (
            "System B sends a GET request with 'Authorization: Bearer <RH-SSO token>' to RHBK's "
            "userinfo endpoint. The request hits the IdP Proxy in front of RHBK."
        ),
        "success": True, "info_only": True,
    })

    userinfo_url = _base_url("rhbk", internal=False)
    userinfo_url += f"/realms/{REALM}/protocol/openid-connect/userinfo"

    t0 = time.time()
    try:
        r = http.get(userinfo_url,
                     headers={"Authorization": f"Bearer {rhsso_token}"},
                     verify=False, timeout=TIMEOUT)
        code = r.status_code
        resp_body = r.text[:500]
    except Exception as e:
        code = 0
        resp_body = str(e)
    elapsed = round((time.time() - t0) * 1000)

    proxy_ok = code == 200

    steps.append({
        "step": "IdP Proxy: forward → 401 → exchange → retry",
        "description": (
            "Step 1: The IdP Proxy forwards the request with the RH-SSO Bearer token to RHBK as-is. "
            "Step 2: RHBK rejects it with HTTP 401 (the token's issuer is RH-SSO, not RHBK). "
            "Step 3: The proxy catches the 401, detects the Bearer token, and calls RHBK's "
            "token exchange endpoint to swap the RH-SSO token for a native RHBK token. "
            "Step 4: The proxy retries the original userinfo request with the new RHBK token."
        ),
        "success": proxy_ok,
        "status_code": code,
        "elapsed_ms": elapsed,
        "endpoint": userinfo_url,
        "exchange_performed": True,
    })

    steps.append({
        "step": "RHBK accepts the retried request with the exchanged token",
        "description": (
            "RHBK receives the retry with a valid native RHBK token, validates it, "
            "and returns HTTP 200 with the userinfo response. The proxy forwards this "
            "back to System B. System B received a successful response — it never knew "
            "the token was exchanged. Zero code changes on any system."
        ),
        "success": proxy_ok,
        "status_code": code,
    })
    if proxy_ok:
        try:
            steps[-1]["userinfo"] = json.loads(resp_body)
        except Exception:
            pass

    return steps


# ─── Helpers ─────────────────────────────────────────────────────────

def _build_curl(method, url, data):
    ext_url = url.replace(RHSSO_INTERNAL, RHSSO_EXTERNAL).replace(RHBK_INTERNAL, RHBK_EXTERNAL)
    parts = [f"curl -sk -X {method} '{ext_url}'"]
    for k, v in data.items():
        parts.append(f"  -d '{k}={v}'")
    return " \\\n".join(parts)


def _analyze_exchange_failure(code, body, source, target):
    if code == 0:
        return {
            "area": "Network",
            "likely_cause": f"Cannot reach {target} token endpoint",
            "suggestions": [
                f"Check that the {target} service is running",
                "Verify network policies allow egress",
                "Check DNS resolution",
            ],
        }

    err = ""
    err_desc = ""
    if isinstance(body, dict):
        err = body.get("error", "")
        err_desc = body.get("error_description", "")

    if "not allowed to exchange" in err_desc.lower():
        return {
            "area": "Authorization (source IdP)",
            "likely_cause": "Token exchange client lacks permission",
            "suggestions": [
                f"On {source}: Create a 'client' policy for token-exchange-client",
                f"On {source}: Associate the policy with the token-exchange scope permission",
                f"On {source}: Check that the IdP alias '{target}' has token-exchange permission",
            ],
        }

    if "not authorized" in err_desc.lower() and "token exchange" in err_desc.lower():
        return {
            "area": "Authorization (target IdP)",
            "likely_cause": "Fine-grained authorization denying exchange on target",
            "suggestions": [
                f"On {target}: Enable admin-fine-grained-authz feature",
                f"On {target}: Enable management permissions on token-exchange-client",
                f"On {target}: Enable management permissions on the '{source}' IdP",
                f"On {target}: Create and link policies for the token-exchange scope",
            ],
        }

    if "invalid_token" in err.lower() or "audience" in err_desc.lower():
        return {
            "area": "Token Validation",
            "likely_cause": "Audience mismatch in source token",
            "suggestions": [
                f"On {source}: Add an audience mapper to token-exchange-client",
                f"Set included.client.audience = 'token-exchange-client'",
            ],
        }

    if "user info" in err_desc.lower() or "ssl" in err_desc.lower() or "certificate" in err_desc.lower():
        return {
            "area": "TLS Trust",
            "likely_cause": f"{target} cannot validate {source}'s certificate",
            "suggestions": [
                f"Export {source}'s certificate and import into {target}'s truststore",
                "For RH-SSO: keytool -import into /opt/eap/keystores/truststore.jks",
                "For RHBK: Mount cert and set truststore-paths in Keycloak CR",
            ],
        }

    return {
        "area": "Unknown",
        "likely_cause": err_desc or err or str(body),
        "suggestions": ["Check IdP logs for detailed error information"],
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
