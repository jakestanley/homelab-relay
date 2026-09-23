# Output wrappers, recorder and status glue. One image, several commands.
FROM python:3.13-slim-trixie

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
