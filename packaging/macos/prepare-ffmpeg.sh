#!/bin/bash
set -euo pipefail

# 两个归档均固定到不可变版本并校验内容；运行时不依赖 Homebrew。
FFMPEG_TAG="n8.1.2"
FFMPEG_COMMIT="38b88335f99e76ed89ff3c93f877fdefce736c13"
FFMPEG_SHA256="9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"
X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"
X264_SHA256="cd71a7515b0e9a012e1ac9b1f8415bebcaf6fc97d4db32286642ac4c0fbe24f9"

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
  "https://code.videolan.org/videolan/x264/-/archive/$X264_COMMIT/x264-$X264_COMMIT.tar.gz" \
  -o "$SOURCE/x264.tar.gz"
echo "$FFMPEG_SHA256  $SOURCE/ffmpeg.tar.gz" | shasum -a 256 -c -
echo "$X264_SHA256  $SOURCE/x264.tar.gz" | shasum -a 256 -c -

tar -xzf "$SOURCE/ffmpeg.tar.gz" -C "$SOURCE"
tar -xzf "$SOURCE/x264.tar.gz" -C "$SOURCE"

export MACOSX_DEPLOYMENT_TARGET=13.0
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"

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
EOF
