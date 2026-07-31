#!/bin/bash
# NOT USED — kept for historical reference only. MPS was tried here for GPU
# sharing with robotics workloads, then deliberately DISABLED after
# confirming it causes Ollama's GPU discovery to hang indefinitely on this
# hardware — a genuine driver-level bug, not a config mistake. See the
# README's "MPS (Multi-Process Service)" glossary entry for the full story.
# Do not run this script; the pipeline now relies on default CUDA
# time-slicing instead.
#
# Original purpose (no longer applicable): run this ON THE THOR HOST (not
# inside a container) to start the CUDA MPS control daemon, so containers
# could connect to it as clients via shared pipe/log directories mounted in
# via docker-compose.
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
