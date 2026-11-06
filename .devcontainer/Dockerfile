FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates libudev1 libpcap0.8 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.6
