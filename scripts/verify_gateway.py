"""Can we delete the fastllm workaround yet?

Run this while SIS changes their serving config. It sends the request our app
would send with our patch removed, and reports PASS or FAIL.

What it sends is the real structured chat turn: the real system prompt built by
`backend.chat.structured`, the real `TURN_SCHEMA`, posted by the real client in
`backend/llm/base.py`. The only thing changed is that the three fastllm
overrides are stripped, so this is the gateway's behaviour without our help:

    adjust_system   neutered  -- no harmony "answer in the final channel" suffix
    vary_on_retry   neutered  -- the seed stays pinned at 42
    response_format restored  -- the strict schema goes back in the request,
                                 not into the prompt

Each trial uses a different question, and moves the seed. Both matter, and
the question matters more. This gateway's failure is prompt-deterministic:
on a prompt that provokes it, every seed returns the same leaked analysis
line, byte for byte. So N trials of one pinned question is one draw counted
N times, however much you stir the seed -- which is exactly how an earlier
version of this script reported a false 0%. CASES is the sample.

Retries are off outside --control, so a trial is one request and one draw.
That matters: the defect is probabilistic across prompts (the baseline was 2
usable replies in 12), so a single PASS means nothing and only a run of N
clean trials is evidence.

A trial passes when the reply parses as JSON and matches the turn contract:
exactly one key, `tool_calls`, holding one to four `{tool, args_json}` objects
naming real tools. Anything else -- prose, empty content, a 500 -- is a FAIL
with the gateway's own words printed next to it.

    python scripts/verify_gateway.py                 # 6 trials
    python scripts/verify_gateway.py --trials 12     # match the runbook numbers
    python scripts/verify_gateway.py --model "GPT 120b"

Exit code is 0 only when every trial passes.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.chat import structured
from backend.llm import base, fastllm

#: The sample. Briefs and questions both vary, because both move this
#: gateway; a long brief provokes the analysis leak more often than a short
#: one. Add real questions here rather than raising --trials, which only
#: recycles this list.
CASES = [
    ("Reservoir Engineer screening.",
     "Which candidates are in stage 1? Use a tool to find out."),
    ("You are reviewing a screening for a Reservoir Engineer position.",
     "Which candidates are in stage 1? Use a tool to find out."),
    ("You are reviewing a screening for a Reservoir Engineer position.",
     "Who are the top five candidates?"),
    ("Reservoir Engineer screening.",
     "How many candidates meet every required qualification?"),
    ("You are reviewing a screening for a Reservoir Engineer position.",
     "Compare stage 1 and stage 2 candidates."),
    ("Reservoir Engineer screening.",
     "Show me the candidates ranked by score."),
]

#: Added to the pinned seed per trial, so a repeated question is at least a
#: fresh draw. Not a multiple of fastllm.RETRY_SEED_STRIDE, so a trial's
#: retries cannot land on another trial's seed and quietly repeat it.
SEED_STRIDE = 7919


class UnpatchedFastLLM(fastllm.FastLLMProvider):
    """The provider as it would be with the workaround deleted."""

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.supports_json_schema = True

    def adjust_system(self, system_text: str, *, structured: bool) -> str:
        return system_text

    def vary_on_retry(self, body: dict[str, Any], attempt: int) -> None:
        return None


def check(reply: Any) -> str:
    """Return "" when the reply satisfies the turn contract, else why not."""
    if not isinstance(reply, dict):
        return f"not an object: {type(reply).__name__}"
    if set(reply) != {"tool_calls"}:
        return f"wrong keys: {sorted(reply)}"
    calls = reply["tool_calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 4:
        return "tool_calls is not a list of one to four calls"
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"tool", "args_json"}:
            return "a call is not exactly {tool, args_json}"
        if call["tool"] not in structured.TOOLS:
            return f"unknown tool {call['tool']!r}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=6)
    ap.add_argument("--model", default="")
    ap.add_argument("--control", action="store_true",
                    help="run WITH the workaround and retries, as a known-good "
                         "comparison; proves the check can report success")
    args = ap.parse_args()

    provider = fastllm.FastLLMProvider(args.model) if args.control         else UnpatchedFastLLM(args.model)
    if not provider.is_configured():
        print("FAIL  the fastllm gateway is not configured (no base_url)")
        return 2
    model = args.model or provider.default_model
    if not model:
        print("FAIL  no model: pass --model, or set LLM_MODEL")
        return 2

    if not args.control:
        base.MAX_RETRIES = 0      # one request per trial; no rescue attempts

    print(f"gateway : {provider.completions_url()}")
    print(f"model   : {model}")
    shape = "workaround ON, retries ON (control)" if args.control else "workaround removed"
    print(f"sending : structured turn, {shape} ({args.trials} trials)")
    print()

    passed = 0
    for i in range(1, args.trials + 1):
        brief, question = CASES[(i - 1) % len(CASES)]
        system = structured.system_prompt(brief)
        messages = [{"role": "user", "content": question}]
        provider.seed = fastllm.DEFAULT_SEED + SEED_STRIDE * (i - 1)
        started = time.monotonic()
        try:
            reply = provider.chat(messages, schema=structured.TURN_SCHEMA,
                                  model=model, system=system, schema_name="ChatTurn")
            problem = check(reply)
        except Exception as exc:                      # LLMError and transport alike
            problem = str(exc).replace("\n", " ")[:150]
        elapsed = f"{time.monotonic() - started:5.1f}s"
        passed += not problem
        print(f"  trial {i:>2}  {'PASS' if not problem else 'FAIL'}  {elapsed}  {question[:38]:<38} {problem}")

    print(f"\n{passed}/{args.trials} trials produced a valid turn ({passed / args.trials:.0%})")
    # The control is not expected to be perfect. Our patch measured roughly
    # 5 in 6 when it was written, so demanding 6/6 of it would report a
    # failure on a healthy day and mislead whoever is reading this mid-call.
    # The unpatched run is held to the real bar, because 'the workaround can
    # go' is a claim that one bad draw should be allowed to veto.
    if args.control:
        if passed >= max(1, round(args.trials * 0.5)):
            print('PASS  control healthy: the check works, and our patch still carries it.')
            return 0
        print('FAIL  the control fell below half; suspect the gateway is down, not fixed.')
        return 1
    if passed == args.trials:
        print('PASS  the gateway holds the contract unaided; the workaround can go.')
        return 0
    print('FAIL  not fixed yet; keep the workaround in backend/llm/fastllm.py.')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
