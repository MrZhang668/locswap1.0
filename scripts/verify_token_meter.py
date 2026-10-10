"""Acceptance for the per-turn token meter.

The number under each answer is a bill, so the thing worth testing is not that
it renders — it is that it is *this turn's* and *all* of this turn's:

**Every call counts.** Routing, the sales tool loop and the proposal check are
three different nodes, and two of them never report their cost up through the
graph result. The meter sits under `record_usage`, which all of them pass
through, so the total is complete rather than "whatever the result happened to
carry".

**A turn is not a thread.** A thread is checkpointed, so last turn's counters
are still sitting in the state when the next turn starts. The meter is reset
per `run_turn` call and never checkpointed, which is what keeps the second
question from being billed for the first.

**One seller is not another.** Sync endpoints run in a threadpool, so two turns
are genuinely concurrent. The meter is a ContextVar, and this asserts that
concurrency directly rather than trusting the type.

Offline: `build_chat_model` is stubbed, so no key and no network are needed —
but the stub is placed *below* `record_usage`, which is the whole point. A fake
that replaced the advisor client (the way the older scripts do) would bypass
the meter and prove nothing.

    python scripts/verify_token_meter.py
"""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.services.agents import graph as graph_mod  # noqa: E402
from app.services.agents import lc_llm  # noqa: E402
from app.services.agents import router as router_mod  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


def usage_msg(in_tok: int, out_tok: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata={
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "input_token_details": {},
        }
    )


# ---------------- A chat model that books usage but talks to nobody ----------------
class FakeChat:
    """Stands in for the LangChain chat model at `build_chat_model`.

    Below `record_usage`, deliberately: the meter reads usage off the message
    this returns, so stubbing here exercises the accounting path instead of
    stepping over it.

    The two paths are priced differently so the assertions can tell the call
    sites apart — if every call billed the same, a total would not distinguish
    "both counted" from "one counted twice". Which path stands for which site
    is not a guess: the router and the proposal step are the structured-output
    callers, and the advisor's tool loop is the only one that calls `invoke`.
    Splitting on the model *name* would not work here — this deployment points
    the light model at the main one.
    """

    ROUTER_IN, ROUTER_OUT = 100, 7
    ADVISOR_IN, ADVISOR_OUT = 1000, 70

    def __init__(self, decision: object, advisor_reply: str):
        self.decision, self.advisor_reply = decision, advisor_reply

    def _msg(self, content, in_tok, out_tok):
        msg = usage_msg(in_tok, out_tok)
        msg.content = content
        msg.tool_calls = []
        return msg

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, include_raw=False):
        def invoke(_msgs):
            return {
                "raw": self._msg("", self.ROUTER_IN, self.ROUTER_OUT),
                # A proposal schema asked of a router decision would not
                # validate, so hand each caller the object it expects.
                "parsed": self.decision if schema is router_mod.RouteDecision else None,
                "parsing_error": None,
            }

        return SimpleNamespace(invoke=invoke)

    def invoke(self, _messages):
        return self._msg(self.advisor_reply, self.ADVISOR_IN, self.ADVISOR_OUT)


class use_fake_models:
    """Pin every model in the stack to a fixed, countable cost."""

    ROUTER_IN, ROUTER_OUT = FakeChat.ROUTER_IN, FakeChat.ROUTER_OUT
    ADVISOR_IN, ADVISOR_OUT = FakeChat.ADVISOR_IN, FakeChat.ADVISOR_OUT

    def __init__(self, route_decision, advisor_reply: str):
        self.route_decision, self.advisor_reply = route_decision, advisor_reply

    def __enter__(self):
        outer = self

        def fake_build(*, model, temperature, max_tokens):
            return FakeChat(outer.route_decision, outer.advisor_reply)

        self._orig_build = lc_llm.build_chat_model
        self._orig_router_build = router_mod.build_chat_model
        self._orig_graph_conf = graph_mod.llm_configured
        self._orig_lc_conf = lc_llm.llm_configured
        lc_llm.build_chat_model = fake_build
        router_mod.build_chat_model = fake_build
        graph_mod.llm_configured = lambda: (True, "")
        lc_llm.llm_configured = lambda: (True, "")
        return self

    def __exit__(self, *a):
        lc_llm.build_chat_model = self._orig_build
        router_mod.build_chat_model = self._orig_router_build
        graph_mod.llm_configured = self._orig_graph_conf
        lc_llm.llm_configured = self._orig_lc_conf


def meter_accumulates() -> None:
    section("the meter adds up what record_usage sees")
    lc_llm.start_turn_meter()
    msg = usage_msg(30, 4)
    lc_llm.record_usage(lc_llm.CALL_SITE_ROUTER, "m", msg)
    lc_llm.record_usage(lc_llm.CALL_SITE_ROUTER, "m", msg)
    lc_llm.record_usage("C.seller_advisor", "m", msg)
    m = lc_llm.read_turn_meter()
    check("totals sum across calls",
          m["input_tokens"] == 90 and m["output_tokens"] == 12, str(m))
    check("call count is kept", m["calls"] == 3, str(m["calls"]))
    check("split is per call site",
          m["by_call_site"][lc_llm.CALL_SITE_ROUTER]["calls"] == 2
          and m["by_call_site"]["C.seller_advisor"]["calls"] == 1,
          str(m["by_call_site"]))

    # A turn is a turn: restarting must not carry the last one forward.
    lc_llm.start_turn_meter()
    check("start_turn_meter resets to zero",
          lc_llm.read_turn_meter() == {"input_tokens": 0, "output_tokens": 0,
                                       "calls": 0, "by_call_site": {}})

    # The read must be a copy — a caller that stashed it must not keep growing.
    lc_llm.record_usage(lc_llm.CALL_SITE_ROUTER, "m", msg)
    snapshot = lc_llm.read_turn_meter()
    lc_llm.record_usage(lc_llm.CALL_SITE_ROUTER, "m", msg)
    check("a read snapshot does not keep mutating",
          snapshot["input_tokens"] == 30, str(snapshot))
    check("nor do its per-call-site rows",
          snapshot["by_call_site"][lc_llm.CALL_SITE_ROUTER]["calls"] == 1,
          str(snapshot["by_call_site"]))


