"""Run this file directly from your IDE to debug one fixture evaluation.

Set breakpoints in evals.run.run_case, agent.loop.run_loop, and evals.run.grade.
The agent runs in this process so the debugger can step into those functions.
The fixture's independent checker still runs in a subprocess.
"""

from datetime import datetime
from pathlib import Path
import os
import sys
from uuid import uuid4

# Support direct IDE execution without requiring the project root on PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from evals import run


# Edit these settings, then click Run or Debug. No command-line arguments needed.
CASES = ["tail_window", "subtraction"]  # Use None to run every fixture.
MODE = "live"  # "golden" is offline; "noop" deliberately leaves the bug unfixed.
DEEPSEEK_MODEL = "deepseek-flash"  # Overrides .env for this debug process only.
# USD per million tokens: cache-hit input, cache-miss input, output.
# DeepSeek Flash peak rates checked 2026-11-17:
# https://api-docs.deepseek.com/quick_start/pricing/
# Use None to read PRICE_* from .env instead; update rates when changing models.
PRICES = run.Pricing(0.006, 0.30, 1.20)
REPEATS = 1
MAX_SPEND_USD = 1.0
RUN_TIMEOUT_SECONDS = 3600  # Includes time paused at debugger breakpoints.
CHECK_TIMEOUT_SECONDS = 5


def main():
    load_dotenv(run.ROOT / ".env")
    if MODE == "live":
        os.environ["DEEPSEEK_MODEL"] = DEEPSEEK_MODEL
    names = run.fixture.list_cases() if CASES is None else CASES
    suite = f"debug-{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"
    output = run.ROOT / "evals" / "results" / f"{suite}.jsonl"

    rows = run.run_suite(
        names,
        mode=MODE,
        repeats=REPEATS,
        output=output,
        timeout=RUN_TIMEOUT_SECONDS,
        check_timeout=CHECK_TIMEOUT_SECONDS,
        max_spend=MAX_SPEND_USD,
        prices=PRICES if MODE == "live" else None,
        executor=run.worker,  # Direct call: no fork between debugger and agent.
    )
    print(f"Results: {output}")
    print(run.render_summary(rows))
    return rows


if __name__ == "__main__":
    main()
