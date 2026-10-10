"""Centralized configuration. Every tunable lives here + the project-root .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Basics ----------
    app_name: str = "LocSwap"
    data_dir: str = "data"
    db_url: str = "sqlite:///data/locswap.db"
    #: "development" or "production". Production is strict about the handful of
    #: defaults that are fine on a laptop and dangerous on the open internet —
    #: see `assert_production_ready`, which is called at startup and refuses to
    #: boot rather than warning. A warning in a log nobody reads is how a demo
    #: secret ends up signing real sessions.
    environment: str = "development"
    #: HMAC key for session tokens. Anyone who knows it can mint a token for any
    #: user id, so the default is refused in production.
    auth_secret: str = "dev-only-change-me"
    auth_token_expire_hours: int = 168
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------- State machine ----------
    confirm_window_hours: int = 24
    default_pickup_range_km: float = 3.0
    expiry_poll_interval_seconds: int = 60

    # ---------- LLM ----------
    #: anthropic = official Anthropic SDK; openai_compat = any OpenAI-compatible API
    #: (OpenRouter / proxies), called directly via httpx — no extra SDK needed
    llm_provider: str = "anthropic"
    #: required for openai_compat, e.g. https://openrouter.ai/api/v1
    llm_base_url: str = ""
    #: key for openai_compat; falls back to anthropic_api_key when empty
    llm_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model_main: str = "claude-sonnet-4-6"
    llm_model_light: str = "claude-haiku-4-5"
    llm_max_tokens_listing: int = 800
    llm_temperature_listing: float = 0.2
    llm_max_tokens_query: int = 300
    llm_temperature_query: float = 0.0
    llm_json_retries: int = 1
    llm_timeout_seconds: float = 60.0

    # ---------- Seller advisor (call site C) ----------
    llm_max_tokens_seller_advisor: int = 1500
    llm_temperature_seller_advisor: float = 0.3
    #: max model rounds per request (each round may carry tool calls)
    #: The *outer* advice loop: how many times the advisor may propose an action
    #: and come back after the answer. Separate budget from the inner tool
    #: rounds below — one is "how many times may it act", the other is "how many
    #: tool calls may it make to answer once", and sharing a number would let a
    #: chatty analysis eat the action budget.
    advisor_react_max_loops: int = 3
    seller_advisor_max_tool_rounds: int = 4
    #: max tool calls executed within a single round
    seller_advisor_max_tool_calls_per_round: int = 3
    #: chat context keeps only the last N turns (1 turn = one user + one advisor message)
    seller_advisor_history_turns: int = 6
    #: server-side cap for find_potential_buyers top_n
    seller_advisor_top_n_max: int = 5
    #: max number of sold-price evidence rows read by analyze_sold_prices
    seller_advisor_evidence_limit: int = 30

    # ---------- Agent orchestration (call site D: intent router) ----------
    #: empty -> llm_model_light. The router classifies on every turn, so
    #: pointing it at the main model quietly doubles the per-turn cost.
    llm_model_router: str = ""
    llm_max_tokens_router: int = 300
    llm_temperature_router: float = 0.0

    # ---------- Call site H: defer-sale intent ----------
    #: 「以后再卖」的识别。跟着路由走同一个轻模型：它和路由一样每轮都跑，出参
    #: 只有一个布尔值和两段引文，用主模型是纯浪费。
    llm_max_tokens_defer_intent: int = 300
    llm_temperature_defer_intent: float = 0.0
    #: 关掉这一层，判断退回 `intent_plan` 的词法规则。留这个开关是因为这一层是
    #: 每轮一次的额外调用：配额紧张时它应该可以被关掉，而不是被删掉。
    defer_intent_model_enabled: bool = True
    #: how many (user, assistant) turns a server-side thread keeps in context
    agent_session_history_turns: int = 6
    #: idle threads are dropped after this long
    agent_session_ttl_minutes: int = 120
    #: hard cap on live threads (the in-process checkpointer has no bound of its own)
    agent_session_max_threads: int = 500
    #: How many turns in a row may end in a question before a default plan runs
    #: instead. Clarifying is not the problem — clarifying *without a
    #: termination condition* is, and two identical questions is already one
    #: more than a seller will sit through. Past this the advisor picks the
    #: default analysis and says in the reply that it did.
    advisor_max_clarify_rounds: int = 2

    # ---------- Plan agent (call site G) ----------
    #: 比销售顾问少一轮：这个 agent 的工具没有一个需要"先查再查"的链条——
    #: 最长的一条路是"列出计划 → 撤掉其中一条"，两轮就够。轮次给多了不会让它
    #: 答得更好，只会让一次跑偏的对话多烧两次调用。
    plan_agent_max_tool_rounds: int = 3
    #: 每轮最多两个。一轮里同时动三条计划的请求真实存在（"这几条都停了"），
    #: 但那种请求分两轮做更安全——每一轮的结果都回到模型眼前，它才能如实报告
    #: 哪几条动了、哪几条没动。
    plan_agent_max_tool_calls_per_round: int = 2
    llm_max_tokens_plan_agent: int = 900
    #: 比销售顾问低（0.3）。这个 agent 的输出里有一半是承诺文案，措辞稳定比
    #: 措辞多样重要得多。
    llm_temperature_plan_agent: float = 0.2

    # ---------- Deferred-sale plans (the "I'll sell it later" watcher) ----------
    #: How long before the seller's target date the watcher wakes up. Six weeks
    #: is chosen to be actionable rather than tidy: it is long enough that a
    #: demand trend has several weekly buckets to be read from, and short
    #: enough that the market it measured is still the one the seller will list
    #: into.
    sale_plan_watch_weeks: int = 6
    #: Reservations in the plan's category in one week, above which the seller
    #: is told the market is hot right now.
    sale_plan_reserve_threshold: int = 10
    #: Week-on-week growth in demand events that counts as a surge.
    sale_plan_growth_threshold: float = 0.30
    #: Floor under the *previous* week before growth means anything. Without it
    #: one search becoming two is a 100% surge, and the seller gets an email
    #: about a market of two people.
    sale_plan_growth_min_base: int = 5
    #: How often the watcher loop looks for due plans. It fires weekly per
    #: plan; this is only how often it checks whether that week has come.
    sale_plan_poll_interval_seconds: int = 3600
    #: Unused since 2026-09-23: the weekly pass no longer waits for a fixed
    #: weekday (a process that was down that day lost the whole week). Kept so
    #: existing .env files that set it still load.
    sale_plan_weekday: int = 0

    # ---------- Outbound email ----------
    #: Off by default and the default is not an accident: every message this
    #: system sends is written to `email_outbox` first, and sending is a second
    #: step that only happens when a real SMTP host is configured. A queued row
    #: is inspectable, testable and harmless; a half-configured mailer that
    #: sends to whatever address a fixture happens to contain is none of those.
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    #: From address. Falls back to smtp_user when empty.
    smtp_from: str = ""
    #: How many queued messages one flush attempts.
    email_outbox_batch: int = 20

    # ---------- Publishing agent (call sites E and F) ----------
    #: separate Chroma collection for the rule book
    rules_vector_collection: str = "listing_rules"
    #: how many advisory rules retrieval adds; every block rule is always sent
    publishing_rule_top_k: int = 6
    publishing_max_tokens_compliance: int = 700
    publishing_max_tokens_extract: int = 900

    # ---------- Embedding ----------
    embedding_backend: str = "auto"  # auto | sentence_transformers | hashing
    #: Chinese corpus since 2026-09-15. Despite the matching "small" in the
    #: name, bge-small-zh-v1.5 is **512-dim**, not 384 like bge-small-en-v1.5 —
    #: so the switch changes the vector store's dimension as well as the vector
    #: values. A collection built for the old model cannot be reused or even
    #: written to; it has to be rebuilt in a fresh collection (see CHROMA_DIR /
    #: MILVUS_COLLECTION_PREFIX). The cosine baselines move too: measured on the
    #: Chinese corpus, positives sit far lower than the English model's did, so
    #: every score floor was recalibrated rather than carried over.
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    #: Matches the model's dimension on purpose. The hashing fallback writes
    #: into the same collection, and a collection is fixed-width: a 384-dim
    #: fallback against a 512-dim collection fails every write instead of
    #: degrading, which is the opposite of what the fallback is for.
    embedding_fallback_dim: int = 512
    #: bge-zh's own instruction prefix. It is prepended to queries only (never
    #: to documents) — that asymmetry is what the model was trained with, and
    #: prefixing both sides measurably flattens the score spread.
    embedding_query_prefix: str = "为这个句子生成表示以用于检索相关文章："

    # ---------- Vector store ----------
    vector_backend: str = "chroma"  # chroma | milvus | memory
    chroma_dir: str = "data/chroma"
    chroma_collection: str = "items"

    # Milvus / Zilliz Cloud. The same two settings address a self-hosted Milvus
    # (uri=http://localhost:19530, empty token), so switching between managed
    # and local is a config change, not a code change.
    #: Prepended to every collection name. Its job is to stop two environments
    #: from sharing one collection: a startup rebuild *resets* the collection
    #: before refilling it, so a dev box, a staging container or a CI run
    #: pointed at the production cluster wipes production's index the moment it
    #: boots. That is not hypothetical — it happened on 2026-09-15, when a
    #: container with an empty data volume emptied the 644-item index.
    #: Set it per environment, e.g. "prod_" / "staging_".
    milvus_collection_prefix: str = ""
    milvus_uri: str = ""
    milvus_token: str = ""
    #: Per-RPC deadline. A cold serverless cluster takes tens of seconds to wake,
    #: and a deadline shorter than that turns a wake-up into a silent fallback
    #: to the empty in-memory store — which answers every search with nothing.
    milvus_timeout_s: float = 30.0
    #: Retries for the liveness check at construction time, for that same reason.
    milvus_connect_attempts: int = 3
    #: "Strong" makes a write visible to the very next search. Milvus defaults to
    #: "Bounded" (a few seconds of staleness), under which a seller can list an
    #: item and not find it — Chroma has no such window, so matching it keeps the
    #: backends interchangeable.
    milvus_consistency: str = "Strong"

    # ---------- Retrieval pipeline ----------
    recall_top_k: int = 20
    final_top_k: int = 8
    rrf_k: int = 60
    retrieval_min_confidence: float = 0.25
    # Vector leg: absolute similarity floor + window relative to the best hit
    # (bge-family cosine baselines run high, so both are needed).
    #
    # Recalibrated 2026-09-15 on the Chinese corpus (644 items, bge-small-zh-v1.5)
    # with scripts/calibrate_retrieval.py: positives score 0.420–0.708 (median
    # 0.564), negatives 0.321–0.481 (washing machine .448, printer .450,
    # graphics card .481...). 0.50 clears every negative; the positives under it
    # are conversational phrasings that the other two legs recall, and the
    # end-to-end sweep keeps 100% of positives there.
    #
    # The whole band sits ~0.15 below the English model's on the same kind of
    # query — the two models' cosine scales are not comparable, which is why
    # this could not be carried over and must be re-measured per corpus.
    #
    # The confidence gate cannot catch this noise — RRF confidence measures
    # "do the legs agree on top1" and is constantly 1.0 when only one leg is
    # active, so noise must be blocked at recall level with these score floors.
    vector_min_similarity: float = 0.50
    vector_relative_window: float = 0.15
    # Keyword leg: absolute BM25 floor + ratio floor relative to the best hit
    # (recalibrate whenever the corpus changes). Measured 2026-09-15: Chinese
    # negatives score a clean 0.0 — jieba emits 洗衣机 as one token and it
    # appears in no document, so the partial-token noise the English floor was
    # fighting ("machine" in "machine washable") does not arise. The floor only
    # has to clear the lowest real hit (5.53).
    bm25_min_score: float = 5.0
    bm25_relative_cutoff: float = 0.30
    # Structured leg: ratio floor relative to the best hit
    structured_relative_cutoff: float = 0.7
    enable_vector_recall: bool = True
    enable_bm25_recall: bool = True
    enable_structured_recall: bool = True
    max_clarify_rounds: int = 1
    chat_history_turns: int = 4
    retrieval_debug_log: bool = True

    # ---------- Images ----------
    image_max_edge: int = 1024
    image_jpeg_quality: int = 85

    # ---------- Standing wants ----------
    want_enabled: bool = True
    want_ttl_days: int = 14
    want_vector_collection: str = "wants"
    #: max "back in stock" pushes per buyer per day.
    #: Seller-initiated pushes count against this too: nuisance control is the
    #: buyer's right and does not lapse just because a human pressed the button.
    want_max_notify_per_day: int = 3
    #: lifetime push cap per want (avoid nagging about the same need).
    #: Seller-initiated pushes are deliberately EXEMPT from this one — it exists
    #: to stop automated matching from grinding away at the same need, and a
    #: seller who hand-picked a card is a stronger signal than any match score.
    want_max_notify_total: int = 5
    #: Per-seller daily cap on hand-picked pushes. Without it the candidate list
    #: turns into a broadcast tool, which is exactly the outcome the anonymised
    #: cards were meant to prevent.
    want_max_push_per_seller_per_day: int = 20

    # ---------- want <-> item matching thresholds ----------
    # Deliberately NOT vector_min_similarity. That line is calibrated for
    # query -> item: a short query (carrying the bge query prefix) against a
    # long item document. Here the comparison is item -> want: the *item*
    # document is embedded as the query and matched against want documents,
    # so both sides differ in length, prefix and phrasing. The two score
    # distributions do not overlap, and sharing one line was a coincidence,
    # not a design decision.
    #
    # 0.53 is set from measurement, but on a SMALL sample — treat it as a
    # working value, not a calibrated one. What was observed:
    #   positives   0.586  "I want to buy a book"     vs a watercolor book
    #               0.527  "I want to buy a notebook" vs the same
    #   negatives   0.523  "printer"   vs a watercolor book
    #               0.515  "used bike" vs a mechanical keyboard  <- the clearest
    #               0.516  "digital products", 0.505 "projector", 0.504 "utensil"
    # The two classes nearly touch (0.527 vs 0.523), so no line separates them
    # cleanly. 0.53 deliberately favours precision over recall: a bogus buyer
    # shown to a seller costs more trust than a missed one costs reach. That
    # drops the marginal 0.527 positive too — accepted on purpose.
    # Run a real sweep before trusting this; scripts/calibrate_retrieval.py
    # currently only sweeps query -> item and needs a want -> item mode added.
    #: Master switch for the opt-in fan-out. Off disables it everywhere,
    #: including for buyers who already turned their own toggle on.
    want_autonotify_enabled: bool = True
    #: Semantic floor for an automatic notification — a backstop, not the gate.
    #:
    #: The gate is word coverage (see `_covers_the_want` in services/wants.py).
    #: That split came out of measuring, not taste. Scoring 86 same-category
    #: (want, listing) pairs on the real corpus showed the similarity score does
    #: **not** separate "this is the thing you asked for" from "this uses your
    #: words" at any cut point:
    #:
    #:     0.7016  "lab manual"          -> physics textbook       (no)
    #:     0.6994  "futon"               -> IKEA futon             (yes)
    #:     0.6912  "psychology textbook" -> physics textbook       (no)
    #:     0.6755  "mechanical keyboard" -> USB microphone         (no)
    #:     0.6075  "graphing calculator" -> scientific calculator  (yes)
    #:
    #: The wrong answers outscore right ones. That is the same finding that got
    #: the automatic fan-out deleted on 2026-08-22, re-derived from data — and
    #: the reason a high threshold was the wrong fix: 0.72 sat above the whole
    #: sample's maximum (0.7107), so it silenced the feature rather than
    #: sharpening it, including for an exact "mechanical keyboard" match at
    #: 0.7062.
    #:
    #: So this number only has to exclude the obviously unrelated; the
    #: discrimination happens on words. Kept above the seller-facing 0.53
    #: because nobody reviews an automatic send.
    want_autonotify_min_similarity: float = 0.60
    want_match_min_similarity: float = 0.53
    want_match_relative_window: float = 0.15
    #: E2's reverse stock scan ("by the way, there's an X in stock") is
    #: buyer-facing, unlike E1/E4/the seller advisor. It follows
    #: want_match_min_similarity by default; set this to tighten that one
    #: outlet alone if the hints start getting noisy.
    want_stock_hint_min_similarity: float | None = None

    # ---------- Derived paths ----------
    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    def production_problems(self) -> list[str]:
        """Settings that are fine locally and unsafe once this is reachable.

        Returned rather than raised so the same list can be printed by a
        pre-deploy check and enforced at startup — one definition, two uses, no
        chance of the check and the enforcement disagreeing.
        """
        problems: list[str] = []
        if self.auth_secret == "dev-only-change-me" or len(self.auth_secret) < 32:
            problems.append(
                "AUTH_SECRET is the built-in default or too short. Session tokens are "
                "HMAC-signed with it, so anyone who knows it can mint a token for any "
                "account. Set it to 32+ random characters."
            )
        if not self.cors_origin_list:
            problems.append(
                "CORS_ORIGINS is empty, which falls back to '*'. Combined with "
                "allow_credentials that lets any site call this API as a logged-in "
                "user. Set it to the site's own origin, e.g. https://example.com"
            )
        if any(o == "*" for o in self.cors_origin_list):
            problems.append("CORS_ORIGINS contains '*'. Name the origins instead.")
        if self.embedding_backend.lower() == "auto":
            problems.append(
                "EMBEDDING_BACKEND=auto silently falls back to the hashing encoder when "
                "the model cannot be loaded — search keeps answering, with far worse "
                "results, and nothing errors. Set it to sentence_transformers so a "
                "missing model fails loudly instead."
            )
        defaults = type(self).model_fields
        if self.llm_provider.lower() != "anthropic":
            for name in ("llm_model_main", "llm_model_light"):
                if getattr(self, name) == defaults[name].default:
                    problems.append(
                        f"{name.upper()} is still the built-in Anthropic model id "
                        f"({getattr(self, name)!r}) but LLM_PROVIDER={self.llm_provider}. "
                        "Every model call would fail. Set it to the provider's id, "
                        "e.g. openai/gpt-5.6-terra on OpenRouter."
                    )
        if self.vector_backend.lower() == "milvus" and not self.milvus_uri:
            problems.append("VECTOR_BACKEND=milvus but MILVUS_URI is empty.")
        if self.vector_backend.lower() == "milvus" and not self.milvus_collection_prefix:
            problems.append(
                "MILVUS_COLLECTION_PREFIX is empty, so this instance shares collection "
                "names with every other one on the same cluster — including development. "
                "A startup rebuild resets the collection first, so whichever instance "
                "boots last wins and the others' index is gone. Set it to something "
                "like 'prod_'."
            )
        return problems

    def assert_production_ready(self) -> None:
        if not self.is_production:
            return
        problems = self.production_problems()
        if problems:
            raise RuntimeError(
                "Refusing to start in production with these settings:\n  - "
                + "\n  - ".join(problems)
            )

    def _abs(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def data_path(self) -> Path:
        return self._abs(self.data_dir)

    @property
    def upload_path(self) -> Path:
        return self.data_path / "uploads"

    @property
    def chroma_path(self) -> Path:
        return self._abs(self.chroma_dir)

    @property
    def prompts_path(self) -> Path:
        return PROJECT_ROOT / "prompts"

    @property
    def sqlalchemy_url(self) -> str:
        """Resolve a relative sqlite path against the project root so cwd doesn't matter."""
        prefix = "sqlite:///"
        if self.db_url.startswith(prefix):
            raw = self.db_url[len(prefix) :]
            return prefix + str(self._abs(raw)).replace("\\", "/")
        return self.db_url

    def ensure_dirs(self) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.upload_path.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()


