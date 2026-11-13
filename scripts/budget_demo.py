"""Watch a batch of tool calls get answered, and watch budgets stop a run.

    python scripts/budget_demo.py --batch        # 3 calls, 1 good 2 broken
    python scripts/budget_demo.py --tool-limit   # budget dies at call 2 of 3
    python scripts/budget_demo.py --token-limit  # stops BEFORE paying
    python scripts/budget_demo.py --all

No API key, no network, no money: the model is scripted (Session 4, step 0).
That is the point - every stopping rule in Session 5 is a rule you can watch
fire without buying a single token.

The number to watch in --token-limit is `model calls made`. A limit that stops
the run after the response has arrived is not a limit, it is a receipt.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.budget import Budget, Pricing
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.policy import workspace_root

# DeepSeek's published rates are NOT hard-coded anywhere in agent/. These are
# here only so the demo can print a non-None cost; a real run reads PRICE_*
# from .env (D37).
DEMO_PRICES = Pricing(
    input_cache_hit_per_m=0.07,
    input_cache_miss_per_m=0.27,
    output_per_m=1.10,
)


class ScriptedModel:
    def __init__(self, *responses: ModelResponse) -> None:
        self.script = list(responses)
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        if self.calls >= len(self.script):
            raise AssertionError("the loop wanted another call; script exhausted")
        response = self.script[self.calls]
        self.calls += 1
        return response


def call(call_id: str, name: str = "list_files", arguments: str = '{"path": "src"}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def wants(*calls, prompt=400, completion=60) -> ModelResponse:
    return ModelResponse(
        text=None,
        model="scripted",
        finish_reason="tool_calls",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        duration_ms=0.0,
        tool_calls=list(calls),
        prompt_cache_hit_tokens=prompt // 2,
        prompt_cache_miss_tokens=prompt - prompt // 2,
    )


def says(text: str, prompt=500, completion=40) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="scripted",
        finish_reason="stop",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        duration_ms=0.0,
        prompt_cache_hit_tokens=prompt // 2,
        prompt_cache_miss_tokens=prompt - prompt // 2,
    )


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def show(result, model: ScriptedModel) -> None:
    print(f"\n  stop_reason     {result.stop_reason}")
    print(f"  model calls     {model.calls}   <- what you paid for")
    print(f"  tool calls run  {result.tool_calls}")
    cost = result.cost_usd
    print(f"  tokens          {result.total_tokens}"
          f"   est. {'unpriced' if cost is None else f'${cost:.6f}'}")
    print(f"  budget          {result.budget.describe()}")
    print("\n  conversation:")
    for message in result.messages:
        role = message["role"]
        if role == "assistant":
            asked = [c["id"] for c in message.get("tool_calls") or []]
            detail = f"asks for {len(asked)} tool call(s): {asked}" if asked else (
                f"answers: {message['content']!r}")
            print(f"    assistant  {detail}")
        else:
            first = message["content"].splitlines()[0]
            print(f"    tool       id={message['tool_call_id']!r:<8} {first[:64]}")
    ids = [m["tool_call_id"] for m in result.messages if m["role"] == "tool"]
    asked_ids = [
        c["id"]
        for m in result.messages
        if m["role"] == "assistant"
        for c in (m.get("tool_calls") or [])
    ]
    print(f"\n  ids asked for   {asked_ids}")
    print(f"  ids answered    {ids}")
    print(f"  every id answered: {asked_ids == ids}")


def demo_batch(root: Path) -> None:
    banner("A batch of three: one valid, one malformed JSON, one unknown tool")
    print("""
Three calls arrive in ONE assistant turn. Two of them are broken. The question
is not whether the broken ones fail - Session 4 settled that - but whether a
broken call takes the rest of the batch down with it. Every id must come back.
""".strip())
    model = ScriptedModel(
        wants(
            call("call_aaa"),
            call("call_bbb", arguments="{not json"),
            call("call_ccc", name="delete_everything"),
        ),
        says("src holds app.py and utils.py; two of my calls were rejected."),
    )
    show(run_loop([], root=root, model_fn=model), model)


def demo_tool_limit(root: Path) -> None:
    banner("The budget dies at call 2 of 3 (D33)")
    print("""
--max-tool-calls=1, and the model asks for three. The run must stop - but it
cannot stop in the MIDDLE of a batch: an assistant turn with three tool_calls
is a promise of three tool messages, and breaking it means the next POST comes
back 400 with an error about nothing to do with budgets.

So calls 2 and 3 are answered with a refusal that names the limit, and the
stop happens after the batch.
""".strip())
    model = ScriptedModel(wants(call("call_aaa"), call("call_bbb"), call("call_ccc")))
    show(
        run_loop([], root=root, budget=Budget(max_tool_calls=1), model_fn=model),
        model,
    )


def demo_token_limit(root: Path) -> None:
    banner("A token budget stops BEFORE the call, not after it (D35/D36)")
    print("""
Budget: 1200 tokens, reserve 400. Each step here costs 460.

  step 1   spends 460.  460 + 400 reserve = 860, under 1200 -> step 2 runs.
  step 2   spends 460.  920 + 400 reserve = 1320, over 1200 -> STOP.

The run stops at 920 tokens of a 1200 budget. It left 280 on the table and
that is the point: the response it refused to ask for could have cost
anything, and its usage only exists once it has been paid for. The reserve is
the price of finding out safely - and it bounds the overshoot at one response.

Watch `model calls`: 2, not 3. The third response is still sitting in the
script, unpaid for.
""".strip())
    model = ScriptedModel(
        wants(call("call_aaa")),
        wants(call("call_bbb")),
        says("never reached - this response was never paid for"),
    )
    budget = Budget(max_total_tokens=1200, token_reserve=400, pricing=DEMO_PRICES)
    show(run_loop([], root=root, budget=budget, model_fn=model), model)


def main(argv: list[str]) -> int:
    root = workspace_root()
    flags = set(argv[1:]) or {"--all"}
    if flags & {"--all", "--batch"}:
        demo_batch(root)
    if flags & {"--all", "--tool-limit"}:
        demo_tool_limit(root)
    if flags & {"--all", "--token-limit"}:
        demo_token_limit(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
