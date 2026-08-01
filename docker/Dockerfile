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
# available tag is r36.4.0). This Dockerfile uses plain ubuntu:24.04 as the
# base instead.
#
# An earlier version of this Dockerfile relied on the NVIDIA container
# runtime's CSV mount mechanism to inject the host's CUDA toolkit at
# container start. That does NOT reliably apply during `docker build` —
# BuildKit manages its own build workers and doesn't consistently honor
# `--runtime nvidia` / the daemon's default-runtime setting the way a plain
# `docker run` does (this is what caused the cmake CUDA-detection failure).
# Since compiling CUDA code (nvcc) doesn't need a live GPU anyway, this
# version installs the CUDA toolkit directly via apt instead — self-
# contained, no dependency on build-time runtime behavior.
#
# A second wrong turn along the way: the *generic* NVIDIA CUDA repo
# (developer.download.nvidia.com/.../ubuntu2404/arm64/) is for SBSA — server-
# class ARM64 with discrete GPUs (Grace-Hopper, Graviton, etc.) — and does
# NOT publish cuda-toolkit packages for Jetson/Tegra at all, only some shared
# packages like cuDNN (which is why that part appeared to work while the
# toolkit silently didn't exist there). The real source, confirmed by
# checking the actual host's /etc/apt/sources.list.d/nvidia-l4t-apt-source.list,
# is repo.download.nvidia.com/jetson/common — a completely different repo,
# trusted via the jetson-ota-public key rather than the generic cuda-keyring
# package. Both are used below.
#
# This container still needs `--runtime nvidia` (not `--gpus=all`, which
# Thor doesn't support) at RUN time, to actually access the GPU — that part
# was never the problem, only the build-time toolkit availability was.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# History of the CUDA-architecture flag for this build, in case it needs
# revisiting on a future CTranslate2/CMake version:
#   1st attempt: -DCMAKE_CUDA_ARCHITECTURES=110 — wrong variable name for
#      this project entirely; CTranslate2 doesn't use CMake's native CUDA
#      language variable, so this was silently ignored.
#   2nd attempt: -DCUDA_ARCH_LIST=11.0 — the right variable name (fed into
#      the legacy FindCUDA-based cuda_select_nvcc_arch_flags() macro), but
#      that macro (bundled with CMake 3.28) has a hardcoded, fixed table of
#      recognized GPU architecture names/numbers that simply doesn't include
#      Thor's compute capability (11.0 / sm_110) — it's newer than that
#      table. No amount of reformatting the number fixes this; it's not a
#      formatting problem, the entry just isn't there.
#   Final fix: skip that macro's lookup entirely. The RUN block below
#   sed-patches CTranslate2's CMakeLists.txt to hardcode the correct
#   -gencode flags for sm_110 directly, rather than asking CMake to resolve
#   them from a table that predates this hardware.

# --- Install CUDA 13.2 toolkit + cuDNN from the real Jetson (L4T) repo ------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget gnupg \
    && wget -qO /etc/apt/trusted.gpg.d/jetson-ota-public.asc \
        https://repo.download.nvidia.com/jetson/jetson-ota-public.asc \
    && echo "deb https://repo.download.nvidia.com/jetson/common r39.2 main" \
        > /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        cuda-toolkit-13-2 \
        libcudnn9-dev-cuda-13 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/usr/local/cuda/bin:${PATH}
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git curl \
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
    && sed -i 's/cuda_select_nvcc_arch_flags(ARCH_FLAGS \${CUDA_ARCH_LIST})/set(ARCH_FLAGS "-gencode;arch=compute_110,code=sm_110")/' CMakeLists.txt \
    && mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release \
             -DWITH_CUDA=ON \
             -DWITH_CUDNN=ON \
             -DWITH_MKL=OFF \
             -DWITH_OPENBLAS=ON \
             -DOPENMP_RUNTIME=COMP \
             .. \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig \
    && cd /opt/build/ctranslate2/python \
    && pip3 install --break-system-packages --no-cache-dir -r install_requirements.txt \
    && python3 setup.py bdist_wheel \
    && pip3 install --break-system-packages --no-cache-dir dist/*.whl

# --- App dependencies -------------------------------------------------------
WORKDIR /app
COPY docker/requirements-docker.txt .
# --no-deps is deliberate: faster-whisper's normal install would happily pull
# down the CPU-only PyPI ctranslate2 wheel and silently shadow the GPU build
# we just compiled above. Its other dependencies are pinned explicitly in
# requirements-docker.txt instead.
RUN pip3 install --break-system-packages --no-cache-dir --no-deps faster-whisper \
    && pip3 install --break-system-packages --no-cache-dir -r requirements-docker.txt

# --- Phase 1 diarization (speaker labels) -----------------------------------
# Unlike faster-whisper/CTranslate2 above (deliberately built from source to
# avoid a PyTorch dependency), alignment (wav2vec2) and diarization
# (pyannote.audio) are both PyTorch-native — confirmed PyTorch's official
# wheels install cleanly and detect CUDA correctly on this hardware (Jetson
# Thor, JetPack 7.2), so unlike CTranslate2 this did NOT need a from-source
# build. See pipeline/diarize.py for the actual integration — adapted from
# murtaza-nasir/whisperx-asr-service (MIT licensed).
#
# Deliberately a separate, optional layer: set ENABLE_DIARIZATION=false (see
# docker-compose.yml) to skip this entirely — useful on smaller GPUs where
# the added ~4GB isn't available. See HARDWARE.md.
RUN pip3 install --break-system-packages --no-cache-dir torch \
    && pip3 install --break-system-packages --no-cache-dir whisperx pyannote.audio

COPY pipeline/config.py pipeline/transcribe.py pipeline/watcher.py pipeline/prompts.py pipeline/analyzer.py pipeline/data_store.py pipeline/diarize.py ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "watcher.py"]