# ============================================================
# Preset campus location dictionary (demo approach: both the signup
# onboarding picker and item pickup locations come from this list).
#
# Setting: Johns Hopkins University (Homewood campus, Baltimore).
# Every location must fall inside CAMPUS_BOUNDS, a lat/lng rectangle
# centered on campus — apartment buildings are real student apartments
# around campus, plus several townhouse blocks in Charles Village /
# Hampden / Remington.
# ============================================================
#: Where a visitor without an account is standing, for the purpose of showing
#: distances. The centroid of the 18 pickup spots, which is a few hundred metres
#: from the Homewood campus itself.
#:
#: A signed-out visitor has no address, but a marketplace card that cannot say
#: "0.2 km away" is just a list — proximity is the product. So the distance is
#: shown from here and **labelled as being from campus**, never presented as
#: being from the visitor: an unqualified "1.2 km" would be a number about
#: somewhere they are not.
CAMPUS_CENTER: dict[str, float] = {"lat": 39.32876, "lng": -76.62101}

CAMPUS_BOUNDS: dict[str, float] = {
    # Center ≈ (39.3300, -76.6205), i.e. the JHU Homewood campus;
    # ~3.3 km N-S (E 25th St ~ W Cold Spring Ln), ~4.2 km E-W (Woodberry ~ Ellerslie Ave).
    # lng_min reaches past the Jones Falls so the Fox Building (3500 Parkdale Ave,
    # Woodberry) can sit on its real coordinate instead of a rounded stand-in.
    "lat_min": 39.3150,
    "lat_max": 39.3450,
    "lng_min": -76.6520,
    "lng_max": -76.6030,
}

