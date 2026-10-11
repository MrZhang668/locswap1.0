"""Agent orchestration layer (LangGraph + LangChain).

`graph` owns the route-then-specialize flow and the per-thread checkpoint;
`router` is call site D and `defer_intent` call site H; `proposals` holds the
two confirmed write actions; `sessions` is the named-conversation registry.
The specialists the graph hands off to live beside them: `sales` (call site C),
`publishing` (E and F) and `planning` (G).

Deliberately imports nothing. Importing a subpackage — even just
`publishing.schemas` — runs this file first, and pulling the compiled graph in
from here would drag the whole agent stack along with every schema import and
close import cycles between the specialists and the graph. Import from the
submodule you need, e.g. `from app.services.agents.graph import run_turn`.
"""
