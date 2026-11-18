#!/usr/bin/env bash
# Fetch and build everything this server needs, for the GPU in this machine.
#   bash scripts/setup.sh            (CUDA 12.8+ with nvcc on PATH or in CUDA_HOME)
# MiraGe and Gaussifier are fetched into third_party/ (not part of this repository).
set -euo pipefail
cd "$(dirname "$0")/.."
MIRAGE_COMMIT=46c3121
GAUSSIFIER_TAG=v0.12.0
export CUDA_HOME=${CUDA_HOME:-$(dirname "$(dirname "$(command -v nvcc)")")}
export PATH="$CUDA_HOME/bin:$PATH"
ARCH=${CUDA_ARCH:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1)}
export TORCH_CUDA_ARCH_LIST=$ARCH
mkdir -p third_party

# MiraGe (research and evaluation licence, see third_party/MiraGe/LICENSE.md)
if [ ! -d third_party/MiraGe ]; then
  git clone https://github.com/waczjoan/MiraGe.git third_party/MiraGe
  git -C third_party/MiraGe checkout -q $MIRAGE_COMMIT
  git -C third_party/MiraGe submodule update --init submodules/simple-knn submodules/diff-gaussian-rasterization
  git -C third_party/MiraGe/submodules/diff-gaussian-rasterization submodule update --init third_party/glm
  # current compilers and Pillow
  sed -i '0,/#include/s//#include <cstdint>\n#include/' \
    third_party/MiraGe/submodules/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.h
  sed -i 's/dtype=np\.byte/dtype=np.uint8/g' \
    third_party/MiraGe/models/scenes/dataset_readers.py third_party/MiraGe/scene/dataset_readers.py
fi

# Gaussifier (fused CUDA runtime and weights)
if [ ! -d third_party/gaussifier ]; then
  git clone --branch $GAUSSIFIER_TAG https://github.com/Gaussifier/gaussifier.git third_party/gaussifier
  (
    cd third_party/gaussifier
    base=https://github.com/Gaussifier/gaussifier/releases/download/$GAUSSIFIER_TAG
    w=src/gaussifier_sampler/weights
    curl -L --create-dirs -o $w/gaussifier_best.pt $base/gaussifier_best.pt
    for f in fm_fp16.gffm fm_fp8.gffm head_fp16.gfch head_fp8.gfch stage1.pt head.pt precision_plan_release.json; do
      curl -L --create-dirs -o $w/frozen_gn/$f $base/$f
    done
    sha256sum -c release/SHA256SUMS
  )
fi
CMAKE_ARCH=${ARCH/./}
cmake -S third_party/gaussifier/cpp_inference -B third_party/gaussifier/cpp_inference/build \
  -DCMAKE_CUDA_ARCHITECTURES=$CMAKE_ARCH
cmake --build third_party/gaussifier/cpp_inference/build --parallel

# The MiraGe extensions build against the installed torch, so install everything else first.
uv sync --no-install-package simple-knn --no-install-package diff-gaussian-rasterization
uv sync
echo "done: run the server with  uv run mirage-gaussifier-mcp"
