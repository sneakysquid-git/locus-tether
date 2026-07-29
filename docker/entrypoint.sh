#!/bin/bash
# Entrypoint for the omi-pipeline container.
#
# MPS was tried here originally (to cap this container's GPU usage, leaving
# headroom for robotics work) and then deliberately disabled after confirming
# on real hardware that it causes Ollama's llama-server subprocess to hang
# indefinitely during GPU discovery — a genuine bug in how MPS interacts with
# Thor's still-maturing driver stack, not a config issue on our end. See
# docker-compose.yml's top comment for the full story. This container now
# just relies on default CUDA time-slicing to share the GPU, which is fine
# given how brief and infrequent this pipeline's actual GPU usage is.
set -euo pipefail

exec "$@"
