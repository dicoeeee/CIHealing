"""Cancellation correlation, privacy, and actual loopback gateway lifecycle tests."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import write_new
from execution_outcome import classify_execution
from gateway_evidence import read_gateway_evidence
from gateway_fixture import cancelled_stream


class GatewayEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ci-gateway-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.binding = {"trial_id": "trial-test", "gateway_id": "gateway-test", "clock_id": "clock-test"}
        self.process = {"status": "timeout", "returncode": 130, "cleanup": {"status": "confirmed"},
                        "identity": {"trial_id": "trial-test", "gateway_id": "gateway-test"}, "clock_id": "clock-test",
                        "termination": {"reason": "timeout", "pid": 100, "pgid": 100, "signals": [
                            {"reason": "timeout", "signal": "SIGINT", "requested_ns": 50, "completed_ns": 51, "delivery": "sent"}]}}

    def events(self, *, error_kind="downstream_disconnect", phase="downstream_write", error_type="BrokenPipeError", ns=60):
        rows = [("gateway_ready", 0, 10, {}), ("request_started", 1, 20, {}),
                ("upstream_headers", 1, 30, {"http_status": 200}),
                (error_kind, 1, ns, {"phase": phase, "error_type": error_type})]
        return [{"schema": "ci-gateway-event-v1", **self.binding, "seq": i, "monotonic_ns": t,
                 "request_id": rid, "kind": kind, **fields} for i, (kind, rid, t, fields) in enumerate(rows, 1)]

    def classify(self, rows=None, process=None, gaps=None, **overrides):
        evidence = {"schema": "ci-gateway-evidence-v1", "binding": self.binding,
                    "events": self.events() if rows is None else rows, "gaps": [] if gaps is None else gaps}
        data = {"process": self.process if process is None else process, "isolation_confirmed": True,
                "filesystem_status": "captured", "gateway_cleanup_confirmed": True,
                "runtime_diagnostics": {"gateway_statuses": [], "gateway_evidence": evidence}, **overrides}
        return classify_execution(data, "run")

    def test_bound_downstream_disconnect_after_cancel_continues(self):
        self.assertEqual(self.classify(), {"code": "trial_timeout", "scope": "case", "batch_action": "continue"})

    def test_disconnect_before_cancel_stops(self):
        self.assertEqual(self.classify(self.events(ns=40))["code"], "downstream_disconnect_unattributed")

    def test_unbound_or_unsent_cancellation_stops(self):
        for key, value in (("identity", {}), ("clock_id", "another-host"), ("termination", {})):
            p = copy.deepcopy(self.process); p[key] = value
            self.assertEqual(self.classify(process=p)["batch_action"], "stop")
        for delivery in ("failed", "process_absent"):
            p = copy.deepcopy(self.process); p["termination"]["signals"][0]["delivery"] = delivery
            self.assertEqual(self.classify(process=p)["batch_action"], "stop")

    def test_new_request_after_cancel_is_not_excused(self):
        rows = self.events(); rows[1]["monotonic_ns"] = 52; rows[2]["monotonic_ns"] = 53
        self.assertEqual(self.classify(rows)["batch_action"], "stop")

    def test_upstream_errors_before_or_after_cancel_always_stop(self):
        for ns in (40, 60):
            for error in ("ReadError", "ReadTimeout"):
                rows = self.events(error_kind="upstream_transport_error", phase="upstream_read", error_type=error, ns=ns)
                self.assertEqual(self.classify(rows)["code"], "provider_transport_error")

    def test_real_upstream_statuses_override_cancellation(self):
        for status, code in ((401, "provider_auth_rejected"), (403, "provider_auth_rejected"),
                             (429, "provider_rate_or_quota_limit"), (503, "provider_transport_error")):
            rows = self.events(); rows[2]["http_status"] = status
            self.assertEqual(self.classify(rows)["code"], code)

    def test_local_gateway_rejection_is_not_provider_auth(self):
        rows = self.events()[:2]
        rows.append({**rows[-1], "seq": 3, "monotonic_ns": 30, "kind": "gateway_policy_rejected", "phase": "request", "http_status": 403})
        self.assertEqual(self.classify(rows)["code"], "gateway_policy_rejected")

    def test_internal_error_is_not_a_disconnect(self):
        rows = self.events(error_kind="gateway_internal_error", error_type="Other")
        self.assertEqual(self.classify(rows)["code"], "gateway_internal_error")

    def test_mixed_upstream_and_downstream_faults_do_not_hide_upstream(self):
        rows = self.events()
        rows.append({**rows[-1], "seq": 5, "monotonic_ns": 61, "kind": "upstream_transport_error", "phase": "upstream_close", "error_type": "ReadError"})
        self.assertEqual(self.classify(rows)["code"], "provider_transport_error")

    def test_cleanup_snapshot_and_operator_boundaries(self):
        for field, value in (("isolation_confirmed", False), ("filesystem_status", "missing"), ("gateway_cleanup_confirmed", False)):
            self.assertEqual(self.classify(**{field: value})["batch_action"], "stop")
        p = copy.deepcopy(self.process); p["status"] = "interrupted"; p["termination"]["reason"] = "operator"
        self.assertEqual(self.classify(process=p)["batch_action"], "stop")

    def test_output_limit_can_be_isolated_but_is_never_success(self):
        p = copy.deepcopy(self.process); p["status"] = p["termination"]["reason"] = "output_limit"
        p["termination"]["signals"][0]["reason"] = "output_limit"
        self.assertEqual(self.classify(process=p)["code"], "trial_output_limit")

    def test_missing_native_gateway_evidence_never_continues(self):
        data = {"backend": "native", "process": self.process, "filesystem_status": "captured", "isolation_confirmed": True}
        self.assertEqual(classify_execution(data, "run")["code"], "gateway_evidence_unconfirmed")

    def test_open_request_can_only_be_terminated_by_bound_host_cancellation(self):
        rows = self.events()[:3]
        self.assertEqual(self.classify(rows)["batch_action"], "continue")
        p = copy.deepcopy(self.process); p["status"] = "completed"
        self.assertEqual(self.classify(rows, process=p)["code"], "gateway_request_unfinished")

    def test_completed_request_does_not_excuse_missing_cancel_receipt(self):
        rows = self.events()[:3]
        rows.append({**rows[1], "seq": 4, "monotonic_ns": 40, "kind": "request_finished", "outcome": "completed"})
        p = copy.deepcopy(self.process); p["termination"] = {}
        self.assertEqual(self.classify(rows, process=p)["batch_action"], "stop")

    def test_successful_request_with_valid_binding_is_not_a_transport_failure(self):
        rows = self.events()[:3]
        rows.append({**rows[1], "seq": 4, "monotonic_ns": 40, "kind": "request_finished", "outcome": "completed"})
        p = copy.deepcopy(self.process); p.update(status="completed", returncode=0)
        self.assertIsNone(self.classify(rows, process=p))

    def test_successful_request_cannot_use_another_trials_evidence(self):
        rows = self.events()[:3]
        rows.append({**rows[1], "seq": 4, "monotonic_ns": 40, "kind": "request_finished", "outcome": "completed"})
        p = copy.deepcopy(self.process); p.update(status="completed", returncode=0)
        p["identity"]["trial_id"] = "another-trial"
        self.assertEqual(self.classify(rows, process=p)["code"], "gateway_evidence_unconfirmed")

    def test_signal_in_flight_can_correlate_only_if_delivery_is_confirmed(self):
        p = copy.deepcopy(self.process); p["termination"]["signals"][0]["completed_ns"] = 65
        self.assertEqual(self.classify(process=p)["batch_action"], "continue")
        p["termination"]["signals"][0]["delivery"] = "failed"
        self.assertEqual(self.classify(process=p)["batch_action"], "stop")

    def test_unsubstantiated_terminal_outcome_is_a_gap(self):
        rows = self.events()[:3]
        rows.append({**rows[1], "seq": 4, "monotonic_ns": 40, "kind": "request_finished", "outcome": "downstream_disconnect"})
        self.assertEqual(self.classify(rows)["code"], "gateway_evidence_unconfirmed")

    def test_missing_unknown_truncated_and_legacy_logs_fail_closed(self):
        missing = read_gateway_evidence(self.root / "missing", self.binding)
        self.assertTrue(missing["gaps"])
        for index, raw in enumerate((b'{"upstream_transport_error":true}\n', b'{broken\n', b'{}\n',
                                     json.dumps(self.events()[0]).encode())):
            path = self.root / str(index); write_new(path, raw)
            d = read_gateway_evidence(path, self.binding)
            self.assertTrue(d["gaps"])
            self.assertEqual(d["http_statuses"], [])  # No fabricated HTTP 502.
            self.assertEqual(self.classify(gaps=d["gaps"])["batch_action"], "stop")

    def test_malformed_or_misbound_events_are_rejected_without_exception(self):
        for key, value in (("kind", []), ("phase", {}), ("error_type", []), ("request_id", "1"),
                           ("gateway_id", "other"), ("clock_id", "other"), ("seq", 2), ("monotonic_ns", 1)):
            rows = self.events(); rows[-1][key] = value
            self.assertEqual(self.classify(rows)["code"], "gateway_evidence_unconfirmed")

    def test_error_events_cannot_claim_success_status(self):
        for kind, phase in (("upstream_http_error", "upstream_connect"), ("gateway_policy_rejected", "request")):
            rows = self.events()[:3]
            rows.append({**rows[1], "seq": 4, "monotonic_ns": 40, "kind": kind, "phase": phase, "http_status": 200})
            self.assertEqual(self.classify(rows)["code"], "gateway_evidence_unconfirmed")

    def test_parser_preserves_only_allowlisted_metadata(self):
        path = self.root / "log"
        rows = self.events(); rows[-1]["error_message"] = "SECRET_MARKER"
        write_new(path, "\n".join(json.dumps(r) for r in rows) + "\n")
        d = read_gateway_evidence(path, self.binding)
        self.assertNotIn("SECRET_MARKER", json.dumps(d))
        self.assertTrue(d["gaps"])

    def test_real_loopback_cancel_produces_classifiable_downstream_event(self):
        data = cancelled_stream(self.root / "loopback")
        evidence = data["runtime_diagnostics"]["gateway_evidence"]
        self.assertEqual(evidence["gaps"], [])
        self.assertIn("downstream_disconnect", [r["kind"] for r in evidence["events"]])
        self.assertEqual(classify_execution(data, "run")["batch_action"], "continue")
        self.assertEqual(data["process"]["cleanup"]["status"], "confirmed")

    def test_real_upstream_error_after_cancel_still_stops(self):
        data = cancelled_stream(self.root / "loopback", upstream_error=True)
        self.assertEqual(classify_execution(data, "run")["code"], "provider_transport_error")
        self.assertNotIn("SECRET_UPSTREAM_ERROR_BODY", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