CAMPUS_LOCATIONS: list[dict] = [
    # Coordinates below are geocoded against OpenStreetMap / Nominatim
    # (2026-09-07), not hand-estimated — the previous rounded values sat up to
    # ~300 m off their real buildings, which showed up both as misplaced map
    # pins and as wrong `pickup_range_km` distances. Re-geocode rather than
    # nudge by hand if a location ever moves.
    #
    # ---- Apartments ----
    # OSM way "The Marylander", 3501 Saint Paul St
    {"name": "The Marylander (3501 St Paul St)", "lat": 39.33043, "lng": -76.61576},
    # OSM "The Carlyle Apartments", 500 W University Pkwy
    {"name": "The Carlyle (500 W University Pkwy)", "lat": 39.33730, "lng": -76.62441},
    # OSM address point 105 W 39th St (the building also hosts Cypriana)
    {"name": "The Broadview (105 W 39th St)", "lat": 39.33598, "lng": -76.62146},
    # OSM "Wyman Park Apartments", 3925 Beech Ave (the W 40th St side)
    {"name": "Wyman Park Apartments (W 40th St)", "lat": 39.33616, "lng": -76.62649},
    # OSM "Icon Residences at the Rotunda", 727 W 40th St
    {"name": "ICON Residences (Hampden)", "lat": 39.33474, "lng": -76.63030},
    # OSM "Nine East 33rd", 9 E 33rd St
    {"name": "Nine East 33rd", "lat": 39.32754, "lng": -76.61649},
    # OSM address point 3305 N Calvert St
    {"name": "Jefferson House (N Calvert St)", "lat": 39.32857, "lng": -76.61497},
    # OSM address point 3201 Saint Paul St
    {"name": "The Allston (St Paul & 32nd)", "lat": 39.32746, "lng": -76.61562},
    # OSM address point 3121 Saint Paul St
    {"name": "The Baltimorean (3121 St Paul St)", "lat": 39.32674, "lng": -76.61560},
    # OSM Cresmont Ave (street centroid; the building has no OSM footprint)
    {"name": "HH Cresmont (Cresmont Ave)", "lat": 39.32368, "lng": -76.62170},
    # No OSM record for this building — snapped onto the E 29th St centerline
    # at its original longitude, so it is at least on the right street.
    {"name": "Campus Square Apartments (E 29th St)", "lat": 39.32357, "lng": -76.61280},
    # OSM address point 3500 Parkdale Ave — really Woodberry, west of the Jones
    # Falls, which is why CAMPUS_BOUNDS["lng_min"] reaches out to -76.6520.
    {"name": "Fox Building Apartments (Hampden)", "lat": 39.33068, "lng": -76.64910},
    # ---- Townhouses (block-level: geocoded to a house number in the block) ----
    # OSM address point 3300 Abell Ave
    {"name": "E 33rd St townhouses (at Abell Ave)", "lat": 39.32855, "lng": -76.61231},
    # OSM address point 3200 Guilford Ave
    {"name": "Guilford Ave 3200 block townhouses", "lat": 39.32713, "lng": -76.61372},
    # OSM address point 2900 Saint Paul St
    {"name": "St Paul St 2900 block townhouses", "lat": 39.32342, "lng": -76.61604},
    # OSM address point 2800 N Charles St
    {"name": "N Charles St 2800 block townhouses", "lat": 39.32223, "lng": -76.61759},
    # OSM address point 2900 Remington Ave
    {"name": "Remington Ave townhouses (at W 29th St)", "lat": 39.32224, "lng": -76.62237},
    # OSM address point 3600 Elm Ave
    {"name": "Hampden Elm Ave townhouses (at W 36th St)", "lat": 39.33128, "lng": -76.63146},
]

