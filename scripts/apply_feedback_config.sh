#!/bin/bash
# 按 docs/真机反馈与根因分析.md 的建议，收敛「爬楼逐条补回复 / 即时消息不回」。
#
# 用法（PowerShell）：
#   看 diff（默认，不写）：
#     wsl -d Ubuntu-24.04 -u root -e bash /mnt/c/Users/<你的用户目录>/Projects/life/scripts/apply_feedback_config.sh
#   真改（先备份再写，然后重启 AstrBot）：
#     ... apply_feedback_config.sh --apply
#
# 群上下文注入只能留一个，用第二个参数选（默认 keep-flow）：
#   keep-flow      保留 astrbot_plugin_group_context_flow 的注入，关掉内置 group_icl_enable
#                  —— 落盘持久化（重启不丢）、按 conversation cursor 增量注入、/reset 不回流、
#                     且有 max_delta_messages 这个可控上限；关掉内置注入**不影响 life 的插话**
#                     （builtin main.py:145 `group_context_enabled = group_icl_enable or active_reply.enable`）
#   keep-builtin   保留内置注入，关掉 flow 插件
#                  —— 少一个第三方插件依赖，但内置那份存在内存里，重启即丢
#
# 两种模式都会顺手做：群历史窗口 700->150 / 1000->200、reply_with_quote 关、分段阈值 400->2000、
# 以及**上下文压缩**（max_turns -1->24 开轮次截断；fallback_max_tokens 128000->32000；
# keep_recent_ratio 0.15->0.3）——会话历史里的内联图片是「回复越来越慢」的真凶，详见
# docs/真机反馈与根因分析.md 第九节。
set -e

APPLY=0
MODE="keep-flow"
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    keep-flow|keep-builtin) MODE="$arg" ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

echo "模式: $MODE   写入: $([ "$APPLY" = "1" ] && echo 是 || echo '否（dry-run）')"

docker exec -i -e APPLY="$APPLY" -e MODE="$MODE" astrbot python - <<'PY'
import glob, json, os, pathlib, shutil, time

APPLY = os.environ.get("APPLY") == "1"
MODE = os.environ.get("MODE", "keep-flow")
STAMP = time.strftime("%Y%m%d_%H%M%S")

TARGETS = ["/AstrBot/data/cmd_config.json"] + [
    p for p in sorted(glob.glob("/AstrBot/data/config/abconf_*.json")) if not p.endswith((".bak",))
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

    touched = []

    # 1) 群上下文注入：二选一
    flow = data.get("flow_settings")
    if isinstance(flow, dict):
        want_flow = MODE == "keep-flow"
        if flow.get("enabled") is not want_flow:
            touched.append(f"flow_settings.enabled: {flow.get('enabled')} -> {want_flow}")
            flow["enabled"] = want_flow
        if want_flow:
            # 给增量注入加硬上限：0 = 不限量，正是上下文爆炸的来源
            if flow.get("max_delta_messages") in (0, None):
                touched.append("flow_settings.max_delta_messages: 0 -> 50")
                flow["max_delta_messages"] = 50
            if isinstance(flow.get("max_log_records"), int) and flow["max_log_records"] > 1000:
                touched.append(
                    f"flow_settings.max_log_records: {flow['max_log_records']} -> 1000"
                )
                flow["max_log_records"] = 1000

    ltm = data.get("provider_ltm_settings")
    if isinstance(ltm, dict):
        want_builtin_inject = MODE == "keep-builtin"
        if ltm.get("group_icl_enable") is not want_builtin_inject:
            touched.append(
                f"provider_ltm_settings.group_icl_enable: {ltm.get('group_icl_enable')} -> {want_builtin_inject}"
            )
            ltm["group_icl_enable"] = want_builtin_inject
        # 窗口收敛（不管哪种模式都做；DB 记录本身留着，别的功能要用）
        if ltm.get("group_message_history_max_cnt") not in (None, 150):
            touched.append(
                f"group_message_history_max_cnt: {ltm['group_message_history_max_cnt']} -> 150"
            )
            ltm["group_message_history_max_cnt"] = 150
        if ltm.get("group_message_max_cnt") not in (None, 200):
            touched.append(f"group_message_max_cnt: {ltm['group_message_max_cnt']} -> 200")
            ltm["group_message_max_cnt"] = 200

    # 2) 回复形态：少拆几条、别引用旧消息
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

    # 3) 上下文压缩：真凶不是注入，是**会话历史里的 base64 图片**在滚雪球
    #    实测单会话 2~11 MB、token_usage 最高 21 万，其中 90~99% 是内联图片。
    #    AstrBot 有两道闸（core/agent/context/manager.py:60-76）：
    #      轮次截断 max_turns  —— 现值 -1 = **完全关闭**
    #      token 压缩 fallback_max_tokens —— 现值 128000，等于不触发
    #    两道都形同虚设，所以历史无限涨。这里把两道都打开。
    ar = data.get("agent_runner")
    if isinstance(ar, dict):
        comp = (ar.get("config") or {}).get("compression")
        if isinstance(comp, dict):
            if comp.get("max_turns") in (-1, None):
                touched.append("agent_runner.config.compression.max_turns: -1 -> 24（开轮次截断）")
                comp["max_turns"] = 24
            if comp.get("trim_turns") in (1, None):
                touched.append("agent_runner.config.compression.trim_turns: 1 -> 6")
                comp["trim_turns"] = 6
            if (comp.get("fallback_max_tokens") or 0) > 32000:
                touched.append(
                    f"agent_runner.config.compression.fallback_max_tokens: "
                    f"{comp['fallback_max_tokens']} -> 32000"
                )
                comp["fallback_max_tokens"] = 32000
            if (comp.get("keep_recent_ratio") or 0) < 0.3:
                touched.append(
                    f"agent_runner.config.compression.keep_recent_ratio: "
                    f"{comp['keep_recent_ratio']} -> 0.3"
                )
                comp["keep_recent_ratio"] = 0.3

    if touched:
        print(f"\n=== {pathlib.Path(path).name} ===")
        for line in touched:
            print(f"    {line}")
        changes.append((path, data))

if not changes:
    print("\n没有需要改的（可能已经是目标状态）")
elif APPLY:
    for path, data in changes:
        backup = f"{path}.bak_feedback_{STAMP}"
        shutil.copy2(path, backup)
        save(path, data)
        print(f"[已写入] {path}（备份 {pathlib.Path(backup).name}）")
    print(f"\n共改 {len(changes)} 个文件。")
else:
    print(f"\n[dry-run] 共需改 {len(changes)} 个文件。加 --apply 才写（写入前自动备份）。")
PY

if [ "$APPLY" = "1" ]; then
  echo
  echo "=== 重启 AstrBot ==="
  docker restart astrbot >/dev/null
  sleep 30
  docker logs --since 1m astrbot 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "Loading plugin life|已注册 4 个" | tail -3
fi
