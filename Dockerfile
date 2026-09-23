# Output wrappers, recorder and status glue. One image, several commands.
#
# Built deliberately by scripts/build.sh into a dated tag, never by up.sh.
# `apt-get install ffmpeg` resolves against the live Debian mirror, so two
# builds of this file can contain different ffmpeg versions; the tag, not
# this file, is what was tested. The base is pinned by digest so a rebuild
# at least starts from the same rootfs.
FROM python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY relay/ /app/relay/

# Shared report directory. A named volume mounted here starts with these
# permissions, so the wrappers can run as a non-root user.
RUN mkdir -p /state && chmod 1777 /state
