#!/bin/zsh
# 重新生成四段电视试播样片（各 20 秒，1080p 杜比视界 P8.1 合成彩条）。
# 依赖：ffmpeg（含 libx265）、dovi_tool、mkvmerge（brew install ffmpeg dovi_tool mkvtoolnix），
#       以及 macOS 自带的 say（中文语音 Tingting；没有的话换成 `say -v '?'` 里任一 zh_CN 语音）。
# 用法：zsh 生成样片.sh        成品写到本脚本所在目录，中间文件用完即删。
set -euo pipefail

OUT="${0:A:h}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

FRAMES=480
VOICE="Tingting"

# 1) 画面：10bit HDR10 彩条，带 B 帧；再注入一份生成的杜比视界 P8.1 元数据
ffmpeg -y -loglevel error -f lavfi -i "testsrc2=s=1920x1080:r=24000/1001" -frames:v $FRAMES \
  -c:v libx265 -preset fast -crf 20 -pix_fmt yuv420p10le \
  -x265-params "bframes=4:log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:repeat-headers=1:aud=1:hrd=0:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400" \
  -f hevc base.hevc
printf '{"cm_version":"V40","length":%d,"level6":{"max_display_mastering_luminance":1000,"min_display_mastering_luminance":1,"max_content_light_level":1000,"max_frame_average_light_level":400}}' $FRAMES > gen.json
dovi_tool generate -j gen.json -o RPU.bin > /dev/null
dovi_tool inject-rpu -i base.hevc --rpu-in RPU.bin -o dv.hevc > /dev/null
mkvmerge -q -o video.mkv --default-duration 0:24000/1001fps dv.hevc

# 2) 音轨：每条循环播报自己的名字，底下垫一个很轻的不同音调
make_audio() {
  local name=$1 text=$2 tone=$3
  say -v "$VOICE" -o "v_${name}.aiff" "$text"
  ffmpeg -y -loglevel error -stream_loop -1 -i "v_${name}.aiff" -f lavfi -i "sine=f=${tone}:r=48000" \
    -filter_complex "[0:a]aresample=48000,volume=2.0[v];[1:a]volume=0.06[t];[v][t]amix=inputs=2:duration=shortest:normalize=0,pan=5.1|FL=c0|FR=c0|FC=c0|LFE=0*c0|BL=0.3*c0|BR=0.3*c0[a]" \
    -map "[a]" -t 20.02 "mix_${name}.wav"
}
make_audio eac3   "现在播放的是，E A C 3，兼容音轨。"   440
make_audio truehd "现在播放的是，True H D，原始音轨。" 880
make_audio dts    "现在播放的是，D T S，原始音轨。"    220
ffmpeg -y -loglevel error -i mix_eac3.wav   -c:a eac3 -b:a 640k            eac3.mka
ffmpeg -y -loglevel error -i mix_truehd.wav -c:a truehd -strict -2         truehd.mka
ffmpeg -y -loglevel error -i mix_dts.wav    -c:a dca -strict -2 -b:a 1509k dts.mka

# 3) 四个样片
E=(-metadata:s:a:0 "title=EAC3 兼容轨" -metadata:s:a:0 language=chi -disposition:a:0 default)
ffmpeg -y -loglevel error -i video.mkv -i eac3.mka -map 0:v:0 -map 1:a:0 -c copy -tag:v hvc1 "${E[@]}" \
  -metadata title="试播A 基准 DV MP4 EAC3" -strict unofficial -movflags +faststart \
  "$OUT/试播A-基准-DV-MP4-只有EAC3.mp4"
ffmpeg -y -loglevel error -i video.mkv -i eac3.mka -i truehd.mka -map 0:v:0 -map 1:a:0 -map 2:a:0 -c copy -tag:v hvc1 "${E[@]}" \
  -metadata:s:a:1 "title=TrueHD 原轨" -metadata:s:a:1 language=chi -disposition:a:1 0 \
  -metadata title="试播B DV MP4 EAC3+TrueHD" -strict experimental -movflags +faststart \
  "$OUT/试播B-DV-MP4-EAC3加TrueHD.mp4"
ffmpeg -y -loglevel error -i video.mkv -i eac3.mka -i dts.mka -map 0:v:0 -map 1:a:0 -map 2:a:0 -c copy -tag:v hvc1 "${E[@]}" \
  -metadata:s:a:1 "title=DTS 原轨" -metadata:s:a:1 language=chi -disposition:a:1 0 \
  -metadata title="试播C DV MP4 EAC3+DTS" -strict experimental -movflags +faststart \
  "$OUT/试播C-DV-MP4-EAC3加DTS.mp4"
mkvmerge -q -o "$OUT/试播D-DV-MKV-EAC3加TrueHD加DTS.mkv" --title "试播D DV MKV EAC3+TrueHD+DTS" video.mkv \
  --language 0:chi --track-name "0:EAC3 兼容轨"   --default-track-flag 0:yes eac3.mka \
  --language 0:chi --track-name "0:TrueHD 原轨" --default-track-flag 0:no  truehd.mka \
  --language 0:chi --track-name "0:DTS 原轨"    --default-track-flag 0:no  dts.mka

echo "已生成："
ls -1 "$OUT"/试播*.mp4 "$OUT"/试播*.mkv
