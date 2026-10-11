"""The agent graph: route once, then hand off to one specialist (LangGraph).

    START -> route -> sales_agent -> propose -(nothing to propose)-> END
                                             -(interrupt: seller answers)-> act -> END
                   -> status_change -(interrupt: seller confirms)-> act -> END
                   -> publishing_agent -> publish_compliance -> (block) -> END
                                                             -> publish_extract -> END
                   -> defer_sale -> END
                   -> clarify    -> END
                   -> item_not_found -> END   (named something they do not own)
                   -> listing_done   -> END   ("uploaded it" — and it is already live)
                   -> degraded   -> END

Why a graph rather than another hand-written branch:
* the conversation is checkpointed per thread, so history stops being something
  the browser posts back and starts being state the server owns;
* the routing decision, the rewritten query and the specialist's result are all
  in one inspectable state object rather than spread across local variables;
* the publishing flow is a multi-step pipeline with a real branch — a blocked
  submission must not reach extraction at all — which is exactly what this
  structure is for.

What deliberately did **not** move into the graph: the seller advisor's tool
loop. Its Pydantic validation, per-item ownership resolution, round caps,
result caching and server-side suppression of ungrounded output are the safety
properties of this feature. They stay in `agents.sales.orchestrator`, and the
node here just calls it.

The SQLAlchemy session travels in `config["configurable"]`, never in the state:
state gets serialized into the checkpoint, and a live DB session must not.

Layout: `state` (AdvisorState), `nodes` (one function per step), `routing`
(topology and branch decisions), `runtime` (compile once, checkpointer,
`run_turn`, thread-state access for callers outside the graph).
"""

from .runtime import THREAD_STATE, get_checkpointer, get_graph, reset_graph, run_turn
from .state import AdvisorState

__all__ = [
    "AdvisorState",
    "THREAD_STATE",
    "get_checkpointer",
    "get_graph",
    "reset_graph",
    "run_turn",
]
