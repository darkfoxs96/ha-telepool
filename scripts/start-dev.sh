#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p dev/config
if [ ! -f dev/config/configuration.yaml ]; then
  cp dev/configuration.yaml dev/config/configuration.yaml
fi
if [ ! -f dev/config/telepool-test.yaml ]; then
  cp dev/telepool-test.yaml dev/config/telepool-test.yaml
fi
docker compose up -d
