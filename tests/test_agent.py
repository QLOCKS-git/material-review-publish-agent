"""Acceptance and safety tests. Scripted models here test controls, not model quality."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest
import sqlite3
from unittest.mock import patch
from tests import fixtures
from src.domain import validate_request, original_hash
from src.models import MockModel, ModelError, decision
from src.runner import run_agent
from src.store import DraftStore
from src.tools import BusinessTools
from src.evaluation import validate_model_path

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DraftStore(self.root / "drafts.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def scenario(self, name="normal", change=None):
        value = json.loads((Path("data/scenarios") / (name + ".json")).read_text())
        if change: change(value)
        path = self.root / "scenario.json"
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def run_case(self, name="normal", change=None, model=None, steps=16):
        return run_agent(self.scenario(name, change), self.store, model or MockModel(), steps)

    def test_normal_only_saves_draft(self):
        output, trace = self.run_case()
        self.assertEqual(output["status"], "COMPLETED")
        self.assertEqual(len(self.store.all()), 1)
        self.assertFalse(output["published"])
        self.assertEqual([row["action"] for row in trace["steps"]],
                         ["read_material", "check_rules", "query_slots", "save_draft", "finish"])

    def test_missing_information_requests_supplement_without_inventing_rights(self):
        output, trace = self.run_case("missing_info")
        self.assertEqual(output["status"], "NEEDS_INFO")
        self.assertFalse(self.store.all())
        self.assertTrue(any(row["tool_parameters"].get("include_supplement") for row in trace["steps"]))
        self.assertEqual(output["items"][0]["review_status"], "NEEDS_INFO")

    def test_supplement_changes_review_and_next_action(self):
        output, trace = self.run_case("supplemented")
        self.assertEqual(output["status"], "COMPLETED")
        self.assertEqual(sum(row["action"] == "check_rules" for row in trace["steps"]), 2)

    def test_blocking_risk_prevents_scheduling(self):
        output, trace = self.run_case("rule_risk")
        self.assertEqual(output["status"], "BLOCKED")
        self.assertFalse(self.store.all())
        self.assertNotIn("save_draft", [row["action"] for row in trace["steps"]])

    def test_conflict_is_suggested_but_hard_requirement_is_unchanged(self):
        output, _ = self.run_case("time_conflict")
        item = output["items"][0]
        self.assertEqual(item["requested_at"], fixtures.at(18))
        self.assertEqual(item["suggested_at"], fixtures.at(19))
        self.assertEqual(item["confirmation_status"], "PENDING")
        self.assertEqual(item["pending_confirmations"][0]["code"], "TIME_CHANGE")
        self.assertFalse(self.store.all()[0]["published"])

    def test_explicit_time_approval_allows_confirmed_draft(self):
        output, _ = self.run_case("time_conflict", lambda value: value["request"]["items"][0].update(
            approved_alternatives=[fixtures.at(19)]))
        self.assertEqual(output["items"][0]["confirmation_status"], "CONFIRMED")

    def test_risk_requires_confirmation_and_can_be_explicitly_confirmed(self):
        output, _ = self.run_case("review_confirmation")
        self.assertEqual(output["status"], "PENDING_CONFIRMATION")
        output, _ = self.run_case("review_confirmation", lambda value: value["request"]["items"][0].update(
            approved_risks=["DISCLAIMER_REQUIRED"]))
        self.assertEqual(output["items"][0]["confirmation_status"], "CONFIRMED")

    def test_duplicate_submission_has_one_stable_draft(self):
        first, _ = self.run_case()
        second, _ = self.run_case()
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(first["items"][0]["draft_id"], second["items"][0]["draft_id"])
        self.assertEqual(second["items"][0]["draft_save_status"], "UNCHANGED")

    def test_changed_occupied_slot_updates_same_draft(self):
        first, _ = self.run_case()
        changed, _ = self.run_case("normal", lambda value: value["world"].update(
            occupied=[{"channel": "main", "at": fixtures.at(18)}]))
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(first["items"][0]["draft_id"], changed["items"][0]["draft_id"])
        self.assertEqual(changed["items"][0]["suggested_at"], fixtures.at(19))
        self.assertEqual(changed["items"][0]["draft_save_status"], "UPDATED")

    def test_save_failure_cannot_claim_previous_row_was_updated(self):
        self.run_case()
        previous = self.store.all()
        output, _ = self.run_case("save_failure")
        self.assertEqual(output["status"], "FAILED")
        self.assertEqual(output["items"][0]["draft_save_status"], "FAILED")
        self.assertIsNone(output["items"][0]["draft_id"])
        self.assertEqual(self.store.all(), previous)

    def test_same_request_id_cannot_change_original_hard_time(self):
        self.run_case()
        output, _ = self.run_case(change=lambda value: value["request"]["items"][0].update(
            requested_at=fixtures.at(19)))
        self.assertEqual(output["items"][0]["draft_save_status"], "NOT_SAVED")
        self.assertIn("硬性要求", output["items"][0]["save_error"])
        self.assertEqual(self.store.all()[0]["requested_at"], fixtures.at(18))

    def test_step_limit_is_explicit_incomplete(self):
        class NeverFinish(MockModel):
            def decide(self, messages, state, request):
                return decision("read_material", "M01", reason="测试：持续读取以验证上限。")
        output, trace = self.run_case(model=NeverFinish(), steps=3)
        self.assertEqual(output["status"], "INCOMPLETE")
        self.assertEqual(output["stop_reason"], "STEP_LIMIT")
        self.assertEqual(len(trace["steps"]), 3)
        self.assertFalse(self.store.all())

    def test_model_claim_of_publication_has_no_authority(self):
        class Fabricating(MockModel):
            def decide(self, messages, state, request):
                return decision("finish", reason="测试恶意声明：已发布，已保存。")
        output, _ = self.run_case(model=Fabricating())
        self.assertFalse(output["published"])
        self.assertEqual(output["status"], "INCOMPLETE")
        self.assertFalse(self.store.all())

    def test_undeclared_publish_tool_is_rejected_and_bounded(self):
        class Publisher(MockModel):
            def decide(self, messages, state, request):
                return {"action": "publish", "decision_basis": "测试越权工具。"}
        output, trace = self.run_case(model=Publisher(), steps=2)
        self.assertEqual(output["status"], "INCOMPLETE")
        self.assertEqual(trace["steps"][0]["return_summary"]["code"], "INVALID_DECISION")
        self.assertFalse(self.store.all())

    def test_real_model_unavailable_never_falls_back_to_mock(self):
        class Unavailable(MockModel):
            kind, name = "real", "unavailable-test-model"
            def decide(self, messages, state, request):
                raise ModelError("simulated model offline")
        output, trace = self.run_case(model=Unavailable())
        self.assertEqual(output["mode"], "real")
        self.assertEqual(output["stop_reason"], "MODEL_ERROR")
        self.assertIn("offline", trace["model_error"])
        self.assertFalse(self.store.all())

    def test_changed_evidence_between_query_and_save_is_rejected(self):
        path = self.scenario()
        request = validate_request(json.loads(path.read_text())["request"])
        tools = BusinessTools(path, request, self.store)
        for action in ("read_material", "check_rules", "query_slots"):
            self.assertTrue(tools.execute(decision(action, "M01"))["ok"])
        value = json.loads(path.read_text())
        value["world"]["occupied"] = [{"channel": "main", "at": fixtures.at(18)}]
        path.write_text(json.dumps(value))
        result = tools.execute(decision("save_draft", "M01", proposed_at=fixtures.at(18)))
        self.assertEqual(result["code"], "STALE_SCHEDULE")
        self.assertFalse(self.store.all())

    def test_no_available_slot_requires_user_input_without_saving(self):
        output, _ = self.run_case(change=lambda value: value["world"].update(slots=[]))
        self.assertEqual(output["status"], "PENDING_CONFIRMATION")
        self.assertIsNone(output["items"][0]["suggested_at"])
        self.assertFalse(self.store.all())

    def test_multiple_materials_do_not_share_a_reserved_slot(self):
        def change(value):
            item = copy.deepcopy(value["request"]["items"][0])
            item["material_id"] = "M04"
            value["request"]["items"].append(item)
        output, _ = self.run_case(change=change)
        self.assertEqual(len(output["items"]), 2)
        self.assertEqual(len({row["proposed_at"] for row in self.store.all()}), 2)
        self.assertEqual(output["items"][1]["confirmation_status"], "PENDING")

    def test_missing_material_is_not_invented(self):
        output, _ = self.run_case(change=lambda value: value["request"]["items"][0].update(material_id="M404"))
        self.assertEqual(output["status"], "NEEDS_INFO")
        self.assertFalse(self.store.all())

    def test_naive_time_is_rejected(self):
        value = json.loads(self.scenario().read_text())["request"]
        value["items"][0]["requested_at"] = "2026-11-05T18:00:00"
        with self.assertRaises(ValueError):
            validate_request(value)

    def test_concurrent_repeats_have_one_row(self):
        payload = {"request_id": "same", "material_id": "M01", "channel": "main",
                   "proposed_at": fixtures.at(18), "published": False}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.save(payload, "original", lambda: True), range(8)))
        self.assertTrue(all(row["ok"] for row in results))
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(len({row["draft"]["draft_id"] for row in results}), 1)

    def test_two_requests_cannot_reserve_the_same_slot(self):
        first = {"request_id": "one", "material_id": "M01", "channel": "main", "proposed_at": fixtures.at(18)}
        second = dict(first, request_id="two")
        self.assertTrue(self.store.save(first, "one", lambda: True)["ok"])
        self.assertEqual(self.store.save(second, "two", lambda: True)["code"], "SLOT_CONFLICT")
        self.assertEqual(len(self.store.all()), 1)

    def test_save_connection_failure_is_explicit_and_not_success(self):
        payload = {"request_id": "one", "material_id": "M01", "channel": "main", "proposed_at": fixtures.at(18)}
        with patch.object(self.store, "connect", side_effect=sqlite3.OperationalError("unavailable")):
            result = self.store.save(payload, "original", lambda: True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SAVE_FAILED")
        self.assertFalse(self.store.all())

    def test_evaluation_rejects_unsafe_path_despite_correct_final_guard(self):
        trace = {"steps": [
            {"action": "check_rules", "tool_parameters": {"material_id": "M02"},
             "return_summary": {"ok": True, "review_status": "NEEDS_INFO"}},
            {"action": "save_draft", "tool_parameters": {"material_id": "M02"},
             "return_summary": {"ok": False, "code": "REVIEW_REQUIRED"}},
        ]}
        with self.assertRaises(AssertionError):
            validate_model_path(trace)
