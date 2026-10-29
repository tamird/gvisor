ARG BASE_IMAGE=ubuntu:22.04
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND="noninteractive"

#-------------------------------------------------------------------------------
# Note: If you want to use bazel directly you will need these packages or their
#       equivalents for your distribution:
#-------------------------------------------------------------------------------
RUN apt-get update && apt-get install -y curl gnupg2 git \
        python-is-python3 python3 python3-setuptools python3-pip \
        python3-build python3-wheel python3-venv \
        build-essential crossbuild-essential-arm64 \
        zip unzip \
        apt-transport-https ca-certificates gnupg-agent \
        software-properties-common \
        pkg-config libffi-dev patch diffutils libssl-dev iptables kmod \
        clang clangd crossbuild-essential-amd64 erofs-utils busybox-static \
        iproute2 netcat-openbsd libnuma-dev

#-------------------------------------------------------------------------------
# The rest of the file is unnecessary for building `runsc`, but are necessary
# for test targets and/or dev containers.
#-------------------------------------------------------------------------------

# Install Docker client for the website build.

RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
      gpg --dearmor --yes --output /usr/share/keyrings/docker.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu" \
      "$(lsb_release -cs)" \
      "stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get -y install docker-ce-cli

# Install gcloud.
RUN ARCH="$(uname -m | sed s/aarch64/arm/)" && \
    curl -fsSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-535.0.0-linux-${ARCH}.tar.gz | \
    tar zxf - google-cloud-sdk && \
    google-cloud-sdk/install.sh --quiet && \
    ln -s /google-cloud-sdk/bin/gcloud /usr/bin/gcloud

# The Bazel APT repository does not provide arm64 binaries.
COPY bazelversion bazel.sha256 install-bazel /tmp/install-bazel/
RUN /tmp/install-bazel/install-bazel /usr/local/bin && rm -r /tmp/install-bazel
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/bazel"]
