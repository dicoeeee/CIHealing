"""Real loopback HTTP cancellation, synthetic upstream only; never calls a model."""

from concurrent.futures import ThreadPoolExecutor
import httpx
import json
from pathlib import Path
import sys
import threading
import time
from unittest import mock

from common import HOST_CLOCK_ID, run_logged, snapshot, write_json, write_new
from environment import gateway
from runtime_diagnostics import write_diagnostics


class Stream(httpx.SyncByteStream):
    def __init__(self, release, upstream_error):
        self.release, self.upstream_error = release, upstream_error

    def __iter__(self):
        yield b"data: first\n\n"
        if not self.release.wait(10):
            raise httpx.ReadTimeout("synthetic fixture synchronization failed")
        if self.upstream_error:
            raise httpx.ReadError("SECRET_UPSTREAM_ERROR_BODY")
        for _ in range(32):
            yield b"x" * 65536


def cancelled_stream(root, trial_id="trial-test", *, upstream_error=False, workspace=None):
    """Run the actual host timeout + gateway + diagnostics path, not a fake receipt."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if workspace is None:
        workspace = root / "visible"
        write_new(workspace / "job-input.txt", "preserve this synthetic input\n")
    before = snapshot(workspace)
    write_json(root / "before.json", before)
    binding = {"trial_id": trial_id, "gateway_id": "synthetic-gateway", "clock_id": HOST_CLOCK_ID}
    release, finished = threading.Event(), threading.Event()
    log = root / "gateway.log"
    def event_sink(event):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        if event["kind"] == "request_finished":
            finished.set()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=Stream(release, upstream_error)))
    client = httpx.Client(transport=transport)
    config = {"base_url": "https://example.invalid", "api_key": "SYNTHETIC_NOT_A_KEY",
              "model_id": "synthetic-model", "local_token": "synthetic-token", "event_binding": binding}
    with mock.patch.object(gateway.httpx, "Client", return_value=client):
        server = gateway.make_server(config, host="127.0.0.1", port=0, event_sink=event_sink)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    server_thread.start()
    code = (
        "import json,sys,time,urllib.request; "
        "body=json.dumps({'model':'synthetic-model','input':'synthetic','stream':True}).encode(); "
        "req=urllib.request.Request(sys.argv[1],data=body,headers={'Authorization':'Bearer synthetic-token'}); "
        "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        "response=opener.open(req,timeout=5); response.read(1); print('client-ready',flush=True); time.sleep(20)"
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_logged, [sys.executable, "-u", "-c", code,
                                             f"http://127.0.0.1:{server.server_address[1]}/v1/responses"],
                                 root / "execution", 2.0, grace_seconds=.3,
                                 identity={k: binding[k] for k in ("trial_id", "gateway_id")})
            # Wait on observed readiness, not an assumed network sleep interval.
            deadline = time.monotonic() + 5
            stdout = root / "execution/stdout.log"
            while not (stdout.exists() and b"client-ready" in stdout.read_bytes()):
                if future.done() or time.monotonic() >= deadline:
                    raise AssertionError("client never consumed the synthetic stream")
                finished.wait(.01)
            process = future.result(timeout=5)
            # Release upstream bytes only AFTER the owned client has exited.
            release.set()
            if not finished.wait(5):
                raise AssertionError("gateway did not record stream termination")
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    if server_thread.is_alive():
        raise AssertionError("gateway server did not stop")
    after = snapshot(workspace)
    write_json(root / "after.json", after)
    if after != before:
        raise AssertionError("loopback fixture changed the source workspace")
    diagnostic = write_diagnostics(root, root / "no-model-sessions", process,
                                   gateway_log=log, gateway_binding=binding, changed_paths=[])
    return {"status": process["status"], "process": process, "filesystem_status": "captured",
            "gateway_cleanup_confirmed": True, "isolation_confirmed": process["cleanup"]["status"] == "confirmed",
            "runtime_diagnostics": diagnostic}
