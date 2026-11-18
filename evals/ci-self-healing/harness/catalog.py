"""One validated inventory of requirements, atomic criteria and scenario designs."""
from pathlib import Path
import re

from .core import EvaluationError, REPO, SPEC, read_allowed, read_json, sha


def _index(rows, name):
    if not isinstance(rows, list) or not rows:
        raise EvaluationError(f"{name} must be a nonempty array")
    result = {}
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9._-]*", identifier) or identifier in result:
            raise EvaluationError(f"invalid or duplicate {name} id: {identifier}")
        result[identifier] = row
    return result


def _validate_java_case(case):
    identifier = case["id"]
    params = case["fixture"].get("parameters", {})
    revision = params.get("source_revision")
    # Validate the identifier only. Frozen catalogs remain readable when their
    # original templates are absent; prepare checks the actual directory tree.
    if not isinstance(revision, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", revision):
        raise EvaluationError(f"unsafe Java source_revision: {identifier}")
    target = params.get("target_contract")
    if target not in ("preserve", "extend", "ambiguous"):
        raise EvaluationError(f"unsupported Java target_contract: {identifier}")
    oracle = case["expected"].get("behavior_oracle", {})
    if oracle.get("kind") == "routing_behavior":
        expected = oracle.get("parameters", {})
        if target not in ("preserve", "extend"):
            raise EvaluationError(f"routing_behavior requires a determinate Java target: {identifier}")
        if expected.get("target_contract") != target:
            raise EvaluationError(f"Java oracle target_contract differs from fixture: {identifier}")
        if expected.get("require_legacy_interface") is not True:
            raise EvaluationError(f"Java routing oracle requires legacy compatibility: {identifier}")


class Catalog:
    def __init__(self, directory=SPEC, *, verify_sources=True):
        self.directory = Path(directory)
        self.documents = {name: read_json(self.directory / (name + ".json"))
                          for name in ("cases", "criteria", "behaviors", "legacy-case-map")}
        self.cases = _index(self.documents["cases"]["cases"], "case")
        self.criteria = _index(self.documents["criteria"]["criteria"], "criterion")
        self.behaviors = _index(self.documents["behaviors"]["behaviors"], "behavior")
        self.suites = self.documents["cases"]["suites"]
        for name, members in self.suites.items():
            if not isinstance(members, list) or not members or len(members) != len(set(members)) or set(members) - self.cases.keys():
                raise EvaluationError(f"invalid suite: {name}")
        if len(self.suites.get("smoke", [])) != 8:
            raise EvaluationError("the approved initial smoke suite must contain eight cases")
        for identifier, criterion in self.criteria.items():
            if criterion["behavior_id"] not in self.behaviors:
                raise EvaluationError(f"unknown behavior: {identifier}")
            if criterion["method"] not in {"deterministic", "semantic"}:
                raise EvaluationError(f"unknown grading method: {identifier}")
            if criterion["method"] == "deterministic" and not criterion.get("checker"):
                raise EvaluationError(f"missing deterministic checker: {identifier}")
            if set(criterion["verdicts"]) != {"pass", "fail", "inconclusive"}:
                raise EvaluationError(f"invalid atomic verdict definitions: {identifier}")
            if not criterion.get("sources") or not criterion.get("evidence"):
                raise EvaluationError(f"criterion lacks source or evidence: {identifier}")
        for identifier, case in self.cases.items():
            for field in ("title", "behavior_ids", "criteria_ids", "fixture", "opportunity", "expected", "legacy_ids", "tags"):
                if field not in case:
                    raise EvaluationError(f"case lacks {field}: {identifier}")
            ids = case["criteria_ids"]
            if not ids or len(ids) != len(set(ids)) or set(ids) - self.criteria.keys():
                raise EvaluationError(f"invalid case criteria: {identifier}")
            if set(case["behavior_ids"]) - self.behaviors.keys():
                raise EvaluationError(f"unknown case behavior: {identifier}")
            if case["fixture"]["kind"] not in {"java_contract", "retrieval", "workspace_isolation"}:
                raise EvaluationError(f"unsupported fixture family: {identifier}")
            if case["fixture"]["kind"] == "java_contract":
                _validate_java_case(case)
                if "contract.interface-oracle" in self.criteria and (
                        "contract.interface-oracle" in ids) != (
                        case["fixture"]["parameters"]["target_contract"] == "preserve"
                        and case["expected"]["behavior_oracle"]["kind"] == "routing_behavior"):
                    raise EvaluationError(f"Java compiled-interface check mismatches target: {identifier}")
            if case["fixture"]["kind"] == "workspace_isolation":
                from .workspace_isolation import validate_case
                validate_case(case)
            if case["task_type"] not in {"repair", "diagnosis"}:
                raise EvaluationError(f"unsupported task type: {identifier}")
            if case["opportunity"]["if_absent"] != "not_covered":
                raise EvaluationError(f"unobserved opportunity must not pass: {identifier}")
        mappings = self.documents["legacy-case-map"]["entries"]
        mapped = set()
        for row in mappings:
            old = row["legacy_id"]
            if old in mapped or set(row["target_case_ids"]) - self.cases.keys():
                raise EvaluationError("invalid legacy disposition mapping")
            mapped.add(old)
            for target in row["target_case_ids"]:
                if old not in self.cases[target]["legacy_ids"]:
                    raise EvaluationError("asymmetric legacy disposition mapping")
        if mapped != {old for c in self.cases.values() for old in c["legacy_ids"]}:
            raise EvaluationError("unmapped source material")
        self.source_drift = []
        if verify_sources:
            for row in self.documents["criteria"]["source_snapshot"]:
                relative = Path(row["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise EvaluationError("unsafe requirement source path")
                if sha(read_allowed(REPO / relative)) != row["sha256"]:
                    self.source_drift.append(row["path"])
            if self.source_drift:
                raise EvaluationError("requirement sources changed; review criteria before running: " + ", ".join(self.source_drift))
        self.digest = sha(self.documents)

    def select(self, suite="smoke", cases=None, tags=None):
        identifiers = list(cases) if cases else list(self.suites.get(suite, []))
        if not identifiers or set(identifiers) - self.cases.keys() or len(identifiers) != len(set(identifiers)):
            raise EvaluationError("empty, duplicate or unknown case selection")
        result = [self.cases[key] for key in identifiers]
        if tags:
            result = [case for case in result if set(tags) & set(case["tags"])]
        if not result:
            raise EvaluationError("selection matched no cases")
        return result

    def for_case(self, case, method=None):
        return [self.criteria[key] for key in case["criteria_ids"]
                if method is None or self.criteria[key]["method"] == method]

    def summary(self):
        referenced = {key for case in self.cases.values() for key in case["criteria_ids"]}
        return {"status": "passed", "cases": len(self.cases), "behaviors": len(self.behaviors),
                "criteria": len(self.criteria), "smoke_cases": self.suites["smoke"],
                "legacy_materials": len(self.documents["legacy-case-map"]["entries"]),
                "criteria_without_cases": sorted(self.criteria.keys() - referenced),
                "catalog_sha256": self.digest, "models_called": False,
                "coverage_status": "specified_not_executed"}
