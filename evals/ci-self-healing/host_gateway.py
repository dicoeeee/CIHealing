"""Trusted loopback gateway process. Credentials never enter the repair sandbox."""

import json
import os
from pathlib import Path
import sys

from common import write_json


def main():
    from chrys.foundation.util.env_templates import resolve_env_templates
    from native_repair import model_profile, generation_budget
    from environment.gateway import serve
    request = json.loads(sys.stdin.buffer.readline(65537))
    profile, fingerprint = model_profile(request["config"])
    if fingerprint != request["profile_sha256"]:
        raise ValueError("model profile changed before gateway startup")
    key = resolve_env_templates(profile.get("api_key", ""), location="native evaluation gateway")
    if not key:
        raise ValueError("missing model credential")
    def ready(port):
        target = Path(request["ready"])
        temporary = target.with_suffix(".tmp")
        write_json(temporary, {"port": port})
        os.rename(temporary, target)
    serve({"base_url": profile["base_url"], "api_key": key, "model_id": profile["model_id"],
           "local_token": request["local_token"], "max_requests": 256,
           "max_output_tokens": generation_budget(profile)["effective_max_output_tokens"],
           "event_binding": request["event_binding"]}, host="127.0.0.1", port=0,
          ready=ready)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"gateway_error_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)