assert all(
    CAMPUS_BOUNDS["lat_min"] <= loc["lat"] <= CAMPUS_BOUNDS["lat_max"]
    and CAMPUS_BOUNDS["lng_min"] <= loc["lng"] <= CAMPUS_BOUNDS["lng_max"]
    for loc in CAMPUS_LOCATIONS
), "a location falls outside the CAMPUS_BOUNDS rectangle"

LOCATION_BY_NAME: dict[str, dict] = {loc["name"]: loc for loc in CAMPUS_LOCATIONS}


# ============================================================
# Dictionaries for structured recall / rule-based parsing (demo grade)
# Category keys are the canonical values stored in items.attributes.category.
# They are Chinese as of the 2026-09-15 localization; English synonyms are kept
# in the value lists on purpose — a campus crowd types "laptop" as readily as
# "笔记本电脑", and keeping both costs nothing at recall level.
# Matching is case-insensitive; English synonyms match on word boundaries,
# Chinese ones match as substrings (there are no spaces to anchor on).
# ============================================================
CATEGORY_DICT: dict[str, list[str]] = {
    "电子产品": [
        "电子产品", "电子设备", "数码", "数码产品", "数码设备", "智能设备", "科技产品",
        # "笔记本" alone is deliberately absent: in Chinese it is both "laptop"
        # and "paper notebook", and Electronics is matched first, so it would
        # steal every stationery query. It stays under 学习用品 and the laptop
        # sense is carried by the unambiguous compounds below.
        "笔记本电脑", "手提电脑", "电脑", "平板", "平板电脑", "手机", "智能手机",
        "耳机", "蓝牙耳机", "头戴耳机", "键盘", "机械键盘", "鼠标", "显示器", "屏幕",
        "相机", "摄像头", "充电宝", "移动电源", "充电器", "音箱", "音响", "游戏机",
        "智能手表", "路由器", "电子书阅读器", "阅读器", "投影仪", "麦克风",
        # Hypernyms / colloquial synonyms: shoppers type the category, not the thing.
        # Deliberately NOT here: bare "配件" — it also heads "自行车配件",
        # "运动配件" and "服饰配件", and Electronics is matched first, so it
        # would hijack all three. Same reasoning as the English "accessories".
        "electronics", "laptop", "computer", "macbook", "chromebook", "phone",
        "smartphone", "iphone", "tablet", "ipad", "kindle", "e-reader", "headphones",
        "earbuds", "airpods", "keyboard", "mouse", "monitor", "camera", "power bank",
        "charger", "speaker", "console", "switch", "smartwatch", "webcam", "router",
        "gadget", "tech product", "tech device", "electronic equipment",
        "digital product", "mobile device", "smart device",
    ],
    "自行车与滑板车": [
        "自行车", "单车", "脚踏车", "山地车", "公路车", "折叠车", "电动车", "电瓶车",
        "电动自行车", "滑板车", "电动滑板车", "代步车", "代步工具", "通勤车", "死飞",
        "bike", "bicycle", "mountain bike", "road bike", "folding bike", "e-bike",
        "scooter", "e-scooter", "skateboard commuter", "fixie", "cruiser",
        "personal transportation",
    ],
    "家具与电器": [
        "地毯",
        "家具", "电器", "家电", "大件家具", "书桌", "桌子", "餐桌", "椅子", "办公椅",
        "床垫", "沙发", "沙发床", "落地灯", "衣柜", "斗柜", "抽屉柜", "风扇",
        "电风扇", "微波炉", "小冰箱", "迷你冰箱", "冰箱", 
        "书架", "床架", "取暖器", "电暖器", "吸尘器", "洗衣机",
        "furniture", "appliance", "desk", "table", "chair", "mattress", "futon",
        "couch", "sofa", "lamp", "wardrobe", "dresser", "fan", "microwave",
        "mini fridge", "fridge", "kettle", "shelf", "bookshelf", "bed frame",
        "space heater", "air fryer", "vacuum",
    ],
    "图书与教材": [
        "书", "图书", "书籍", "教材", "课本", "教科书", "小说", "习题册", "习题集",
        "答案册", "解题手册", "词典", "字典", "考试用书", "复习资料", "参考书",
        "工具书", "专业书", "原版书",
        "book", "textbook", "novel", "course book", "solution manual", "dictionary",
        "study guide", "mcat", "lsat", "workbook",
    ],
    "服装与鞋靴": [
        "跑鞋",
        "衣服", "服装", "衣物", "外套", "大衣", "羽绒服", "冲锋衣", "卫衣", "帽衫",
        "毛衣", "针织衫", "鞋", "鞋子", "球鞋", "运动鞋", "靴子", "雪地靴", "背包",
        "书包", "双肩包", "包", "帽子", "裙子", "连衣裙", "裤子", "牛仔裤", "围巾",
        "时尚", "穿搭", "行李箱", "拉杆箱",
        "clothes", "clothing", "jacket", "coat", "down jacket", "hoodie", "sweater",
        "shoes", "sneakers", "boots", "backpack", "bag", "hat", "dress", "pants",
        "jeans", "scarf",
        "fashion", "trainer",  # "trainer" = British English for sneakers
    ],
    "运动与户外": [
        "运动", "健身", "体育", "户外", "哑铃", "壶铃", "瑜伽垫", "球拍", "羽毛球",
        "羽毛球拍", "网球拍", "篮球", "足球", "网球", "帐篷", "滑板", 
        "露营", "弹力带", "阻力带", "跳绳", "运动器材", "健身器材",
        "sports", "gym", "fitness", "dumbbell", "yoga mat", "racket", "badminton",
        "basketball", "soccer", "tennis", "tent", "skateboard", "running shoes",
        "camping", "kettlebell", "resistance bands",
        "recreational equipment",
    ],
    "家居与厨房": [
        "空气炸锅",
        "置物架",
        "烧水壶",
        "电热水壶",
        "台灯",
        "家居", "厨房", "厨具", "日用品", "生活用品", "水杯", "保温杯", "锅",
        "炒锅", "平底锅", "碗", "餐具", "碗碟", "被子", "被褥", "毯子", "枕头",
        "拖鞋", "镜子", "收纳箱", "收纳盒", "衣架", "窗帘", "电饭煲",
        "电饭锅", "咖啡机", "破壁机", "榨汁机", "居家好物", "收纳",
        "kitchen", "household", "water bottle", "pot", "pan", "bowl", "dishes",
        "comforter", "blanket", "pillow", "slippers", "mirror", "storage bins",
        "hangers", "curtains", "rug", "instant pot", "coffee maker", "blender",
        "home product", "home organization",
    ],
    "乐器": [
        "乐器", "吉他", "木吉他", "电吉他", "尤克里里", "钢琴", "电钢琴", "键盘琴",
        "电子琴", "口琴", "贝斯", "小提琴", "音箱功放", "架子鼓", "笛子",
        "guitar", "ukulele", "piano", "keyboard piano", "digital piano", "harmonica",
        "instrument", "bass", "violin", "amp",
    ],
    "学习用品": [
        "文具", "学习用品", "笔", "中性笔", "钢笔", "本子", "活页纸",
        "计算器", "科学计算器", "图形计算器", "手账", "计划本", "画板", "颜料",
        "水彩", "马克笔", "文件夹", "活页夹", "白板", "便利贴",
        "学习必备", "考试用品",
        "stationery", "pens", "notebook paper", "calculator", "ti-84", "planner",
        "drawing board", "paint set", "binder", "index cards", "whiteboard",
        "school essential",
    ],
}

