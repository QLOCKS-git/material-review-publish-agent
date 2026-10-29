"""Run the executable test suite and persist its actual results for reviewers."""
import io
import json
from pathlib import Path
import sys
import time
import unittest
from datetime import datetime, timezone

stream = io.StringIO()
started = time.monotonic()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.discover("tests"))
Path("examples").mkdir(exist_ok=True)
Path("examples/test-results.txt").write_text(stream.getvalue())
Path("examples/test-results.json").write_text(json.dumps({
    "generated_at": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
    "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
    "passed": result.wasSuccessful(), "elapsed_seconds": round(time.monotonic() - started, 3),
    "report": "examples/test-results.txt",
}, ensure_ascii=False, indent=2) + "\n")
print(stream.getvalue())
raise SystemExit(0 if result.wasSuccessful() else 1)
