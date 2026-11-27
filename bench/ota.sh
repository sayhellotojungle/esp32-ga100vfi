#!/bin/bash
# 刷板 + **证明板上跑的真是这张镜像**。
#   usage: BOARD=http://<设备IP> IMG=$HOME/deploy/x.bin EXPECT=<sha256前缀> bash bench/ota.sh
#
# 为什么要有 EXPECT 这一步：这两天里"板上到底是哪个固件"反复无法回答 —— 旧镜像没有
# 版本字段，我们只能靠口头保证。新固件把编译时刻印进 /api/status 的 net.build，
# 所以刷完必须看到它，且值必须等于我们刚编出来的那一次，否则按刷失败处理。
set -uo pipefail
# 板子地址不给默认值：写死任何一台台架的地址，都会让下一个用它的人去打别人的卡。
if [ -z "${BOARD:-}" ]; then
  echo "[!!] BOARD 未设置：BOARD=http://<设备IP> bash $0（地址取板子串口横幅或 /api/status 的 net.ip）" >&2
  exit 2
fi
GA_BASE=${GA_BASE:-$HOME/ga100vfi}
IMG=${IMG:?用法: BOARD=http://<设备IP> IMG=镜像 [EXPECT=sha前缀] bash ota.sh}
EXPECT=${EXPECT:-}
# BOARD=http://IP/ 是最自然的写法，而设备的 HTTP 路由按整串 URI 匹配：多个斜杠的
# //api/ota 直接 404，六次重试全烧在同一个拼写上（2026-11-25 实测）。地址归一化在这里做，
# 不在调用者的记性里做。
BOARD=${BOARD%/}

systemctl stop ga100vfi-agent 2>/dev/null || true
pkill -9 -f 'ga100vfi ru[n]|ga100vfi_ag[ent]' 2>/dev/null || true
sleep 1
[ -f "$IMG" ] || { echo "[!!] 镜像不存在：$IMG"; exit 1; }
SHA=$(sha256sum "$IMG" | cut -c1-16)
echo "== 镜像 $IMG  $(stat -c %s "$IMG") B  sha256=$SHA =="
if [ -n "$EXPECT" ] && [ "$SHA" != "$EXPECT" ]; then
  echo "[!!] 镜像哈希不等于EXPECT=$EXPECT —— 拒绝刷不知哪来的文件"; exit 1
fi

echo "== 推送到 $BOARD/api/ota =="
ok=0
for try in 1 2 3 4 5 6; do
  code=$(curl -s -m 600 --data-binary "@$IMG" -H 'Content-Type: application/octet-stream' \
         -o /tmp/ota.body -w '%{http_code}' "$BOARD/api/ota" 2>/dev/null)
  echo "   第 $try 次 -> http ${code:-none} $(head -c 80 /tmp/ota.body 2>/dev/null | tr -d '\n')"
  [ "$code" = "200" ] && { ok=1; break; }
  sleep 4
done
[ "$ok" = 1 ] || { echo "[!!] OTA 六次都没成功，板子还是旧镜像"; exit 2; }

echo "== 等板子重启回来 =="
back=0
for i in $(seq 1 40); do
  sleep 3
  if curl -s -m 8 "$BOARD/api/status" > /tmp/st.json 2>/dev/null; then
    echo "   $((i*3))s 后应答"; back=1; break
  fi
done
[ "$back" = 1 ] || { echo "[!!] OTA 200 但板子 120s 没回来 —— 可能新镜像起不来，准备 USB 兜底"; exit 3; }

echo "== 版本自证：/api/status 的 net.build 必须是刚编的这一次 =="
BUILD=$(python3 - <<'PY'
import json
try:
    print(json.load(open("/tmp/st.json")).get("net", {}).get("build", ""))
except Exception as exc:
    print(f"<读取失败 {exc}>")
PY
)
echo "   net.build = ${BUILD:-<没有这个字段：板子还是旧镜像！>}"
if [ -z "${BUILD:-}" ]; then
  echo "[!!] 没有 net.build —— OTA 没生效或刷了不含版本字段的镜像。停在这里，别开火。"
  exit 4
fi
# 只看"有没有 net.build"是不够的：那只能证明板子上有个带版本字段的镜像，不能证明是刚推
# 的这张。0.5.0 第一次 OTA 就是靠这条差一点被当成成功 —— 上传回 200、板子重启回来自报
# 0.3.2（旧槽），差别只在版本号。镜像文件名本身就带版本号（esp32-ga100vfi-<版本>-app.bin），
# 所以拿它当期望值，不必再加一个参数，也不会出现"传了 A 的镜像、按 B 验收"。
WANT=$(basename "$IMG" | sed -n 's/^esp32-ga100vfi-\([0-9][.0-9]*\)-.*$/\1/p')
case "$WANT" in
  "") echo "   [!!] 镜像文件名里没有版本号，跳过版本比对（只做存在性检查）";;
  *) case "$BUILD" in
       "$WANT"*) echo "   [ok] 板子自报 $BUILD，与镜像 $WANT 一致";;
       *) echo "[!!] 板子自报 $BUILD，而镜像是 $WANT —— 刷上去的不是这张（或启动失败回滚）"
          exit 5;;
     esac;;
esac

echo "== 控制器状态（红线 1：只问一句 FUSECTRL）=="
if [ -f "$GA_BASE/bench/poison_check.sh" ]; then
  cd "$GA_BASE/host" && bash "$GA_BASE/bench/poison_check.sh" 2>&1 | sed 's/^/   /'
fi
echo "[ok] 刷写完成，可以进 crack.sh 的开火闸门了。"
