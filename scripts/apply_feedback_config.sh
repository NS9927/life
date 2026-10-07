#!/bin/bash
# 按 docs/真机反馈与根因分析.md 的建议，收敛「爬楼逐条补回复 / 即时消息不回」。
#
# 用法（在 PowerShell 里）：
#   dry-run（默认，只看 diff，不写）：
#     wsl -d Ubuntu-24.04 -u root -e bash /mnt/c/Users/<你的用户目录>/Projects/life/scripts/apply_feedback_config.sh
#   真改（会先备份，再重启 AstrBot）：
#     wsl -d Ubuntu-24.04 -u root -e bash .../apply_feedback_config.sh --apply
#
# 改什么：
#   1. 关掉 astrbot_plugin_group_context_flow 的注入（保留内置那份，life 的插话正靠内置 active_reply）
#   2. 群历史注入窗口 700 -> 150、group_message_max_cnt 1000 -> 200
#   3. reply_with_quote true -> false（最能消除「他在翻前面的一个个回」的观感）
#   4. segmented_reply.words_count_threshold 400 -> 2000（否则一次回答仍会被拆成多条）
#
# 会同时改 cmd_config.json 和所有 abconf_*.json：实际生效的是会话绑定的配置档，
# 全局那份改不改都行，一起改省得下次搞混。
set -e

APPLY=0
[ "$1" = "--apply" ] && APPLY=1

docker exec -i -e APPLY="$APPLY" astrbot python - <<'PY'
import glob, json, os, pathlib, shutil, time

APPLY = os.environ.get("APPLY") == "1"
STAMP = time.strftime("%Y%m%d_%H%M%S")

TARGETS = ["/AstrBot/data/cmd_config.json"] + [
    p for p in glob.glob("/AstrBot/data/config/abconf_*.json") if not p.endswith((".bak",))
]
TARGETS.append("/AstrBot/data/config/astrbot_plugin_group_context_flow_config.json")

changes = []


def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))


def save(path, data):
    pathlib.Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig"
    )


for path in TARGETS:
    if not pathlib.Path(path).exists():
        continue
    try:
        data = load(path)
    except Exception as exc:
        print(f"[跳过] {path}: {exc}")
        continue

    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    touched = []

    # 1) flow 插件：关掉重复注入
    flow = data.get("flow_settings")
    if isinstance(flow, dict) and flow.get("enabled") is True:
        flow["enabled"] = False
        touched.append("flow_settings.enabled: true -> false")

    # 2) 内置群上下文窗口
    ltm = data.get("provider_ltm_settings")
    if isinstance(ltm, dict):
        if ltm.get("group_message_history_max_cnt") not in (None, 150):
            touched.append(
                f"group_message_history_max_cnt: {ltm['group_message_history_max_cnt']} -> 150"
            )
            ltm["group_message_history_max_cnt"] = 150
        if ltm.get("group_message_max_cnt") not in (None, 200):
            touched.append(f"group_message_max_cnt: {ltm['group_message_max_cnt']} -> 200")
            ltm["group_message_max_cnt"] = 200

    # 3)+4) 平台回复形态
    ps = data.get("platform_settings")
    if isinstance(ps, dict):
        if ps.get("reply_with_quote") is True:
            touched.append("platform_settings.reply_with_quote: true -> false")
            ps["reply_with_quote"] = False
        seg = ps.get("segmented_reply")
        if isinstance(seg, dict) and seg.get("words_count_threshold") not in (None, 2000):
            touched.append(
                f"segmented_reply.words_count_threshold: {seg['words_count_threshold']} -> 2000"
            )
            seg["words_count_threshold"] = 2000

    if not touched:
        continue

    name = pathlib.Path(path).name
    print(f"\n=== {name} ===")
    for line in touched:
        print(f"    {line}")

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if before == after:
        continue

    changes.append((path, data))

if not changes:
    print("\n没有需要改的（可能已经改过了）")
else:
    if APPLY:
        for path, data in changes:
            backup = f"{path}.bak_feedback_{STAMP}"
            shutil.copy2(path, backup)
            save(path, data)
            print(f"[已写入] {path}（备份 {pathlib.Path(backup).name}）")
        print(f"\n共改 {len(changes)} 个文件。")
    else:
        print(f"\n[dry-run] 共需改 {len(changes)} 个文件。加 --apply 才会真写（写入前会备份）。")
PY

if [ "$APPLY" = "1" ]; then
  echo
  echo "=== 重启 AstrBot ==="
  docker restart astrbot >/dev/null
  sleep 30
  docker logs --since 1m astrbot 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "Loading plugin life|已注册 4 个" | tail -3
fi
