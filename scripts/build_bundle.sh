#!/usr/bin/env bash
# Build the linux/arm64 Lambda bundle (build/lambda) and stage the image build context
# (build/image-src) for CodeBuild. No Docker: wheels are downloaded for the target platform.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
STAGE=$(mktemp -d build/stage.XXXXXX)
uv pip install --python-platform aarch64-manylinux_2_28 --python-version 3.14 --target "$STAGE/lambda" \
  --only-binary=:all: --exclude-newer "7 days" --exclude-newer-package "pillow=2026-10-18T00:00:00Z" --quiet -r src/functions/requirements.txt
cp -R src/edge_pipeline "$STAGE/lambda/edge_pipeline"
cp src/functions/handlers.py "$STAGE/lambda/handlers.py"
mkdir -p "$STAGE/image-src/src"
cp -R containers greengrass "$STAGE/image-src/"
cp -R src/edge_pipeline "$STAGE/image-src/src/edge_pipeline"
for d in "$STAGE/lambda" "$STAGE/image-src"; do
  find "$d" -name "*.pyc" -type f -delete
  find "$d" -name "__pycache__" -type d -empty -delete
  find "$d" -type d -exec chmod 755 {} +
  find "$d" -type f -exec chmod 644 {} +
done
# Replace the previous build atomically (old directories are moved aside, not deleted recursively).
for name in lambda image-src; do
  [ -d "build/$name" ] && mv "build/$name" "$STAGE/old-$name"
  mv "$STAGE/$name" "build/$name"
done
echo "$STAGE" >> build/.stale-stages   # left-over staging directories can be removed by hand
du -sh build/lambda build/image-src
