"""The compiled graph (once per process), its checkpoint store, one turn
through it, and the thread-state access other layers use instead of importing
the graph."""

from __future__ import annotations

import logging

from langgraph.types import Command
from sqlalchemy.orm import Session

from ....config import settings
from ...llm.langchain_client import read_turn_meter, start_turn_meter
from .routing import build_graph
from .state import AdvisorState

logger = logging.getLogger(__name__)


# ---------------- Graph construction ----------------
_graph = None
_checkpointer = None


def get_checkpointer():
    """Checkpoints go to their own SQLite file under DATA_DIR.

    Persistent rather than in-process because conversations are now named
    things a seller manages in a list: a restart that leaves the list intact
    but every transcript empty is worse than not having the list. Kept in a
    separate file from the business database so a checkpoint-format change
    never touches listings.
    """
    global _checkpointer
    if _checkpointer is None:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        settings.data_path.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(settings.data_path / "advisor_threads.sqlite"), check_same_thread=False
        )
        _checkpointer = SqliteSaver(conn)
        _checkpointer.setup()
    return _checkpointer


def get_graph():
    """Compiled once per process; nodes are stateless so this is safe to share."""
    global _graph
    if _graph is None:
        _graph = build_graph(get_checkpointer())
    return _graph


def reset_graph() -> None:
    """Test hook: rebuild the graph and drop every checkpoint."""
    global _graph, _checkpointer
    _graph = None
    _checkpointer = None


def run_turn(
    db: Session, seller_id: int, message: str, thread_id: str, photo_path: str = ""
) -> AdvisorState:
    """One turn through the graph. Returns the resulting state.

    `router_in`/`router_out` are reset per turn: they are this turn's bill, and
    a checkpointed thread would otherwise keep re-adding every earlier turn's
    tokens to the total.

    What the turn actually cost is metered in `llm.langchain_client` instead of summed out of
    the state, and attached to the returned snapshot as `usage`. The state
    cannot answer the question honestly: `propose_node` spends a model call
    whose return value `interrupt()` throws away, a declined proposal runs the
    graph twice for one turn, and a resumed thread carries the *previous*
    turn's counters in its checkpoint. The meter sees every call either way,
    and starting it here — once, around everything — is what makes the number
    this turn's rather than the thread's.
    """
    start_turn_meter()
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id, "db": db}}

    # A thread paused at a confirm prompt is resumed with this message as the
    # answer, not restarted with it as a new question. Checked against the
    # graph's own state rather than a flag we keep: the graph is the authority
    # on whether it is suspended, and a flag could disagree with it.
    snapshot = graph.get_state(config)
    if snapshot.next:
        logger.info("[react] resuming thread %s at %s", thread_id, snapshot.next)
        resumed = graph.invoke(Command(resume=message), config=config)
        declined = resumed.get("declined_message")
        if not declined:
            resumed["usage"] = read_turn_meter()
            return resumed
        # Not a yes, so the offer is gone — but the seller said something, and
        # it deserves an answer rather than being spent as a "no". Run it as an
        # ordinary turn, which is what it would have been had nothing been
        # pending.
        logger.info("[react] proposal declined; answering the message instead")
        graph.update_state(config, {"declined_message": ""})

    state = graph.invoke(
        {
            "seller_id": int(seller_id),
            "message": message,
            "photo_path": photo_path or "",
            "router_in": 0,
            "router_out": 0,
        },
        config=config,
    )
    # Read after the invoke, not accumulated into the state: `usage` never
    # reaches the checkpoint, so it cannot be mistaken for thread state and
    # re-reported on a later turn.
    state["usage"] = read_turn_meter()
    return state


class _GraphThreadState:
    """Read and patch one thread's checkpointed state from outside the graph.

    Publishing approval is the caller: the seller presses "publish" on a REST
    endpoint, not in a chat turn, and the thread still has to learn that the
    listing went out. It receives this object instead of importing the graph,
    which keeps `publishing` free of any dependency on `graph`."""

    @staticmethod
    def _config(db: Session, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id, "db": db}}

    def read(self, db: Session, thread_id: str) -> dict:
        snapshot = get_graph().get_state(self._config(db, thread_id))
        return snapshot.values or {}

    def patch(self, db: Session, thread_id: str, values: dict) -> None:
        get_graph().update_state(self._config(db, thread_id), values)


THREAD_STATE = _GraphThreadState()
