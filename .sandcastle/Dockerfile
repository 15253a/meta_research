FROM ghcr.io/astral-sh/uv:0.11.3@sha256:c152167fbb12521eafe9328693e88adb7474fbe2f1227576ceea87aeec18683e AS uv

FROM node:22-bookworm@sha256:673fce836d5a9185da33352682bfedb17c174d016370d08616748dff76fda862

COPY --from=uv /uv /uvx /bin/

# The pinned full Node image already supplies Git, curl, Python 3, and the native
# build chain. Keep ticket-specific runtimes in the project's locked release
# rather than silently adding mutable system packages to this generic sandbox.

# Build-args for UID/GID alignment: sandcastle docker build-image
# defaults these to the host user's UID/GID so image-built files
# and bind-mounted files share an owner without runtime chown.
ARG AGENT_UID=1000
ARG AGENT_GID=1000

# Rename the base image's "node" user to "agent" and align UID/GID.
RUN groupmod -o -g $AGENT_GID node && usermod -o -u $AGENT_UID -g $AGENT_GID -d /home/agent -m -l agent node

# `npm run sandcastle:build-image` prepares this ignored, Linux x86_64 Codex
# tree on the host. COPY keeps registry credentials and proxy configuration out
# of both the Docker build and the resulting image.
RUN install -d -m 0700 -o ${AGENT_UID} -g ${AGENT_GID} /opt/codex
COPY --chown=${AGENT_UID}:${AGENT_GID} .image-codex/node_modules /opt/codex/node_modules
RUN chmod -R u=rwX,go= /opt/codex \
  && ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex

RUN mkdir -p /home/agent/.agents /home/agent/.codex \
  && chown -R ${AGENT_UID}:${AGENT_GID} /home/agent

ENV UV_LINK_MODE=copy

USER ${AGENT_UID}:${AGENT_GID}

WORKDIR /home/agent

# In worktree sandbox mode, Sandcastle bind-mounts the git worktree at /home/agent/workspace
# and overrides the working directory to /home/agent/workspace at container start.
# Structure your Dockerfile so that /home/agent/workspace can serve as the project root.
ENTRYPOINT ["sleep", "infinity"]
