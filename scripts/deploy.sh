#!/bin/bash
# 把插件部署到 WSL 里的 AstrBot 并重启。
# 用法（PowerShell），路径换成你自己的：
#   wsl -d Ubuntu-24.04 -u root -e bash /mnt/c/<你的用户目录>/Projects/life/scripts/deploy.sh
set -e

# 从脚本自身位置推导项目根，避免写死用户名 / 工作区路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$SCRIPT_DIR/.." && pwd)/astrbot_plugin_reply_gate"
DST=/home/bot/bot/data/plugins/astrbot_plugin_reply_gate

echo "=== 同步插件 ==="
echo "源：$SRC"
echo "目标：$DST"
rm -rf "$DST"
cp -r "$SRC" "$DST"
find "$DST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chown -R 1000:1000 "$DST"
ls -la "$DST"

echo
echo "=== 重启 AstrBot ==="
docker restart astrbot
sleep 30
docker ps --format '{{.Names}} {{.Status}}'

echo
echo "=== 插件加载日志 ==="
docker logs astrbot 2>&1 | grep -iE "reply_gate|Loading plugin" | tail -12