#: Brand names stay in their original Latin spelling — that is how they are
#: printed on the product and how sellers type them. The Chinese aliases below
#: only feed recall: a buyer searching 宜家 must find the IKEA desk.
BRAND_DICT: list[str] = [
    "Apple", "Samsung", "Google", "Dell", "HP", "Lenovo", "ASUS", "Acer",
    "Microsoft", "Logitech", "Sony", "Bose", "JBL", "Anker", "Nintendo", "Razer",
    "Kindle", "Trek", "Giant", "Schwinn", "Specialized", "Nike", "Adidas",
    "New Balance", "Vans", "Converse", "The North Face", "Patagonia", "Columbia",
    "Champion", "Levi's", "Uniqlo", "IKEA", "Instant Pot", "Keurig", "Ninja",
    "Dyson", "Hamilton Beach", "Yamaha", "Fender", "Casio", "Texas Instruments",
    "Wilson", "Spalding", "Coleman",
]

#: Chinese aliases -> canonical brand. The query parser resolves through this so
#: a Chinese brand query lands on the Latin spelling actually stored on items.
BRAND_ALIAS_ZH: dict[str, str] = {
    "苹果": "Apple", "三星": "Samsung", "谷歌": "Google", "戴尔": "Dell",
    "惠普": "HP", "联想": "Lenovo", "华硕": "ASUS", "宏碁": "Acer",
    "微软": "Microsoft", "罗技": "Logitech", "索尼": "Sony",
    "安克": "Anker", "任天堂": "Nintendo", "雷蛇": "Razer", "捷安特": "Giant",
    "耐克": "Nike", "阿迪达斯": "Adidas", "阿迪": "Adidas", "新百伦": "New Balance",
    "匡威": "Converse", "北面": "The North Face", "优衣库": "Uniqlo",
    "宜家": "IKEA", "戴森": "Dyson", "雅马哈": "Yamaha", "卡西欧": "Casio",
    "德州仪器": "Texas Instruments", "威尔胜": "Wilson",
}

