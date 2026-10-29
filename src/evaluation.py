"""Generate actual input/output/trace records and verify acceptance invariants."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from .cli import model_for, write_json
from .runner import run_agent
from .store import DraftStore

CASES = ("normal", "missing_info", "rule_risk", "time_conflict", "save_failure", "supplemented", "review_confirmation", "duplicate_submission")

def validate_case(case, output, store):
    item = output["items"][0]
    assert not output["published"] and not item["published"]
    expected = {
        "normal": ("COMPLETED", 1), "missing_info": ("NEEDS_INFO", 0),
        "rule_risk": ("BLOCKED", 0), "time_conflict": ("PENDING_CONFIRMATION", 1),
        "save_failure": ("FAILED", 0), "supplemented": ("COMPLETED", 1),
        "review_confirmation": ("PENDING_CONFIRMATION", 1),
    }
    status, rows = expected[case]
    assert output["status"] == status, f"expected {status}, got {output['status']}"
    assert len(store.all()) == rows, f"expected {rows} stored draft(s)"
    if case == "time_conflict":
        assert item["requested_at"].endswith("18:00:00+08:00")
        assert item["suggested_at"].endswith("19:00:00+08:00")
        assert item["confirmation_status"] == "PENDING"
        assert any(row["code"] == "TIME_CHANGE" for row in item["pending_confirmations"])
    if case == "save_failure":
        assert item["draft_save_status"] == "FAILED" and item["draft_id"] is None
    if case == "normal":
        assert item["requested_at"] == item["suggested_at"] and item["confirmation_status"] == "CONFIRMED"

def validate_model_path(trace):
    """A correct final guard result must not hide an invalid model action."""
    reviews = {}
    for step in trace["steps"]:
        action = step["action"]
        material_id = step["tool_parameters"].get("material_id")
        if action == "read_material":
            reviews.pop(material_id, None)
        if action in ("query_slots", "save_draft"):
            assert reviews.get(material_id) in ("APPROVED", "REQUIRES_CONFIRMATION"), (
                "model attempted scheduling before a complete, allowed review")
        if action == "check_rules" and step["return_summary"].get("ok"):
            reviews[material_id] = step["return_summary"]["review_status"]

def evaluate_suite(mode, destination, max_steps=16):
    destination = Path(destination)
    results = []
    with TemporaryDirectory(prefix="scheduling-eval-") as temporary:
        for case in CASES:
            source = json.loads((Path("data/scenarios") / (case + ".json")).read_text())
            store = DraftStore(Path(temporary) / (case + ".sqlite"))
            folder = destination / case
            folder.mkdir(parents=True, exist_ok=True)
            runs, traces = [], []
            if case == "duplicate_submission":
                variants = [("first", []), ("repeated", []),
                            ("occupied_18", [source["request"]["items"][0]["requested_at"]]),
                            ("occupied_18_19", [source["request"]["items"][0]["requested_at"], "2026-11-05T19:00:00+08:00"])]
            else:
                variants = [("", None)]
            for label, occupied in variants:
                current = copy.deepcopy(source)
                if occupied is not None:
                    current["world"]["occupied"] = [{"channel": "main", "at": value} for value in occupied]
                target = folder / label if label else folder
                input_path = target / "input.json"
                write_json(input_path, current)
                output, trace = run_agent(input_path, store, model_for(mode), max_steps)
                write_json(target / "output.json", output)
                write_json(target / "trace.json", trace)
                runs.append(output)
                traces.append(trace)
            write_json(folder / "stored-drafts.json", store.all())
            try:
                for actual_trace in traces:
                    validate_model_path(actual_trace)
                if case == "duplicate_submission":
                    first, repeated, changed, changed_again = [run["items"][0] for run in runs]
                    assert first["draft_save_status"] == "SAVED"
                    assert repeated["draft_save_status"] == "UNCHANGED"
                    assert len({item["draft_id"] for item in (first, repeated, changed, changed_again)}) == 1
                    assert len(store.all()) == 1
                    assert changed["draft_save_status"] == changed_again["draft_save_status"] == "UPDATED"
                    assert changed["suggested_at"].endswith("19:00:00+08:00")
                    assert changed_again["suggested_at"].endswith("20:00:00+08:00")
                    assert changed["confirmation_status"] == changed_again["confirmation_status"] == "PENDING"
                    assert all(not run["published"] for run in runs)
                else:
                    validate_case(case, runs[0], store)
                passed, error = True, None
            except AssertionError as failure:
                passed, error = False, str(failure) or "Acceptance invariant failed"
            results.append({"case": case, "passed": passed, "actual_statuses": [run["status"] for run in runs],
                            "error": error, "artifacts": str(folder)})
            print(case, "PASS" if passed else "FAIL", flush=True)
        limit_input = destination / "step_limit/input.json"
        write_json(limit_input, json.loads(Path("data/scenarios/normal.json").read_text()))
        limited, limit_trace = run_agent(limit_input, DraftStore(Path(temporary) / "limit.sqlite"), model_for(mode), 1)
        write_json(destination / "step_limit/output.json", limited)
        write_json(destination / "step_limit/trace.json", limit_trace)
        results.append({"case": "step_limit", "passed": limited["status"] == "INCOMPLETE" and limited["stop_reason"] == "STEP_LIMIT",
                        "actual_statuses": [limited["status"]], "error": None, "artifacts": str(destination / "step_limit")})
    report = {"mode": mode, "evidence_scope": "real_local_model_decisions" if mode == "real" else "offline_flow_only_not_model_quality",
              "passed": all(row["passed"] for row in results), "passed_cases": sum(row["passed"] for row in results),
              "total_cases": len(results), "cases": results}
    write_json(destination / "evaluation.json", report)
    return report
