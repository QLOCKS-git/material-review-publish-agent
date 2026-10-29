"""Local tools; observations and user consent are authoritative."""
import json
from pathlib import Path
from .domain import at, fingerprint, original_hash

def review(metadata, rules):
    missing = [key for key in rules["required_fields"] if metadata.get(key) is None or metadata.get(key) == ""]
    risks = []
    if missing:
        risks.append({"code": "MISSING_METADATA", "severity": "info", "message": "缺少素材信息：" + "、".join(missing)})
    if metadata.get("rights_cleared") is False:
        risks.append({"code": "RIGHTS_REJECTED", "severity": "block", "message": "素材未取得使用授权。"})
    labels = set(metadata.get("labels", [])) & set(rules.get("blocked_labels", []))
    if labels:
        risks.append({"code": "CONTENT_BLOCKED", "severity": "block", "message": "命中禁止标签：" + "、".join(sorted(labels))})
    for flag in metadata.get("review_flags", []):
        if flag in rules.get("confirmation_flags", {}):
            risks.append({"code": flag, "severity": "confirm", "message": rules["confirmation_flags"][flag]})
    status = "BLOCKED" if any(r["severity"] == "block" for r in risks) else (
        "NEEDS_INFO" if missing else "REQUIRES_CONFIRMATION" if risks else "APPROVED")
    return {"review_status": status, "risks": risks, "missing_fields": missing}

class BusinessTools:
    def __init__(self, scenario_path, request, store):
        self.path, self.request, self.store = Path(scenario_path), request, store
        self.items = {item["material_id"]: item for item in request["items"]}
        self.state = {key: {} for key in self.items}

    def world(self):
        world = json.loads(self.path.read_text(encoding="utf-8"))["world"]
        return world, fingerprint(world)

    def execute(self, decision):
        action, material_id = decision["action"], decision.get("material_id")
        if material_id not in self.items:
            return {"ok": False, "code": "UNKNOWN_MATERIAL", "message": "只能处理本次请求中的素材。"}
        world, version = self.world()
        state = self.state[material_id]
        if action == "read_material":
            material = world["materials"].get(material_id)
            if material is None:
                state.clear()
                state["read"] = {"ok": False, "code": "MATERIAL_NOT_FOUND", "message": "素材不存在，请补充有效素材编号。"}
                return state["read"]
            metadata = dict(material["metadata"])
            if decision.get("include_supplement"):
                metadata.update(material.get("supplement", {}))
            state.clear()
            state["read_version"] = version
            state["read"] = {"ok": True, "material_id": material_id, "metadata": metadata,
                             "supplement_available": bool(material.get("supplement")),
                             "supplement_checked": bool(decision.get("include_supplement"))}
            return state["read"]
        if action == "check_rules":
            if not state.get("read", {}).get("ok") or state.get("read_version") != version:
                return {"ok": False, "code": "READ_REQUIRED", "message": "请先读取最新素材资料。"}
            state["review"] = review(state["read"]["metadata"], world["rules"])
            state["review_version"] = version
            state.pop("save", None)
            return {"ok": True, **state["review"]}
        if action == "query_slots":
            if not state.get("read", {}).get("ok"):
                return {"ok": False, "code": "READ_REQUIRED", "message": "请先读取素材。"}
            item = self.items[material_id]
            occupied = {(row["channel"], at(row["at"])) for row in world["occupied"]}
            occupied.update((row["channel"], row["proposed_at"]) for row in self.store.reservations(self.request["request_id"], material_id))
            candidates = sorted({at(row["at"]) for row in world["slots"]
                if row["channel"] == item["channel"] and at(row["at"])[:10] == item["requested_at"][:10]
                and (item["channel"], at(row["at"])) not in occupied})
            state["slots"] = {"ok": True, "requested_at": item["requested_at"],
                "requested_available": item["requested_at"] in candidates,
                "available_slots": candidates, "schedule_version": version}
            if state.get("save", {}).get("retryable"):
                state.pop("save", None)
            return state["slots"]
        if action == "save_draft":
            result = self._save_checked(material_id, decision, world, version)
            state["save"] = result
            state["attempted_at"] = at(decision["proposed_at"])
            return result
        return {"ok": False, "code": "UNKNOWN_TOOL", "message": "工具不在允许列表内。"}

    def _save_checked(self, material_id, decision, world, version):
        state, item = self.state[material_id], self.items[material_id]
        checked = state.get("review", {})
        if checked.get("review_status") not in ("APPROVED", "REQUIRES_CONFIRMATION"):
            return {"ok": False, "code": "REVIEW_REQUIRED", "message": "审核未通过或资料不完整，不能保存排期。", "retryable": False}
        if state.get("review_version") != version or state.get("slots", {}).get("schedule_version") != version:
            return {"ok": False, "code": "STALE_SCHEDULE", "message": "请重新核验审核结果及可用时段。", "retryable": True}
        proposed = at(decision["proposed_at"])
        if proposed not in state["slots"]["available_slots"]:
            return {"ok": False, "code": "SLOT_CONFLICT", "message": "建议时间不在已核验的可用时段中。", "retryable": True}
        pending = []
        if item["hard_time"] and proposed != item["requested_at"] and proposed not in item["approved_alternatives"]:
            pending.append({"code": "TIME_CHANGE", "message": "建议时段与硬性要求不同，须由用户确认。"})
        for risk in checked["risks"]:
            if risk["severity"] == "confirm" and risk["code"] not in item["approved_risks"]:
                pending.append({"code": risk["code"], "message": risk["message"]})
        if world.get("faults", {}).get("save_draft"):
            return {"ok": False, "code": "SAVE_FAILED", "message": "模拟草稿存储服务不可用，未保存。", "retryable": False}
        payload = {
            "request_id": self.request["request_id"], "material_id": material_id,
            "channel": item["channel"], "requested_at": item["requested_at"], "hard_time": item["hard_time"],
            "proposed_at": proposed, "review_status": checked["review_status"], "risks": checked["risks"],
            "confirmation_status": "PENDING" if pending else "CONFIRMED",
            "pending_confirmations": pending, "published": False, "schedule_version": version,
        }
        return self.store.save(payload, original_hash(item), lambda: self.world()[1] == version)

    def snapshot(self):
        return self.state
