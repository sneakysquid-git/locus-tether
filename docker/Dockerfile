# Dockerfile for the Omi -> Thor pipeline (watcher + faster-whisper).
#
# Values below are confirmed against the real device (not placeholders):
#   - L4T release: R39.2 / JetPack 7.2 (from `cat /etc/nv_tegra_release`)
#   - Compute capability: 11.0 (from `nvidia-smi --query-gpu=compute_cap`),
#     i.e. CMAKE_CUDA_ARCHITECTURES=110.
#   - CUDA toolkit: 13.2, per community reports on this exact hardware/L4T
#     combo (dusty-nv/jetson-containers issue #1661).
#
# IMPORTANT: as of this build, NGC has NOT published an l4t-jetpack tag for
# L4T R39.x (verified directly against the registry API — the latest
# available tag is r36.4.0). Rather than wait on that, this Dockerfile uses
# plain ubuntu:24.04 as the base and relies on the NVIDIA container runtime's
# CSV mount mechanism (the Jetson-specific method of injecting CUDA/cuDNN/
# driver libraries from the HOST into any container at launch — this is how
# l4t-jetpack images get their CUDA too, it's not baked into the image layer)
# to supply CUDA 13.2 matching your actual host install. This should be more
# correct than a prebuilt tag anyway, since it's guaranteed to match your
# real hardware rather than whatever CUDA point-release NGC happened to bake in.
#
# One thing to verify before trusting the full build: whether the CSV mounts
# on this host include the CUDA *toolkit* (nvcc, headers) needed to compile
# against, not just runtime .so files needed to merely run precompiled CUDA
# binaries. Quick check before running the full build below:
#   docker run --rm --runtime nvidia ubuntu:24.04 bash -c "ls /usr/local/cuda/bin/nvcc"
# If that 404s / file-not-found's, the toolkit isn't being mounted and we'll
# need to install cuda-toolkit-13-2 via apt inside the container instead
# (NVIDIA's cuda-keyring package + apt, same as a normal x86 CUDA install,
# just for aarch64) rather than depending on the host mount for build-time use.
#
# This container is meant to run with the NVIDIA container runtime. On this
# machine that's already the Docker default (`default-runtime: nvidia` in
# /etc/docker/daemon.json), so no extra flag is strictly needed, but
# docker-compose.yml sets `runtime: nvidia` explicitly anyway — Thor does NOT
# support the more common `--gpus=all` flag/option, only `--runtime=nvidia`.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV CUDA_ARCH=110

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git curl wget \
    python3 python3-pip python3-dev \
    ffmpeg \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# --- Build ctranslate2 from source with CUDA support for Thor -------------
# Mirrors the approach used by community Jetson builds (e.g. cbinckly/speaches
# for Orin, which targets sm_87) but targeting Thor's confirmed sm_110.
# Do NOT `pip install ctranslate2` anywhere in this image outside this block —
# the PyPI aarch64 wheel is CPU-only and will silently shadow this GPU build.
WORKDIR /opt/build
RUN git clone --recursive https://github.com/OpenNMT/CTranslate2.git ctranslate2 \
    && cd ctranslate2 \
    && mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release \
             -DWITH_CUDA=ON \
             -DWITH_CUDNN=ON \
             -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
             -DWITH_MKL=OFF \
             -DWITH_OPENBLAS=ON \
             .. \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && cd /opt/build/ctranslate2/python \
    && pip3 install --no-cache-dir -r install_requirements.txt \
    && python3 setup.py bdist_wheel \
    && pip3 install --no-cache-dir dist/*.whl

# --- App dependencies -------------------------------------------------------
WORKDIR /app
COPY requirements-docker.txt .
# --no-deps is deliberate: faster-whisper's normal install would happily pull
# down the CPU-only PyPI ctranslate2 wheel and silently shadow the GPU build
# we just compiled above. Its other dependencies are pinned explicitly in
# requirements-docker.txt instead.
RUN pip3 install --no-cache-dir --no-deps faster-whisper \
    && pip3 install --no-cache-dir -r requirements-docker.txt

COPY config.py transcribe.py watcher.py ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "watcher.py"]