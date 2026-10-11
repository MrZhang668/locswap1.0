# LocSwap — one image, frontend built in and the embedding model baked in.
#
# Two things here are deliberate and easy to lose in a "simplification":
#
# 1. **The embedding model is downloaded at build time, not at run time.**
#    `SentenceTransformer("BAAI/bge-small-zh-v1.5")` reaches Hugging Face on
#    first use. On a fresh server that call can fail — no network yet, a blocked
#    host, a rate limit — and `EMBEDDING_BACKEND=auto` would then quietly fall
#    back to the hashing encoder: search keeps answering, with far worse
#    results, and nothing errors. Baking the weights in (~95 MB) and running
#    with HF_HUB_OFFLINE=1 makes startup deterministic and offline.
#
# 2. **CPU-only torch.** sentence-transformers pulls torch, and the default
#    wheel carries CUDA (~2.5 GB) that a web server will never use. The CPU
#    index brings it to ~200 MB.

# ---------- stage 1: the frontend ----------
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /web/dist

# ---------- stage 2: python deps, including the model ----------
FROM python:3.13-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# CPU torch first so the requirements resolve against it rather than the CUDA build.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Bake the weights into the image.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer;\
m = SentenceTransformer('BAAI/bge-small-zh-v1.5');\
print('baked in, dim =', m.get_sentence_embedding_dimension())"

# Same failure shape as the model: services/text.py falls back to character
# bigrams when jieba is missing and only logs a warning. It was missing from
# requirements until 2026-10-07 and the first production build shipped without
# it. Fail the build instead.
RUN python -c "import jieba; print('jieba ok:', '/'.join(jieba.lcut('二手电饭煲')))"

# Same shape again: the advisor's checkpointer import is lazy and its failure is
# caught (chat answers "顾问暂时不可用", transcripts come back empty), so a
# missing package looks like a healthy 200. It was missing until 2026-10-10.
RUN python -c "from langgraph.checkpoint.sqlite import SqliteSaver; print('checkpoint sqlite ok')"

# ---------- stage 3: the runtime ----------
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    # Never reach for the network for weights at run time. With the model baked
    # in this changes nothing; without it, startup fails loudly, which is the
    # point — see EMBEDDING_BACKEND below.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 locswap

COPY --from=deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY --from=deps /opt/hf /opt/hf

WORKDIR /app
COPY backend/ /app/backend/
COPY prompts/ /app/prompts/
COPY scripts/ /app/scripts/
COPY --from=web /web/dist /app/web

# DATA_DIR is a volume: the SQLite database, the advisor's thread store, the
# uploaded photos and the Chroma directory all live here and must outlive the
# container.
RUN mkdir -p /app/data && chown -R locswap:locswap /app
USER locswap
ENV DATA_DIR=/app/data \
    DB_URL=sqlite:////app/data/locswap.db \
    ENVIRONMENT=production \
    # Explicit, not "auto": if the baked model is somehow missing, fail at
    # startup instead of silently serving hashing-quality search.
    EMBEDDING_BACKEND=sentence_transformers

EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8010/api/health || exit 1

# One worker on purpose. Two in-process caches are not shared between
# processes — the advisor's candidate-card mapping (services/agents/sales/
# tools.py) and the LangGraph thread state — so a second worker would answer
# "that candidate list has expired" at random. Scaling past one means moving
# those to the database first.
# Started from /app/backend as `app.main:app` — the same way the README
# starts it in development. `backend.app.main` would need backend/ to be a
# package and it is not, so that import fails when the container starts
# rather than when it builds: the worst place to find out.
WORKDIR /app/backend
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
