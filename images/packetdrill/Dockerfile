FROM ubuntu:24.04
RUN apt-get update && apt-get install -y net-tools git iptables iputils-ping \
        netcat-traditional tcpdump jq tar bison flex make iproute2 python3
RUN git init packetdrill && \
    git -C packetdrill fetch --depth 1 https://github.com/google/packetdrill.git \
        43500f46de461c751670ce9d17f9f5a157e69f83 && \
    git -C packetdrill checkout --detach FETCH_HEAD
RUN cd packetdrill/gtests/net/packetdrill && ./configure && make
