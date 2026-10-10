"""Pre-deploy check: would this configuration be safe to put on the internet?

Run it against the file you are about to deploy with:

    python scripts/check_production_config.py --env-file .env.production

The blocking rules come from `Settings.production_problems()` — the same
function the app calls at startup — so this cannot pass something the server
would then refuse, or miss something the server would allow. Everything else
here is an advisory: true of the code rather than of the configuration, and
worth reading once before the first deploy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  injects backend into sys.path

_warnings: list[str] = []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env.production",
                    help="the env file to check (default .env.production)")
    args = ap.parse_args()

    path = Path(_bootstrap.PROJECT_ROOT) / args.env_file
    if not path.exists():
        print(f"[FAIL] {args.env_file} not found.")
        print("       cp deploy/env.production.example .env.production, then fill it in.")
        return 1

    from app.config import Settings

    settings = Settings(_env_file=str(path))
    print(f"checking {args.env_file}")
    print(f"  ENVIRONMENT = {settings.environment}\n")

    if not settings.is_production:
        print("[FAIL] ENVIRONMENT is not 'production'.")
        print("       None of the strict checks apply, /docs stays open, and a")
        print("       development AUTH_SECRET would be accepted.")
        return 1

    problems = settings.production_problems()

    # Settings ignores unknown keys (extra="ignore"), so a misspelled name is
    # dropped without a word and the field keeps its default. That happened:
    # this file once said LLM_MODEL= where the field is LLM_MODEL_MAIN, which
    # would have sent every model call to the built-in Anthropic id. The app
    # cannot catch this — it never sees which file the values came from — so
    # the check does.
    known = {name.upper() for name in Settings.model_fields}
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and key and not key.startswith("#") and key.upper() not in known:
            problems.append(
                f"{key} is not a setting; the app ignores it and uses the default "
                "for whatever it was meant to set. Check the spelling against "
                "backend/app/config.py."
            )

    print("-- blocking (the app refuses to start on these) " + "-" * 12)
    if problems:
        for p in problems:
            print("  [FAIL] " + p)
    else:
        print("  [ok] nothing blocking")

    # ---- advisories: properties of this deployment, not of the settings ----
    print("\n-- worth knowing before the first deploy " + "-" * 18)
    notes = [
        ("Single worker only.",
         "The advisor's candidate-card mapping and its LangGraph thread state are "
         "in-process. A second worker answers 'that list has expired' at random. "
         "Scaling out means moving both to the database first."),
        ("SQLite on one volume.",
         "Every account, listing and conversation is in that one file. Nothing here "
         "backs it up — schedule a copy of the volume before you have real users."),
        ("Uploaded photos are served from DATA_DIR/uploads and nothing else is.",
         "That mount used to cover all of DATA_DIR, which served the database itself "
         "over HTTP. scripts/verify_anonymous_browsing.py keeps it closed."),
    ]
    if settings.vector_backend.lower() == "milvus":
        notes.append((
            "The vector store is remote.",
            "Every buyer search makes a network round trip to it. Measure the latency "
            "from the server (scripts/verify_milvus.py prints a p50) — a cross-region "
            "hop shows up on every single search.",
        ))
    if not settings.want_autonotify_enabled:
        notes.append(("Opt-in buyer notifications are switched off.",
                      "WANT_AUTONOTIFY_ENABLED=false silences them even for buyers "
                      "who turned their own toggle on."))
    for title, body in notes:
        print(f"  · {title}")
        print(f"      {body}")

    print()
    if problems:
        print(f"[FAIL] {len(problems)} blocking problem(s) — fix these, the app will not start.")
        return 1
    print("[ok] this configuration will start in production")
    return 0


if __name__ == "__main__":
    sys.exit(main())
