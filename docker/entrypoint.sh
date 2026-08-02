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

# BUG CONFIRMED ON REAL HARDWARE: this container runs as root by default, so
# every transcript/analysis file it writes to the bind-mounted data
# directory ends up root-owned ON THE HOST. That's invisible until
# something else on the host (webapp.py's edit feature, running natively
# as your own user via systemd) tries to WRITE to one of those same files
# and hits a silent PermissionError.
#
# Deliberately fixing this via umask, NOT by changing which user the
# container runs as (`user:` in docker-compose.yml) — that alternative was
# considered and rejected: it would break write access to
# /root/.cache/huggingface (the diarization model cache, a named volume
# mounted at root's home specifically), which only root can reliably write
# to regardless of chown. umask 000 keeps files root-owned but makes them
# permissive enough (666/777) for any host user to still read/write them —
# fine for a single-user personal device like this, not something you'd
# want on a shared/multi-tenant machine.
umask 000

exec "$@"