def unmetered_is_not_an_error() -> None:
    section("no meter running — scripts and tests are not a bug")
    lc_llm._turn_meter.set(None)
    in_tok, out_tok = lc_llm.record_usage(lc_llm.CALL_SITE_ROUTER, "m", usage_msg(5, 1))
    check("record_usage still returns the usage", (in_tok, out_tok) == (5, 1))
    check("reading an unstarted meter gives zeros, not a crash",
          lc_llm.read_turn_meter()["calls"] == 0)


def isolated_between_threads() -> None:
    section("concurrent turns are billed separately")
    seen: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def turn(name: str, n: int) -> None:
        lc_llm.start_turn_meter()
        barrier.wait()   # both meters are live at the same time
        lc_llm.record_usage("C.seller_advisor", "m", usage_msg(n, 0))
        barrier.wait()
        seen[name] = lc_llm.read_turn_meter()

    threads = [threading.Thread(target=turn, args=("a", 11)),
               threading.Thread(target=turn, args=("b", 22))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("each thread sees only its own tokens",
          seen["a"]["input_tokens"] == 11 and seen["b"]["input_tokens"] == 22, str(seen))


def whole_turn() -> None:
    """The real question: does one seller message produce one complete bill."""
    section("end to end — router + tool loop land in one turn's total")
    from app.db import SessionLocal
    from app.models import Item, ItemStatus, User

    with SessionLocal() as db:
        item = (
            db.query(Item).filter(Item.status.in_(tuple(ItemStatus.SEARCHABLE)))
            .order_by(Item.id).first()
        )
        if item is None:
            print("  [skip] no active listing in the database — seed first")
            return
        seller_id = item.seller_id
        seller = db.get(User, seller_id)
        print(f"  seller #{seller_id} ({getattr(seller, 'email', '?')}), item #{item.id}")

    decision = router_mod.RouteDecision(
        agent="sales_agent", scenario="none",
        rewritten_query="谁在找我的东西", clarify_question="",
    )
    final = json.dumps({"reply": "这是分析结果。", "pricing": None}, ensure_ascii=False)
    F = use_fake_models

    graph_mod.reset_graph()
    with use_fake_models(decision, final), SessionLocal() as db:
        state = graph_mod.run_turn(db, seller_id, "有人在找我的东西吗？", "meter-thread-1")

    usage = state.get("usage") or {}
    by_site = usage.get("by_call_site", {})
    expected_in = F.ROUTER_IN + F.ADVISOR_IN
    expected_out = F.ROUTER_OUT + F.ADVISOR_OUT
    check("the routing call is in the total",
          by_site.get(lc_llm.CALL_SITE_ROUTER, {}).get("input") == F.ROUTER_IN, str(usage))
    check("the sales loop is in the total",
          by_site.get("C.seller_advisor", {}).get("input") == F.ADVISOR_IN, str(usage))
    check("turn total = router + advisor",
          usage.get("input_tokens") == expected_in
          and usage.get("output_tokens") == expected_out,
          f"{usage.get('input_tokens')}/{usage.get('output_tokens')} "
          f"!= {expected_in}/{expected_out}")
    check("call count matches", usage.get("calls") == 2, str(usage.get("calls")))

    # Second turn on the *same* thread: the checkpoint still holds turn one's
    # router_in/router_out, so this is where a state-summed total would
    # double-bill.
    with use_fake_models(decision, final), SessionLocal() as db:
        state2 = graph_mod.run_turn(db, seller_id, "那定价呢？", "meter-thread-1")
    usage2 = state2.get("usage") or {}
    check("turn two is billed on its own, not cumulatively",
          usage2.get("input_tokens") == expected_in, str(usage2.get("input_tokens")))

    checkpointed = graph_mod.get_graph().get_state(
        {"configurable": {"thread_id": "meter-thread-1"}}
    ).values
    check("usage never reaches the checkpoint", "usage" not in checkpointed,
          "usage leaked into checkpointed state")


def degraded_turn_is_free() -> None:
    section("a turn that ran no model reports zero, with zero calls")
    from app.db import SessionLocal

    orig = graph_mod.llm_configured
    graph_mod.llm_configured = lambda: (False, "no key")
    graph_mod.reset_graph()
    try:
        with SessionLocal() as db:
            state = graph_mod.run_turn(db, 1, "在吗", "meter-degraded-1")
    finally:
        graph_mod.llm_configured = orig
        graph_mod.reset_graph()
    usage = state.get("usage") or {}
    check("degraded turn costs nothing",
          usage.get("input_tokens") == 0 and usage.get("calls") == 0, str(usage))


def main() -> int:
    print("=" * 66)
    print("per-turn token meter — acceptance")
    print("=" * 66)
    meter_accumulates()
    unmetered_is_not_an_error()
    isolated_between_threads()
    whole_turn()
    degraded_turn_is_free()
    print("\n" + "=" * 66)
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
