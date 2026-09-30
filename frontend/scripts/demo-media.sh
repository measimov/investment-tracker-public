#!/usr/bin/env bash
# 演示视频转码：playwright.demo.config.ts 录下的 webm →
#   docs/media/<name>.gif            README 内嵌（短片，800 宽、8 帧）
#   demo-output/release/<name>.mp4   GitHub Release 附件（完整分辨率，不进仓库）
# 用法：scripts/demo-media.sh [名称...]（默认 tour research walkthrough；GIF 只转前两个）
set -euo pipefail

cd "$(dirname "$0")/.."
VIDEOS=demo-output/videos
RELEASE=demo-output/release
MEDIA=../docs/media
GIF_NAMES=" tour research "

command -v ffmpeg >/dev/null || { echo "需要 ffmpeg" >&2; exit 1; }
mkdir -p "$RELEASE" "$MEDIA"

names=("$@")
[ ${#names[@]} -eq 0 ] && names=(tour research walkthrough)

for name in "${names[@]}"; do
  src="$VIDEOS/$name.webm"
  [ -s "$src" ] || { echo "缺少 $src（先跑 npx playwright test -c playwright.demo.config.ts -g 视频）" >&2; exit 1; }

  ffmpeg -loglevel error -y -i "$src" \
    -c:v libx264 -preset slow -crf 24 -pix_fmt yuv420p -movflags +faststart -an \
    "$RELEASE/$name.mp4"
  echo "mp4: $RELEASE/$name.mp4 ($(du -h "$RELEASE/$name.mp4" | cut -f1))"

  if [[ "$GIF_NAMES" == *" $name "* ]]; then
    ffmpeg -loglevel error -y -i "$src" \
      -vf "fps=8,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
      "$MEDIA/$name.gif"
    echo "gif: $MEDIA/$name.gif ($(du -h "$MEDIA/$name.gif" | cut -f1))"
  fi
done
