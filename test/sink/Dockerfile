# Local test sink: a throwaway MediaMTX that accepts any publish, on RTMP
# (:1935) and RTMPS (:1936) with a self-signed certificate made at build time.
# Test tooling only; started with the `sinks` compose profile.
FROM alpine:3 AS cert
RUN apk add --no-cache openssl \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=relay-test-sink \
       -keyout /server.key -out /server.crt

FROM bluenviron/mediamtx:1.21.1
COPY --from=cert /server.key /server.crt /
COPY mediamtx.yml /mediamtx.yml
