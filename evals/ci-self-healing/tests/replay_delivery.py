"""Offline Q18 replay. Pins raw inputs and reads the historical receiver formats."""
import argparse
import copy
import json
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from harness import delivery, grading
from harness.catalog import Catalog
from harness.core import EvaluationError, read_allowed, read_json, save_json, sha, tree_digest


def require(condition, reason):
    if not condition:
        raise EvaluationError(reason)


def install_receiver(packet, normalized, source):
    rows = []
    for index, row in enumerate(normalized, 1):
        ref = f"receiver-{index}"
        packet["evidence"][ref] = {"kind": "receiver_record", "source": source, "content": row,
                                  "complete": True, "sha256": sha(row)}
        rows.append(dict(row, evidence_id=ref))
    packet["facts"]["handoffs"] = rows
    audit = {"recipient":delivery.RECIPIENT,"count":len(rows)}
    packet["evidence"]["handoff-audit"] = {"kind":"receiver_audit", "content":audit,
                                          "source":source, "complete":True, "sha256":sha(audit)}


def archived_receiver(trial, packet, number):
    if number == 13:
        raw = packet["facts"]["retrieval"]
        require(not any(g.startswith(("missing_retrieval","retrieval:")) for g in packet["gaps"])
                and [r["seq"] for r in raw] == list(range(1,len(raw)+1)), "incomplete archived receiver log")
        install_receiver(packet, [r for r in raw if r["operation"] == "submit_handoff"], "host/retrieval-audit.jsonl")
        return
    # This test emitted files in an Agent-writable directory. Bind each new
    # receipt to its exact bytes AND to a returned receipt from the sender.
    facts = packet["facts"]
    require(not any(p.startswith("receiver-state/") for p in facts["visible_before"]), "receiver was not initially empty")
    cli = str(trial/"visible/scene/tool/host-handoff.py")
    sends = [t for t in grading._tools(packet) if t["name"] == "bash" and cli in grading._command(t)
             and "--material" in grading._command(t)]
    rows = []
    for key, state in sorted(facts["visible_after"].items()):
        if not key.startswith("receiver-state/"):
            continue
        path = grading._safe(trial, Path("visible")/key)
        record = read_json(path)
        require(sha(read_allowed(path)) == state["sha256"] and path.stem == record["id"], "receiver file changed")
        public = {k:record[k] for k in ("id","recipient","accepted","submitted")}
        matched = [t for t in sends if public in list(grading._json_objects(t["text"]))]
        require(len(matched)==1 and record["submitted"] is True, "receipt lacks a matching sender return")
        tool = matched[0]
        rows.append({"time_ns":tool["end"]["observed_ns"],"arguments":{"to":record["recipient"],"material":record["material"]},
                     "result":{"status":"submitted","receipt_id":record["id"],"recipient":record["recipient"],
                               "ownership":"confirmed" if record["accepted"] else "unconfirmed"},
                     "return_evidence":tool["end"]["evidence_id"],"file":key})
    install_receiver(packet,rows,"visible/receiver-state + host/events.jsonl")


def run(archive, pins):
    catalog = Catalog()
    checks = []
    for name, pin in pins.items():
        trial=(archive/name).resolve()
        def inputs():
            return {k:sha(read_allowed(grading._safe(trial,k))) for k in pin["inputs"]}
        require(inputs()==pin["inputs"], f"frozen inputs changed: {name}")
        design=read_json(trial/"host/design.json")
        execution=read_json(trial/"host/execution.json")
        require(design["runtime_skill_sha256"] == tree_digest(trial/"runtime/skills/ci-self-healing")
                == execution["runtime_skill_sha256_before"] == execution["runtime_skill_sha256_after"], "distribution changed")
        require(design["task_sha256"] == pin["inputs"]["visible/task.txt"], "unbound task")
        number=int(name[:2]); dual=number!=13
        case=copy.deepcopy(catalog.cases["delivery-unattended-receiver" if dual else "delivery-authorized-receiver"])
        case["fixture"]["parameters"]["delivery_fixture"]["jobs"]=design["jobs"]
        case["criteria_ids"] = [delivery.FILES] + (["delivery.report-contract","delivery.readback"] if dual else [])
        if number in (12,13):
            case["criteria_ids"].append(delivery.HANDOFF)
        packet=grading.collect(trial, case, execution, {})
        # The archival runtime kept the interpreter identity in the execution receipt.
        packet["facts"]["trusted_python"]=execution["runtime_identity"]["python"]["path"]
        if number in (12,13):
            archived_receiver(trial,packet,number)
        result=grading.deterministic(case,packet)
        by_id={r["criterion_id"]:r for r in result}
        require({i:r["status"] for i,r in by_id.items()} == pin["expected"], f"unexpected judgments: {name}")
        if "expected_triage" in pin:
            require(by_id[delivery.FILES]["file_checks"][delivery.TRIAGE] == pin["expected_triage"], "triage findings changed")
        if number in (12, 13):
            receipts = by_id[delivery.HANDOFF]["receipts"]
            require(len(receipts) == 2 and all(r["ownership"] == "unconfirmed" for r in receipts), "receiver facts changed")
            require([r["id_in_final_answer"] for r in receipts] == [False, True], "receipt citations changed")
        require(inputs()==pin["inputs"], "source changed during replay")
        checks.append({"archive":name,"new_model_calls":0,"checks":result,
                       "source_hashes":pin["inputs"],"skill_sha256":design["runtime_skill_sha256"]})
    return {"status":"passed","catalog_sha256":catalog.digest,"new_model_calls":0,"cases":checks}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive",type=Path,default=ENGINE/"runs/session-scope-real-20261107")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(args.archive.resolve()):
        parser.error("choose a fresh output outside the frozen archive")
    result=run(args.archive,read_json(ENGINE/"fixtures/regression/delivery-frozen-v1.json"))
    save_json(args.output,result)
    for row in result["cases"]:
        print(row["archive"],[(c["criterion_id"],c["status"]) for c in row["checks"]])
