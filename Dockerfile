# The relay on Linux: one image, every process. Each container runs
# `python -m relay.run <service>`, which takes the program, arguments and
# environment from Config.services() -- the same code NSSM is configured
# from on Windows -- so renditions, keys and validation are shared.
#
# Built deliberately by scripts/build.sh into a dated tag, never by up.sh.
# `apt-get install ffmpeg` resolves against the live Debian mirror, so two
# builds of this file can contain different ffmpeg versions; the tag, not
# this file, is what was tested. The base is pinned by digest and MediaMTX by
# SHA-256 (the same release scripts/fetch-tools.ps1 pins for Windows).
FROM python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

ARG MEDIAMTX_VERSION=v1.21.1
ARG MEDIAMTX_SHA256=653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && curl -fsSL -o /tmp/mediamtx.tar.gz \
       "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
    && echo "${MEDIAMTX_SHA256}  /tmp/mediamtx.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
    && rm /tmp/mediamtx.tar.gz \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Baked in rather than bind-mounted, so a deploy from an ephemeral clone keeps
# working after the clone is removed.
COPY relay/ /app/relay/
COPY relay.yaml /app/
COPY mediamtx/mediamtx.yml /app/mediamtx/

# Shared report directory. A named volume mounted here starts with these
# permissions, so the processes can run as a non-root user.
RUN mkdir -p /state && chmod 1777 /state
