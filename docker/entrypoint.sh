#!/bin/bash
# Entrypoint for the omi-pipeline container.
#
# The MPS control daemon itself runs on the HOST (Thor), not in this
# container — see docker/host-setup/enable-mps.sh. This container just needs
# to act as an MPS *client*, which means:
#   1. CUDA_MPS_PIPE_DIRECTORY / CUDA_MPS_LOG_DIRECTORY point at the same
#      directories the host daemon is using (mounted in via docker-compose).
#   2. CUDA_MPS_ACTIVE_THREAD_PERCENTAGE caps how much of the GPU's SMs this
#      container's CUDA context is allowed to use — leaving the rest free for
#      robotics/other work. Defaults to 25% if not set explicitly.
set -euo pipefail

export CUDA_MPS_PIPE_DIRECTORY="${CUDA_MPS_PIPE_DIRECTORY:-/tmp/nvidia-mps}"
export CUDA_MPS_LOG_DIRECTORY="${CUDA_MPS_LOG_DIRECTORY:-/tmp/nvidia-log}"
export CUDA_MPS_ACTIVE_THREAD_PERCENTAGE="${CUDA_MPS_ACTIVE_THREAD_PERCENTAGE:-25}"

echo "[entrypoint] MPS pipe dir: ${CUDA_MPS_PIPE_DIRECTORY}"
echo "[entrypoint] MPS thread cap: ${CUDA_MPS_ACTIVE_THREAD_PERCENTAGE}%"

if [ ! -d "${CUDA_MPS_PIPE_DIRECTORY}" ]; then
    echo "[entrypoint] WARNING: MPS pipe directory not found — is the host MPS"
    echo "  control daemon running, and is docker/host-setup/enable-mps.sh's"
    echo "  directory mounted into this container at the same path?"
    echo "  Continuing anyway; CUDA will fall back to default time-slicing"
    echo "  instead of the MPS thread-percentage cap."
fi

exec "$@"
