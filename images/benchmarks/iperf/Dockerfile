FROM ubuntu:noble

RUN set -x \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            iperf=2.1.9+dfsg-1 ca-certificates \
        && rm -rf /var/lib/apt/lists/*

