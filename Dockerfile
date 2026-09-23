# Build the web UI first, so the runtime image has no node in it.
#
# Deliberately not pinned to --platform=$BUILDPLATFORM, tempting as that is: the
# variable only exists under BuildKit, the classic builder leaves it empty, and
# an empty --platform is a parse error. The Home Assistant Supervisor builds a
# local add-on with whichever builder the host has, so this has to work on both.
# The cost is that the cross-built arm64 image runs this stage under QEMU in CI.
FROM node:22-alpine AS webui
WORKDIR /build
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies as their own layer, so a source change does not refetch them.
# LICENSE too: the image redistributes GPL-3.0 code, so the licence travels with it.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY --from=webui /build/dist ./webui/dist
RUN uv sync --frozen --no-dev

# VNODE_CONFIG_FILE is where the Home Assistant Supervisor writes the add-on's
# options. Under docker-compose that file does not exist, and a path that is not
# there is simply not a settings source - so one image serves both deployments.
ENV PATH="/app/.venv/bin:$PATH" \
    VNODE_DB_PATH=/data/vnode.sqlite3 \
    VNODE_LISTEN_HOST=0.0.0.0 \
    VNODE_WEB_HOST=0.0.0.0 \
    VNODE_CONFIG_FILE=/data/options.json
VOLUME ["/data"]
EXPOSE 4404 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,sys,urllib.request; port=os.environ.get('VNODE_WEB_PORT','8080'); sys.exit(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=4).status != 200)"

CMD ["mesh-vnode", "run"]
