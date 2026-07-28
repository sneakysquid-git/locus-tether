# Dockerfile for the Omi -> Thor pipeline (watcher + faster-whisper).
#
# Values below are confirmed against the real device (not placeholders):
#   - L4T release: R39.2 / JetPack 7.2 (from `cat /etc/nv_tegra_release`)
#   - Compute capability: 11.0 (from `nvidia-smi --query-gpu=compute_cap`),
#     i.e. CMAKE_CUDA_ARCHITECTURES=110 — trust nvidia-smi on real hardware
#     over secondary sources that reference sm_101 for Thor.
#   - CUDA toolkit: 13.2, per community reports building on this exact
#     hardware/L4T combo (dusty-nv/jetson-containers issue #1661).
#
# If the exact `l4t-jetpack:r39.2.0` tag below 404s on NGC (JetPack 7.2 is
# very recent, the matching container tag may lag the OS release slightly),
# check https://catalog.ngc.nvidia.com/orgs/nvidia/containers/l4t-jetpack/tags
# for the closest available r39.x tag.
#
# Known gotcha (per jetson-containers issue #1661, same hardware/L4T combo):
# some Jetson-specific pip indexes (e.g. pypi.jetson-ai-lab.io) are missing
# common packages like numpy/huggingface_hub for the cu132/sbsa target,
# causing installs to fail with "not found in package registry." This
# Dockerfile installs from standard PyPI by default, but if the base image's
# pip config redirects elsewhere and installs start failing that way, add
# `--extra-index-url https://pypi.org/simple` to the affected pip3 commands.
#
# This container is meant to run with the NVIDIA container runtime. On this
# machine that's already the Docker default (`default-runtime: nvidia` in
# /etc/docker/daemon.json), so no extra flag is strictly needed, but
# docker-compose.yml sets `runtime: nvidia` explicitly anyway — Thor does NOT
# support the more common `--gpus=all` flag/option, only `--runtime=nvidia`.

FROM nvcr.io/nvidia/l4t-jetpack:r39.2.0

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
