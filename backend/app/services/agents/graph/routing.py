"""Graph topology: the nodes, the edges between them, and the three branch
decisions. Node functions are read off `nodes` when the graph is built, so a
test that swaps one out before the first build gets its replacement wired in."""

from __future__ import annotations

from . import nodes
from .state import AdvisorState


def _pick(state: AdvisorState) -> str:
    return state.get("route") or "clarify"


def _after_propose(state: AdvisorState) -> str:
    return "act" if state.get("pending_action") else "end"


def _after_compliance(state: AdvisorState) -> str:
    """A blocked or failed submission stops here — extraction is only for
    things that may actually be listed."""
    return "stop" if state.get("publish_verdict") in ("block", "error") else "extract"


def build_graph(checkpointer):
    """Wire the nodes together and compile against `checkpointer`."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(AdvisorState)
    builder.add_node("route", nodes.route_node)
    builder.add_node("sales_agent", nodes.sales_node)
    builder.add_node("status_change", nodes.status_change_node)
    builder.add_node("propose", nodes.propose_node)
    builder.add_node("act", nodes.act_node)
    builder.add_node("defer_sale", nodes.defer_sale_node)
    builder.add_node("publishing_agent", nodes.publishing_node)
    builder.add_node("publish_compliance", nodes.publish_compliance_node)
    builder.add_node("publish_extract", nodes.publish_extract_node)
    builder.add_node("clarify", nodes.clarify_node)
    builder.add_node("item_not_found", nodes.item_not_found_node)
    builder.add_node("listing_done", nodes.listing_done_node)
    builder.add_node("degraded", nodes.degraded_node)
    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route",
        _pick,
        {
            "sales_agent": "sales_agent",
            "status_change": "status_change",
            "defer_sale": "defer_sale",
            "publishing_agent": "publishing_agent",
            "clarify": "clarify",
            "item_not_found": "item_not_found",
            "listing_done": "listing_done",
            "degraded": "degraded",
        },
    )
    builder.add_edge("publishing_agent", "publish_compliance")
    builder.add_conditional_edges(
        "publish_compliance", _after_compliance,
        {"extract": "publish_extract", "stop": END},
    )
    builder.add_edge("sales_agent", "propose")
    builder.add_conditional_edges("propose", _after_propose, {"act": "act", "end": END})
    # The status-change gate ends in the same `act` node as a push: one
    # place where a confirmed action is carried out, one place where its
    # outcome is written back into the reply.
    builder.add_conditional_edges(
        "status_change", _after_propose, {"act": "act", "end": END}
    )
    for node in (
        "act", "publish_extract", "defer_sale", "clarify",
        "item_not_found", "listing_done", "degraded",
    ):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)