COLOR_DICT: list[str] = [
    "黑色", "白色", "灰色", "银色", "红色", "蓝色", "绿色", "黄色",
    "粉色", "紫色", "米色", "棕色", "金色", "橙色", "多色",
]

CONDITION_DICT: dict[str, list[str]] = {
    "全新": ["全新", "全新未拆", "未拆封", "没用过", "未使用", "吊牌还在", "原封",
             "brand new", "new in box", "unopened", "never used", "sealed", "new with tags"],
    "几乎全新": ["几乎全新", "接近全新", "用过几次", "基本全新", "九九新", "99新",
                 "like new", "barely used", "almost new", "mint condition", "mint"],
    "成色很好": ["成色很好", "成色不错", "轻微使用", "九成新", "9成新", "状态很好",
                 "very good", "great condition", "lightly used", "excellent condition"],
    "成色良好": ["成色良好", "成色还行", "正常使用痕迹", "八成新", "8成新", "能正常用",
                 "good condition", "gently used", "some wear", "used but works"],
    "成色一般": ["成色一般", "使用痕迹明显", "有磨损", "磨损", "比较旧", "七成新",
                 "fair condition", "well used", "worn", "visible wear", "beat up", "old"],
}

# ------------------------------------------------------------
# Legacy value mapping. The corpus, the vector index and these dictionaries were
# English until the 2026-09-15 localization. Rows written before the switch (and
# any old export replayed into the DB) still carry the English canonical values,
# so read paths normalize through these maps rather than trusting the stored
# string. Migration is one-way: nothing writes English canonical values again.
# ------------------------------------------------------------
CATEGORY_EN_TO_ZH: dict[str, str] = {
    "Electronics": "电子产品",
    "Bikes & Scooters": "自行车与滑板车",
    "Furniture & Appliances": "家具与电器",
    "Books & Textbooks": "图书与教材",
    "Clothing & Shoes": "服装与鞋靴",
    "Sports & Outdoors": "运动与户外",
    "Home & Kitchen": "家居与厨房",
    "Musical Instruments": "乐器",
    "School Supplies": "学习用品",
}

