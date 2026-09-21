# One image for every Python service (PLAN §12.1). The scorer, graph-refresh, API,
# dashboard, backfill and replayer all run from this same build: they share the whole
# codebase, and separate images would mean six chances for their dependencies to drift
# apart while the code assumes they have not.

# Slim, not alpine: alpine uses musl, which has no manylinux wheels, so numpy, pandas,
# xgboost and duckdb would all compile from source.
FROM python:3.12-slim

# uv from its official image, pinned to the version used on the VM (`uv --version`).
# Copying the binary avoids a pip bootstrap layer, and the exact tag keeps the
# resolver identical to the one that produced uv.lock (PLAN §15.3).
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

# Everything lives under /app; the bind mounts in §12.2 attach beneath it.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    # Unbuffered stdout, or `docker compose logs -f` shows nothing until a buffer fills
    # and the scorer looks hung while it is working.
    UV_COMPILE_BYTECODE=1 \
    # Compile .pyc at build time so the first request does not pay for it.
    UV_LINK_MODE=copy \
    # Copy rather than hardlink: the uv cache and /app can be different filesystems.
    PATH="/app/.venv/bin:$PATH"
    # Put the venv first, so `python` and `uvicorn` resolve without `uv run`.

# 1) Dependencies first, from the lockfile alone. This layer stays cached until
#    pyproject.toml or uv.lock changes, so editing code never reinstalls xgboost.
COPY pyproject.toml uv.lock ./

# --frozen fails if the lock is stale rather than silently resolving something else.
# --no-dev drops the dev/sim/analysis groups: pytest, faker, shap and matplotlib are
# not serving dependencies. --no-install-project keeps this layer free of our code.
RUN uv sync --frozen --no-dev --no-install-project

# 2) Then the code. README.md is copied because pyproject declares it as the readme,
#    and the build fails without it.
COPY README.md ./
COPY src/ src/
COPY configs/ configs/
COPY dashboard/ dashboard/

# Install the project itself into the venv built above.
RUN uv sync --frozen --no-dev

# xgboost's default wheel depends on the CUDA runtime, which is 288 MB of libraries
# that a CPU-only box (§15.2: i5-1235U, integrated graphics) can never load. Removing
# them here rather than switching to the xgboost-cpu package keeps uv.lock — and so the
# host environment that trained models/v1 — byte-identical to what CI resolves.
# PLAN §2.2 lists a properly slim serving image as a NICE TO HAVE; this is the part of
# it that pays for itself immediately.
RUN rm -rf /app/.venv/lib/python3.12/site-packages/nvidia \
           /app/.venv/lib/python3.12/site-packages/nvidia_*.dist-info

# 3) Drop root. uid 1000 matches the VM's first user, so the ./data bind mount is
#    writable by the scorer without a `user:` override in Compose (PLAN §12.2 notes).
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# No CMD: every service in §12.2 supplies its own command, and a default here would
# just be one more thing that could disagree with Compose.
