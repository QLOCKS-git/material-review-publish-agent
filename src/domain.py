"""Validated inputs and immutable publishing requirements."""
from datetime import datetime
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

ACTIONS = ("read_material", "check_rules", "query_slots", "save_draft", "finish")

def at(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return stamp.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")

def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def validate_request(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("request_id"), str) or not raw["request_id"].strip():
        raise ValueError("request_id 必须是非空字符串")
    if not isinstance(raw.get("items"), list) or not 1 <= len(raw["items"]) <= 10:
        raise ValueError("items 必须包含 1..10 项素材")
    result, seen = {"request_id": raw["request_id"], "items": []}, set()
    for item in raw["items"]:
        if not isinstance(item, dict):
            raise ValueError("素材项必须是对象")
        material_id = item.get("material_id")
        if not isinstance(material_id, str) or not material_id or material_id in seen:
            raise ValueError("material_id 必须非空且在请求中唯一")
        seen.add(material_id)
        hard, channel = item.get("hard_time", True), item.get("channel", "main")
        if not isinstance(hard, bool) or not isinstance(channel, str) or not channel:
            raise ValueError("hard_time 或 channel 不合法")
        risks, alternatives = item.get("approved_risks", []), item.get("approved_alternatives", [])
        if not isinstance(risks, list) or not all(isinstance(x, str) for x in risks) or not isinstance(alternatives, list):
            raise ValueError("确认项必须为数组")
        result["items"].append({
            "material_id": material_id, "channel": channel, "requested_at": at(item["requested_at"]),
            "hard_time": hard, "approved_alternatives": [at(x) for x in alternatives], "approved_risks": risks,
        })
    return result

def original_hash(item):
    return fingerprint({key: item[key] for key in ("material_id", "channel", "requested_at", "hard_time")})

def validate_decision(raw):
    if not isinstance(raw, dict) or raw.get("action") not in ACTIONS:
        raise ValueError("action 必须是声明的工具或 finish")
    if not isinstance(raw.get("decision_basis"), str) or not raw["decision_basis"].strip():
        raise ValueError("需要一句基于可见结果的决策依据")
    if raw["action"] != "finish" and not isinstance(raw.get("material_id"), str):
        raise ValueError("工具动作需要 material_id")
    if not isinstance(raw.get("include_supplement", False), bool):
        raise ValueError("include_supplement 必须为布尔值")
    if raw["action"] == "save_draft" and not isinstance(raw.get("proposed_at"), str):
        raise ValueError("保存需要 proposed_at")
    return raw
