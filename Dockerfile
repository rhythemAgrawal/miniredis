# syntax=docker/dockerfile:1

# =====================================================================
# Stage 1: builder  — has the toolchain, compiles the C extension,
#                     resolves and installs all dependencies.
#                     This whole stage is thrown away at the end.
# =====================================================================
FROM python:3.13-slim-bookworm AS builder

# uv = fast, lockfile-driven installer. We pull its static binary from
# the official image instead of pip-installing it (one clean copy, no
# extra Python packages polluting the build).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# UV_COMPILE_BYTECODE: precompile .pyc at install time -> faster startup.
# UV_LINK_MODE=copy:   put real files in the venv (not hardlinks into uv's
#                      cache), so the venv is self-contained and survives
#                      the COPY into the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# To compile miniredis._rdb we need gcc AND the C standard-library headers
# (assert.h, stdio.h, ...) from libc6-dev. python:*-slim ships Python.h but
# neither a compiler nor libc6-dev -- and --no-install-recommends would skip
# libc6-dev (gcc only "recommends" it), so we name it explicitly. Then delete
# the apt lists so they don't bloat this (disposable) layer.
#
# Hardening: a flaky connection or a caching proxy can corrupt .deb downloads,
# which apt reports as "Hash Sum mismatch". Retry fetches, bypass stale
# intermediate caches, and disable HTTP pipelining (which confuses some proxies).
RUN printf 'Acquire::Retries "5";\nAcquire::http::No-Cache "true";\nAcquire::http::Pipeline-Depth "0";\n' \
        > /etc/apt/apt.conf.d/99-fetch-hardening \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- dependency layer (changes rarely) -------------------------------
# Copy ONLY the lock + manifest first. As long as these two files don't
# change, Docker reuses the cached result of the install below, even when
# your source changes. This is the layer-cache trick.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# --- project layer (changes often) -----------------------------------
# Now the source. --no-editable builds miniredis into the venv's
# site-packages (with the compiled .so) instead of linking back to /app/src,
# so the runtime stage needs nothing but the venv.
COPY . .
RUN uv sync --frozen --no-dev --no-editable

# =====================================================================
# Stage 2: runtime  — minimal. No gcc, no headers, no source, no uv.
#                     Just the interpreter (from the base) + the venv.
# =====================================================================
FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

# The one cross-stage copy: the finished virtualenv. It holds the deps
# AND the installed miniredis package (including _rdb.so). The interpreter
# and OS userland come from this stage's own base image.
COPY --from=builder /app/.venv /app/.venv

# Put the venv first on PATH so `python` resolves to the venv's python.
ENV PATH="/app/.venv/bin:$PATH"

# Inside the container the network namespace is isolated, so binding all
# interfaces (0.0.0.0) is safe — eth0 is only reachable via an explicit `-p`
# mapping. The code still defaults to 127.0.0.1 for bare (non-container) runs.
ENV HOST=0.0.0.0 \
    PORT=6380

# Persistence lives under /data, a dedicated dir kept SEPARATE from /app so a
# volume mounted here can never shadow the installed venv at /app/.venv. Both
# AOF files live here so the rewrite's temp -> main rename stays on one
# filesystem and stays atomic. These are CONTAINER paths; the app writes here,
# and a named volume mapped to /data makes the bytes outlive the container:
#   docker run -v miniredis-data:/data ...
RUN mkdir -p /data
ENV AOF_MAIN_FILE_PATH=/data/main.aof \
    AOF_TEMP_FILE_PATH=/data/temp.aof \
    SNAPSHOT_PATH=/data/dump.rdb

# Declares /data as a volume (matches the official Redis image). If you forget
# `-v`, Docker still persists to an anonymous volume rather than losing data --
# though a NAMED `-v miniredis-data:/data` is what you want, so you can find it
# again. Tradeoff: anonymous volumes can accumulate; prefer the named form.
VOLUME /data

# Documents the port for humans and tooling. Does NOT publish it — that's
# `-p` at `docker run` time.
EXPOSE 6380

CMD ["python", "-m", "miniredis"]
