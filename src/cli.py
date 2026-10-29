"""CLI entry point; model and mock modes never silently substitute for each other."""
import argparse
import json
import os
from pathlib import Path
from .models import MockModel, OllamaModel, LlamaCppModel
from .runner import run_agent
from .store import DraftStore

def load_env():
    path = Path(".env")
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def model_for(mode):
    if mode == "mock":
        return MockModel()
    backend = os.getenv("MODEL_BACKEND", "llamacpp")
    if backend not in ("llamacpp", "ollama"):
        raise ValueError("MODEL_BACKEND 必须是 llamacpp 或 ollama")
    adapter = LlamaCppModel if backend == "llamacpp" else OllamaModel
    return adapter(
        os.getenv("MODEL_BASE_URL", "http://127.0.0.1:11434"),
        os.getenv("MODEL_NAME", "qwen3:4b-instruct-2507-q4_K_M"),
        timeout=int(os.getenv("MODEL_TIMEOUT", "120")))

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main():
    load_env()
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent（只保存草稿）")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--mode", choices=("real", "mock"), required=True)
    run.add_argument("--db", type=Path, default=Path(".state/drafts.sqlite"))
    run.add_argument("--output", type=Path, default=Path(".state/output.json"))
    run.add_argument("--trace", type=Path, default=Path(".state/trace.json"))
    run.add_argument("--max-steps", type=int, default=16)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--mode", choices=("real", "mock"), required=True)
    evaluate.add_argument("--output-dir", type=Path)
    evaluate.add_argument("--max-steps", type=int, default=16)
    listing = commands.add_parser("drafts")
    listing.add_argument("--db", type=Path, default=Path(".state/drafts.sqlite"))
    args = parser.parse_args()
    try:
        if args.command == "run":
            output, trace = run_agent(args.input, DraftStore(args.db), model_for(args.mode), args.max_steps)
            write_json(args.output, output)
            write_json(args.trace, trace)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2 if output["status"] in ("INCOMPLETE", "FAILED") else 0
        if args.command == "evaluate":
            from .evaluation import evaluate_suite
            report = evaluate_suite(args.mode, args.output_dir or Path("examples") / args.mode, args.max_steps)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 2
        print(json.dumps(DraftStore(args.db).all(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))

if __name__ == "__main__":
    raise SystemExit(main())
