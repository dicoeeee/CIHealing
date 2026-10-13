"""Run inside an eval-network-only container. No successful model requests."""

import json
import os
import socket
import sys
import urllib.error
import urllib.request


def main():
    model = sys.argv[1]
    base = "http://ci-eval-gateway:8080"
    def request(path, body=None, token="ci-eval-local-only"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
    body = {"model": model, "input": "unused"}
    checks = {
        "gateway_healthy": request("/health") == 200,
        "wrong_token_rejected": request("/v1/responses", body, "wrong") == 401,
        "wrong_model_rejected": request("/v1/responses", {**body, "model": "not-allowed"}) == 403,
        "arbitrary_path_rejected": request("/v1/chat/completions", body) == 404,
        "hosted_tool_rejected": request("/v1/responses", {**body, "tools": [{"type": "web_search"}]}) == 403,
        "storage_reference_rejected": request("/v1/responses", {**body, "previous_response_id": "not-allowed"}) == 403,
        "no_upstream_credential_env": all("API_KEY" not in name for name in os.environ),
    }
    for address, port in (("1.1.1.1", 443), ("192.168.5.2", 15236)):
        try:
            with socket.create_connection((address, port), timeout=2):
                blocked = False
        except OSError:
            blocked = True
        checks[f"direct_{address}_{port}_blocked"] = blocked
    print(json.dumps(checks))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
