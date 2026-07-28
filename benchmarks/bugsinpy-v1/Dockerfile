FROM python:3.7-slim-bullseye@sha256:603879aeff06df23f0d392a19f7ce02aa85e14e6b4d25fd9647b116e0cb93467 AS python37

FROM python:3.12-slim-trixie@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl dos2unix git vim \
       build-essential ccache pkg-config \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/0.11.33/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh

COPY --from=python37 /usr/local /opt/python37
COPY --from=python37 /usr/lib/x86_64-linux-gnu/libcrypto.so.1.1 /opt/python37/lib/
COPY --from=python37 /usr/lib/x86_64-linux-gnu/libssl.so.1.1 /opt/python37/lib/

ENV PATH="/root/.local/bin/:/usr/local/bin:/opt/python37/bin:$PATH"
ENV LD_LIBRARY_PATH="/opt/python37/lib"
ENV PATH="/usr/lib/ccache:$PATH"
ENV BUGSINPY_HOME="/home/bugsinpy/"
WORKDIR /evaluation
CMD ["bash"]
