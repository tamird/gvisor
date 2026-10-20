FROM docker:29-dind@sha256:3e5756e2b6dad6a5f1f5deacf0a19f9eaa2581b76ae092b2d4a118162e1d4d4e

RUN apk add --no-cache bash iproute2 iptables && \
    docker compose version && \
    docker buildx version

COPY start-dockerd.sh /start-dockerd.sh
ENTRYPOINT ["/start-dockerd.sh"]
