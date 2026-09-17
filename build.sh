#!/bin/bash
# Build services with the egress block temporarily disabled.
cd "$(dirname "$0")"

echo "Temporarily lifting the egress block for the build."
sudo iptables -F DOCKER-USER

if [ $# -gt 0 ]; then
    docker compose build "$@"
else
    docker compose build
fi

BUILD_STATUS=$?

echo "Restoring the egress block."
sudo /usr/local/bin/indicode-firewall.sh

if [ $BUILD_STATUS -eq 0 ]; then
    echo "Build complete; airgap restored."
    docker compose up -d
else
    echo "Build failed; airgap restored."
fi

exit $BUILD_STATUS