CONDITION_EN_TO_ZH: dict[str, str] = {
    "Brand new": "全新",
    "Like new": "几乎全新",
    "Very good": "成色很好",
    "Good": "成色良好",
    "Fair": "成色一般",
}

COLOR_EN_TO_ZH: dict[str, str] = {
    "Black": "黑色", "White": "白色", "Gray": "灰色", "Grey": "灰色",
    "Silver": "银色", "Red": "红色", "Blue": "蓝色", "Green": "绿色",
    "Yellow": "黄色", "Pink": "粉色", "Purple": "紫色", "Beige": "米色",
    "Brown": "棕色", "Gold": "金色", "Orange": "橙色", "Multicolor": "多色",
}


def normalize_category(value: str | None) -> str | None:
    """Return the Chinese canonical category for a stored value (either language)."""
    if not value:
        return None
    return CATEGORY_EN_TO_ZH.get(value.strip(), value.strip())


def normalize_condition(value: str | None) -> str | None:
    """Return the Chinese canonical condition for a stored value (either language)."""
    if not value:
        return None
    v = value.strip()
    if v in CONDITION_EN_TO_ZH:
        return CONDITION_EN_TO_ZH[v]
    # Seed data title-cased these inconsistently ("very good" / "Very good"),
    # so fold case before giving up.
    for en, zh in CONDITION_EN_TO_ZH.items():
        if en.lower() == v.lower():
            return zh
    return v


def normalize_color(value: str | None) -> str | None:
    """Return the Chinese canonical color for a stored value (either language)."""
    if not value:
        return None
    v = value.strip()
    if v in COLOR_EN_TO_ZH:
        return COLOR_EN_TO_ZH[v]
    for en, zh in COLOR_EN_TO_ZH.items():
        if en.lower() == v.lower():
            return zh
    return v


#: Canonical Chinese pickup-time vocabulary. Seed data carried English time
#: strings ("Weekdays 6:00 PM"); the migration and the mock generators compose
#: these pieces instead.
PICKUP_DAY_EN_TO_ZH: dict[str, str] = {
    "Weeknight evenings": "工作日晚上",
    "Weekends all day": "周末全天",
    "By appointment": "预约时间",
    "Weekdays": "工作日",
    "Weekday": "工作日",
    "Weekends": "周末",
    "Weekend": "周末",
    "Daily": "每天",
    "Anytime": "随时",
    "Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四",
    "Fri": "周五", "Sat": "周六", "Sun": "周日",
}
