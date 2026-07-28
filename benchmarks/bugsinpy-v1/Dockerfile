FROM python:3.12-slim-trixie@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl dos2unix git vim \
       build-essential ccache pkg-config \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/0.11.33/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh

ENV PATH="/root/.local/bin/:$PATH"
ENV PATH="/usr/lib/ccache:$PATH"
ENV BUGSINPY_HOME="/home/bugsinpy/"
WORKDIR /evaluation
CMD ["bash"]
