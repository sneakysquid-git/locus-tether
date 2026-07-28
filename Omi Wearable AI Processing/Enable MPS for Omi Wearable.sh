#!/bin/bash
# Run this ON THE THOR HOST (not inside a container) to start the CUDA MPS
# control daemon. Containers then connect to it as clients via the same
# pipe/log directories, mounted in via docker-compose.
#
# Safe to run this once and leave it running indefinitely — it's a lightweight
# always-on daemon, not something you start/stop per-workload. Workloads
# (this pipeline's container, and separately your robotics processes) each
# just set CUDA_MPS_ACTIVE_THREAD_PERCENTAGE for their own share when they
# start, and MPS enforces it while they're running.
set -euo pipefail

export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-log

mkdir -p "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"

if pgrep -f nvidia-cuda-mps-control > /dev/null; then
    echo "MPS control daemon already running."
else
    echo "Starting MPS control daemon..."
    nvidia-cuda-mps-control -d
    sleep 1
    if pgrep -f nvidia-cuda-mps-control > /dev/null; then
        echo "MPS control daemon started."
    else
        echo "MPS control daemon failed to start — check that this device's"
        echo "JetPack/CUDA version actually ships nvidia-cuda-mps-control"
        echo "(MPS has been supported on Jetson since CUDA 12.5 / JetPack 6.1,"
        echo "but confirm on your exact Thor image with:"
        echo "  which nvidia-cuda-mps-control"
        exit 1
    fi
fi

echo
echo "Pipe directory: $CUDA_MPS_PIPE_DIRECTORY"
echo "Log directory:  $CUDA_MPS_LOG_DIRECTORY"
echo
echo "To make this persistent across reboots, install"
echo "docker/host-setup/nvidia-mps.service via systemd (see README)."
