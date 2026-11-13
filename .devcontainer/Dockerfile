# Dev container for GitHub Codespaces: Python 3.14 + Node 22 + FFmpeg.
# Node comes from the official image rather than a devcontainer "feature", so this file builds
# with plain `docker build` too (and is tested that way).
FROM node:22-slim AS node

FROM mcr.microsoft.com/devcontainers/python:3.14
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg redis-tools \
    && rm -rf /var/lib/apt/lists/*
