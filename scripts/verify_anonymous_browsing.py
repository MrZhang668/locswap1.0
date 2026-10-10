"""Acceptance for signed-out browsing.

Opening the door is easy to do and easy to do too much of, so this checks the
line from both sides against a running backend:

**A visitor can browse, really browse.** Not a teaser — the same board, the same
search, the same item pages, with distances on the cards. A marketplace listing
without "how far away is it" is just a list, so the distance is measured from
the campus centre and labelled `distance_from: "campus"` rather than dropped.

**A visitor cannot do anything that belongs to an account.** Reserving, listing,
saving a want, reading a cart or notifications, and everything on the seller
side all still answer 401 with no token. This half is the one that matters: the
failure mode of an auth change is not a missing feature, it is a write that
should have needed a login.

Needs the backend running on :8010. No API key needed for the open endpoints.

    python scripts/verify_anonymous_browsing.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401  injects backend into sys.path

import os

# Point at a deployed site with LOCSWAP_BASE=http://<server> (through nginx).
BASE = os.environ.get("LOCSWAP_BASE", "http://127.0.0.1:8010").rstrip("/")
_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


#: Magic bytes for the things that must never be downloadable. Checked against
#: the response body because a status code cannot tell "refused" from "the
#: single-page app answered".
_SIGNATURES = {
    b"SQLite format 3": "a SQLite database",
    b"PK": "a zip/xlsx archive",
}


def _looks_like_a_file(body) -> str:
    """Name what the response body actually is, when it is something that should
    not have been served. Returns "" when the body is harmless."""
    raw = body if isinstance(body, (bytes, bytearray)) else None
    if raw is None:
        # `call` returns a dict for JSON and a byte count for anything else; a
        # dict is never a leaked file.
        return ""
    for sig, name in _SIGNATURES.items():
        if raw.startswith(sig):
            return name
    # A log file is plain text with no signature — look for its shape instead.
    text = raw[:400].decode("utf-8", "ignore")
    if "INFO " in text and "app.services" in text:
        return "a server log"
    return ""


def call(path: str, body=None, token: str | None = None, method: str | None = None):
    """Returns (status, payload). Never raises on an HTTP error — a 401 is a
    result here, not a failure."""
    m = method or ("POST" if body is not None else "GET")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=m,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if not raw:
                return r.status, {}
            # Not every response is JSON — an image is the whole point of one of
            # these checks. Parsing unconditionally turned a perfectly good 200
            # into a caught exception and reported it as status 0, i.e. a
            # passing endpoint looked like a broken one.
            if "json" not in (r.headers.get("Content-Type") or ""):
                # Raw, so callers can inspect what it actually is.
                return r.status, raw
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:160]


#: Everything a signed-out visitor must still be refused. Each entry is a real
#: request, not a route string — the point is what the server does, not what the
#: source says it does.
LOCKED = [
    ("reserve an item", "POST", "/api/reservations/1", None),
    ("cancel a reservation", "DELETE", "/api/reservations/1", None),
    ("read someone's cart", "GET", "/api/cart", None),
    ("list an item for sale", "POST", "/api/items", {
        "name": "x", "description": "x", "summary": "x", "price": 1.0,
        "attributes": {"category": "电子产品", "condition": "成色良好"},
        "pickup_location_name": "The Marylander (3501 St Paul St)",
        "pickup_time_start": "a", "pickup_time_end": "b"}),
    ("upload a photo", "POST", "/api/items/photo", None),
    ("save a want", "POST", "/api/wants", {"text": "a bike"}),
    ("read wants", "GET", "/api/wants", None),
    ("read notifications", "GET", "/api/notifications", None),
    ("read the seller's own listings", "GET", "/api/seller/items", None),
    ("push a listing to a buyer", "POST", "/api/seller/items/1/push", {"card_id": "candidate-1"}),
    ("talk to the sales advisor", "POST", "/api/seller/advisor/chat", {"message": "你好"}),
    ("read the account", "GET", "/api/auth/me", None),
]


def locked_routes_from_the_app() -> list[str]:
    """Every AUTH route, taken from the app rather than from a hand-written list.

    The hand-written list above is the readable part — it names the actions in
    the words a person would use. This is the completeness check behind it: a
    new endpoint that requires a login should not be able to appear without
    anyone noticing, and a route whose path was guessed wrong shows up here as a
    mismatch instead of passing as a 404.
    """
    import app.main as m

    out = []
    for r in m.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api"):
            continue
        names: set[str] = set()

        def walk(d):
            if d is None:
                return
            call_ = getattr(d, "call", None)
            if call_ is not None:
                names.add(getattr(call_, "__name__", ""))
            for sub in getattr(d, "dependencies", []):
                walk(sub)

        walk(getattr(r, "dependant", None))
        if "get_current_user" in names:
            out.append(path)
    return sorted(set(out))


def main() -> int:
    status, _ = call("/api/meta/config")
    if status != 200:
        print(f"backend not answering on {BASE} (status {status}) — start it first")
        return 2

    section("a visitor can browse")
    status, board = call("/api/items/browse?limit=6")
    check("the board loads with no token", status == 200, str(board)[:120])
    cards = (board or {}).get("items") or []
    check("and it has items on it", len(cards) > 0, f"{len(cards)} cards")

    if cards:
        card = cards[0]
        check("cards carry a distance", card.get("distance_km") is not None, str(card)[:140])
        check("labelled as measured from campus, not from the visitor",
              card.get("distance_from") == "campus", str(card.get("distance_from")))
        check("no reservation is attributed to nobody",
              card.get("reserved_by_me") in (False, None), str(card.get("reserved_by_me")))
        status, one = call(f"/api/items/{card['item_id']}")
        check("an item page opens", status == 200, str(one)[:120])
        check("and it is the same item", (one or {}).get("item_id") == card["item_id"])
        status, _ = call(f"/api/items/{card['item_id']}/click", body={})
        # Clicks must count for visitors: impressions do, and a click-through
        # rate built from one but not the other is an undercount the seller is
        # shown as a fact about their listing.
        check("opening a card counts as a click", status == 200, str(status))

    status, res = call("/api/search/chat", {"message": "机械键盘",
                                            "history": [], "clarify_round": 0})
    check("search answers a visitor", status == 200, str(res)[:140])
    found = (res or {}).get("cards") or []
    check("and returns results", len(found) > 0, f"{len(found)} cards")
    check("a visitor's search leaves no standing want behind",
          (res or {}).get("want_id") in (None, 0), str((res or {}).get("want_id")))

    status, mp = call("/api/search/map")
    check("the map loads for a visitor", status == 200, str(mp)[:120])

    section("a visitor cannot act")
    for label, method, path, body in LOCKED:
        status, _ = call(path, body=body, method=method)
        # 401 is the answer we want. A 422 would mean the request was accepted
        # for processing and only then failed on its shape — the auth gate has
        # to come first.
        check(f"401 on: {label}", status == 401, f"got {status}")

    section("the static mount exposes photos and nothing else")
    # This was a real hole, found while preparing to deploy: /static was mounted
    # on DATA_DIR, so one GET returned the whole database. Invisible on
    # localhost, a full breach in public. It stays a test because the fix is one
    # line that someone could reasonably "simplify" back.
    status, _ = call("/api/items/browse?limit=1")
    secrets = [
        ("the database", "/static/locswap.db"),
        ("the advisor's conversation store", "/static/advisor_threads.sqlite"),
        ("the seed spreadsheet", "/static/items_dataset.xlsx"),
        ("the server log", "/static/backend_server.log"),
        ("the data directory itself", "/static/"),
        ("a path traversal out of uploads", "/static/uploads/../locswap.db"),
    ]
    for label, path in secrets:
        status, body = call(path)
        # Judged on what comes back, not on the status code. Behind nginx the
        # single-page app's `try_files ... /index.html` answers every unknown
        # path with 200 and the app shell, so a status-only assertion reports a
        # breach that is not one — and, worse, would pass if a future config
        # ever served the file with some other 2xx.
        leaked = _looks_like_a_file(body)
        check(f"not served: {label}", not leaked,
              f"{path} returned {status} with {leaked}")

    # The same probe, proved from the other side: the photo endpoint really does
    # return an image, so "nothing is served" above is not just everything being
    # broken.

    # ...while the photos it is there for still load.
    status, board = call("/api/items/browse?limit=4")
    photo = next((c.get("photo_url") for c in ((board or {}).get("items") or [])
                  if c.get("photo_url")), None)
    if photo:
        status, img = call(photo)
        check("listing photos still load",
              status == 200 and isinstance(img, (bytes, bytearray)) and len(img) > 500,
              f"{photo} -> {status}, {len(img) if hasattr(img,'__len__') else '?'} bytes")
    else:
        print("  [skip] no listing photo in the sample to check")

    section("nothing that needs a login was missed")
    declared = locked_routes_from_the_app()
    covered = {path for _, _, path, _ in LOCKED}
    # Compare by shape, since the list above uses concrete ids where the route
    # has a parameter.
    import re

    def shape(p: str) -> str:
        return re.sub(r"/\d+", "/{id}", re.sub(r"\{[^}]+\}", "{id}", p))

    covered_shapes = {shape(p) for p in covered}
    missed = [p for p in declared if shape(p) not in covered_shapes]
    print(f"     {len(declared)} routes require a login; {len(covered)} probed by name")
    check("every probed path is a route the app actually has",
          all(shape(p) in {shape(d) for d in declared} for p in covered),
          str([p for p in covered if shape(p) not in {shape(d) for d in declared}]))
    if missed:
        print(f"     not probed individually ({len(missed)}): {', '.join(missed[:8])}"
              + (" ..." if len(missed) > 8 else ""))

    section("signing in restores what is theirs")
    status, auth = call("/api/auth/login", {"email": "tester2@campus.edu", "password": "test1234"})
    if status != 200:
        print("  [skip] test account unavailable; signed-in half not checked")
    else:
        token = auth["token"]
        status, board2 = call("/api/items/browse?limit=6", token=token)
        check("the same board loads signed in", status == 200)
        c2 = ((board2 or {}).get("items") or [{}])[0]
        check("and distances are now from the buyer, not the campus",
              c2.get("distance_from") == "you", str(c2.get("distance_from")))
        status, _ = call("/api/cart", token=token)
        check("the cart opens signed in", status == 200)

    print()
    if _failures:
        print(f"[FAIL] {len(_failures)} check(s):")
        for f in _failures:
            print("    - " + f)
        return 1
    print("[ok] anonymous browsing acceptance passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
