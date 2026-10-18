FROM redis:7.0.12

RUN printf 'intentional source-builder diagnostic failure\n' >&2; exit 42

RUN apt-get update && apt-get install -y net-tools
