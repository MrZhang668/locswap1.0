"""Row constructor shared by every dataset part.

Kept separate from `dataset.py` so the parts can import it without a cycle:
parts append into `ROWS`, `dataset` imports the parts and re-exports the list.
"""

from __future__ import annotations

from taxonomy import LEAVES

# Real uploaded photos already on disk — reused so the publishing branch gets a
# loadable image without this script uploading anything new.
PHOTO_A = "uploads/a25e011e6ece4f9ea2cc4bf0d5377578.jpg"
PHOTO_B = "uploads/eebc94cb85ff4a269a115006ddced2af.jpg"
PHOTO_C = "uploads/02c830d022e84c618b546c93c9dcccdc.jpg"

ROWS: list[dict] = []


def Q(leaf: str, text: str, style: str = "clear", cat: str = "", quality: str = "good",
      seller: str = "multi", photo: str = "", ctx: str = "", note: str = "",
      agent: str | None = None, scn: str | None = None, conf: str = "high",
      second: str | None = None, req: tuple | None = None, ok: tuple | None = None) -> None:
    """Append one labelled query.

    `agent` / `scn` default to the leaf's gold label; pass them only for rows
    where the leaf's usual answer does not apply (a seller with nothing listed,
    say). `conf="review"` marks a row whose gold label the prompt does not
    settle on its own — those are the rows the human pass has to rule on.

    `second` is the second intent carried by the same message when the route can
    only serve the first one (a seller with nothing listed who asks to list an
    item *and* asks what it is worth). It is not a second gold label — the route
    stays single-valued — it is what the 多意图保全率 metric reads the reply
    against.

    `req` / `ok` override the leaf's tool expectation for a row whose wording
    asks for something the leaf's usual answer does not.
    """
    spec = LEAVES[leaf]
    ROWS.append(dict(
        leaf=leaf, family=spec["family"], leaf_cn=spec["cn"],
        text=text, style=style, cat=cat, quality=quality, seller=seller,
        photo=photo, ctx=ctx, note=note,
        gold_agent=spec["agent"] if agent is None else agent,
        gold_scenario=spec["scn"] if scn is None else scn,
        label_conf=conf,
        # Single-hop unless the leaf declares an ideal sequence (family F only).
        gold_seq=spec.get("seq") or (spec["agent"] if agent is None else agent),
        second_intent=spec.get("second", "") if second is None else second,
        tools_req=spec["tools_req"] if req is None else req,
        tools_ok=spec["tools_ok"] if ok is None else ok,
    ))
