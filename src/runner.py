"""Bounded model -> tool -> observation loop; outputs are derived from tool facts."""
from datetime import datetime, timezone
import json
import time
from .domain import validate_request, validate_decision
from .models import SYSTEM_PROMPT, ModelError
from .tools import BusinessTools

def build_output(tools, model, stop_reason, steps):
    results, outcomes = [], []
    _, current_version = tools.world()
    for item in tools.request["items"]:
        state = tools.state[item["material_id"]]
        checked = state.get("review", {})
        read = state.get("read", {})
        saved = state.get("save", {})
        draft = saved.get("draft", {}) if saved.get("ok") else {}
        review_status = checked.get("review_status", "NEEDS_INFO" if read and not read.get("ok") else "NOT_REVIEWED")
        risks = checked.get("risks", [])
        confirmations = draft.get("pending_confirmations", [])
        suggested = draft.get("proposed_at", state.get("attempted_at"))
        draft_status = saved.get("code") if saved.get("ok") else "FAILED" if saved.get("code") == "SAVE_FAILED" else "NOT_SAVED"
        error = None if saved.get("ok") else saved.get("message")
        if saved.get("ok") and draft.get("schedule_version") != current_version:
            draft_status, error = "STALE", "保存后资料或排期发生变化，草稿需重新核验。"
            confirmations = [{"code": "SCHEDULE_CHANGED", "message": error}]
        if review_status == "BLOCKED":
            outcome = "BLOCKED"
        elif review_status == "NEEDS_INFO":
            outcome = "NEEDS_INFO"
            confirmations = [{"code": "METADATA_REQUIRED", "message": "请补充缺失素材资料或有效素材编号。"}]
        elif draft_status == "FAILED":
            outcome = "FAILED"
        elif draft_status == "STALE":
            outcome = "INCOMPLETE"
        elif saved.get("ok"):
            outcome = "PENDING_CONFIRMATION" if draft["confirmation_status"] == "PENDING" else "COMPLETED"
        elif state.get("slots") and not state["slots"]["available_slots"]:
            outcome = "PENDING_CONFIRMATION"
            confirmations = [{"code": "NO_AVAILABLE_SLOT", "message": "当前没有可用时段，请调整日期或时间范围。"}]
        else:
            outcome = "INCOMPLETE"
        outcomes.append(outcome)
        results.append({
            "material_id": item["material_id"], "review_status": review_status, "risks": risks,
            "requested_at": item["requested_at"], "hard_time": item["hard_time"], "suggested_at": suggested,
            "confirmation_status": "PENDING" if draft_status == "STALE" else draft.get("confirmation_status", "PENDING" if confirmations else "NOT_APPLICABLE"),
            "pending_confirmations": confirmations,
            "draft_save_status": draft_status, "draft_id": draft.get("draft_id"), "save_error": error,
            "outcome": outcome, "published": False,
        })
    if stop_reason != "MODEL_FINISH":
        status = "INCOMPLETE"
    elif "INCOMPLETE" in outcomes:
        status = "INCOMPLETE"
    elif len(set(outcomes)) == 1:
        status = outcomes[0]
    elif "FAILED" in outcomes:
        status = "PARTIAL"
    elif "PENDING_CONFIRMATION" in outcomes:
        status = "PENDING_CONFIRMATION"
    else:
        status = "PARTIAL"
    return {"request_id": tools.request["request_id"], "mode": model.kind, "model": model.name,
            "status": status, "stop_reason": stop_reason, "model_steps": steps,
            "publication_status": "DRAFT_ONLY", "published": False, "items": results}

def run_agent(scenario_path, store, model, max_steps=16):
    if not 1 <= max_steps <= 100:
        raise ValueError("max_steps 必须为 1..100")
    raw = json.loads(scenario_path.read_text(encoding="utf-8"))
    request = validate_request(raw["request"])
    tools = BusinessTools(scenario_path, request, store)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]
    trace = {
        "mode": model.kind, "model": model.name,
        "evidence_scope": "real_local_model_decisions" if model.kind == "real" else "offline_flow_only_not_model_quality",
        "started_at": datetime.now(timezone.utc).isoformat(), "max_steps": max_steps, "steps": [],
    }
    stop_reason = "STEP_LIMIT"
    started = time.monotonic()
    for number in range(1, max_steps + 1):
        try:
            raw_decision = model.decide(messages, tools.snapshot(), request)
        except ModelError as error:
            if trace["steps"]:
                trace["steps"][-1]["next_action"] = "MODEL_ERROR"
            trace["model_error"] = str(error)
            stop_reason = "MODEL_ERROR"
            break
        try:
            chosen = validate_decision(raw_decision)
        except (ValueError, TypeError) as error:
            chosen = {"action": "INVALID_DECISION", "decision_basis": "模型输出不符合动作协议。"}
            observation = {"ok": False, "code": "INVALID_DECISION", "message": str(error)}
        else:
            observation = None
        if trace["steps"]:
            trace["steps"][-1]["next_action"] = chosen["action"]
        params = {}
        if chosen["action"] != "finish":
            params["material_id"] = chosen.get("material_id")
            if chosen["action"] == "read_material":
                params["include_supplement"] = chosen.get("include_supplement", False)
            if chosen["action"] == "save_draft":
                params["proposed_at"] = chosen.get("proposed_at")
        if chosen["action"] == "finish":
            observation = {"ok": True, "message": "模型结束本次处理，最终状态由工具证据汇总。"}
        elif observation is None:
            try:
                observation = tools.execute(chosen)
            except (ValueError, KeyError, TypeError) as error:
                observation = {"ok": False, "code": "INVALID_TOOL_ARGUMENTS", "message": str(error)}
            except OSError:
                observation = {"ok": False, "code": "TOOL_UNAVAILABLE", "message": "本地业务资料暂不可读。"}
        trace["steps"].append({
            "step": number, "action": chosen["action"], "tool_parameters": params,
            "return_summary": observation, "decision_basis": chosen["decision_basis"][:240],
            "next_action": None,
        })
        messages.append({"role": "assistant", "content": json.dumps(chosen, ensure_ascii=False)})
        messages.append({"role": "user", "content": "工具观察结果：" + json.dumps(observation, ensure_ascii=False)})
        if chosen["action"] == "finish":
            stop_reason = "MODEL_FINISH"
            trace["steps"][-1]["next_action"] = "END"
            break
    if trace["steps"] and trace["steps"][-1]["next_action"] is None:
        trace["steps"][-1]["next_action"] = stop_reason
    output = build_output(tools, model, stop_reason, len(trace["steps"]))
    trace["stop_reason"] = stop_reason
    trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
    trace["usage"] = getattr(model, "calls", [])
    return output, trace
