#!/bin/bash
set -euo pipefail

# 源码归档均固定到不可变版本并校验内容；运行时不依赖 Homebrew。
FFMPEG_TAG="n8.1.2"
FFMPEG_COMMIT="38b88335f99e76ed89ff3c93f877fdefce736c13"
FFMPEG_SHA256="9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"
X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"
X264_SHA256="cd71a7515b0e9a012e1ac9b1f8415bebcaf6fc97d4db32286642ac4c0fbe24f9"
NASM_VERSION="2.16.03"
NASM_SHA256="1412a1c760bbd05db026b6c0d1657affd6631cd0a63cddb6f73cc6d4aa616148"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/third_party/ffmpeg"
WORK="$ROOT/build/macos-ffmpeg"
PREFIX="$WORK/prefix"
SOURCE="$WORK/source"
JOBS="$(sysctl -n hw.ncpu)"

cache_is_valid() {
  [[ -x "$DEST/bin/ffmpeg" && -x "$DEST/bin/ffprobe" ]] || return 1
  [[ -f "$DEST/licenses/SOURCE-VERSIONS.txt" ]] || return 1
  file "$DEST/bin/ffmpeg" | grep -q "$(uname -m)" || return 1
  grep -Fq "FFmpeg peeled commit: $FFMPEG_COMMIT" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
  grep -Fq "FFmpeg archive SHA-256: $FFMPEG_SHA256" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
  grep -Fq "x264 commit: $X264_COMMIT" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
  grep -Fq "x264 archive SHA-256: $X264_SHA256" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
  grep -Fq "NASM version: $NASM_VERSION" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
  grep -Fq "NASM archive SHA-256: $NASM_SHA256" "$DEST/licenses/SOURCE-VERSIONS.txt" || return 1
}

if cache_is_valid; then
  "$DEST/bin/ffmpeg" -hide_banner -version
  "$DEST/bin/ffmpeg" -hide_banner -encoders > "$DEST/encoders.txt" 2>&1
  grep -q 'libx264' "$DEST/encoders.txt"
  grep -q 'h264_videotoolbox' "$DEST/encoders.txt"
  rm "$DEST/encoders.txt"
  exit 0
fi

rm -rf "$WORK" "$DEST"
mkdir -p "$SOURCE" "$PREFIX" "$DEST/bin" "$DEST/licenses"

curl -fL --retry 3 \
  "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/$FFMPEG_TAG.tar.gz" \
  -o "$SOURCE/ffmpeg.tar.gz"
curl -fL --retry 3 \
  "https://codeload.github.com/mirror/x264/tar.gz/$X264_COMMIT" \
  -o "$SOURCE/x264.tar.gz"
if [[ "$(uname -m)" == "x86_64" ]]; then
  curl -fL --retry 3 \
    "https://www.nasm.us/pub/nasm/releasebuilds/$NASM_VERSION/nasm-$NASM_VERSION.tar.xz" \
    -o "$SOURCE/nasm.tar.xz"
fi
echo "$FFMPEG_SHA256  $SOURCE/ffmpeg.tar.gz" | shasum -a 256 -c -
echo "$X264_SHA256  $SOURCE/x264.tar.gz" | shasum -a 256 -c -
if [[ "$(uname -m)" == "x86_64" ]]; then
  echo "$NASM_SHA256  $SOURCE/nasm.tar.xz" | shasum -a 256 -c -
fi

tar -xzf "$SOURCE/ffmpeg.tar.gz" -C "$SOURCE"
tar -xzf "$SOURCE/x264.tar.gz" -C "$SOURCE"
if [[ "$(uname -m)" == "x86_64" ]]; then
  tar -xJf "$SOURCE/nasm.tar.xz" -C "$SOURCE"
fi

export MACOSX_DEPLOYMENT_TARGET=13.0
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PATH="$PREFIX/bin:$PATH"

# Intel x264 需要 NASM；Runner 未预装，因此从固定源码构建到私有前缀。
if [[ "$(uname -m)" == "x86_64" ]]; then
  pushd "$SOURCE/nasm-$NASM_VERSION"
  ./configure --prefix="$PREFIX"
  make -j"$JOBS"
  make install
  popd
fi

pushd "$SOURCE/x264-$X264_COMMIT"
./configure \
  --prefix="$PREFIX" \
  --enable-static \
  --disable-cli \
  --disable-opencl
make -j"$JOBS"
make install
popd

pushd "$SOURCE/FFmpeg-${FFMPEG_TAG}"
./configure \
  --prefix="$PREFIX" \
  --cc=clang \
  --enable-gpl \
  --enable-libx264 \
  --enable-avfoundation \
  --enable-videotoolbox \
  --enable-static \
  --disable-shared \
  --disable-debug \
  --disable-doc \
  --disable-ffplay \
  --disable-sdl2 \
  --pkg-config-flags=--static \
  --extra-cflags="-I$PREFIX/include" \
  --extra-ldflags="-L$PREFIX/lib"
make -j"$JOBS"
make install
cp COPYING.GPLv2 COPYING.GPLv3 COPYING.LGPLv2.1 COPYING.LGPLv3 LICENSE.md "$DEST/licenses/"
popd

cp "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe" "$DEST/bin/"
cp "$SOURCE/x264-$X264_COMMIT/COPYING" "$DEST/licenses/x264-COPYING"
cp "$SOURCE/nasm-$NASM_VERSION/LICENSE" "$DEST/licenses/NASM-LICENSE" 2>/dev/null || true

"$DEST/bin/ffmpeg" -hide_banner -version
"$DEST/bin/ffmpeg" -hide_banner -encoders > "$DEST/encoders.txt" 2>&1
grep -q 'libx264' "$DEST/encoders.txt"
grep -q 'h264_videotoolbox' "$DEST/encoders.txt"
rm "$DEST/encoders.txt"

cat > "$DEST/licenses/SOURCE-VERSIONS.txt" <<EOF
FFmpeg tag: $FFMPEG_TAG
FFmpeg peeled commit: $FFMPEG_COMMIT
FFmpeg archive SHA-256: $FFMPEG_SHA256
x264 commit: $X264_COMMIT
x264 archive SHA-256: $X264_SHA256
NASM version: $NASM_VERSION
NASM archive SHA-256: $NASM_SHA256
EOF
