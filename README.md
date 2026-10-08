# life

AstrBot 拟人化**闸门**插件项目。

> 仓库：<https://github.com/NS9927/life> · 许可：GPL-3.0（见 [LICENSE](LICENSE)）

> **当前状态：v0.4.0 已在真机（WSL 里的 AstrBot v4.28.2）跑通，175 个单测通过。**
> 2026-10-07 真机验证：闸门判定、内置插话概率改写、按发送者连丢兜底、低活跃度攒批回复、
> 插件页面接口全部实测生效。设计依据见 `docs/可行性报告与设计.md`，改动前先读它。

---

## 做什么

一个插件，两个闸门，**AND 关系**（都放行才开口）：

| 闸门 | 作用 |
|---|---|
| **作息闸门** | 按时段活跃度**概率放行**消息。凌晨 0 不发，晚上 80 多发 |
| **已读不回闸门** | 静默名单 + 循环熔断，防两个 bot 互刷烧 token |

设计上的关键立场：

- **概率放行只对「没人问它」的消息才像人**（主动开口 / 群聊插话）
- **被 @ / 被私聊绝不能用概率** —— 真人被点名一定会回，随机不回 = 像坏了
- 睡眠时段对「被点名」的消息**排队起床补发**，对「群聊闲聊」直接丢

---

## 目录

```text
life/
├── README.md                        本文件
├── LICENSE                          GPL-3.0 全文
├── .gitattributes                   统一 LF（否则 deploy.sh 在 WSL 里报 \r 错）
├── docs/
│   ├── 可行性报告与设计.md          ★ 主文档，含源码级证据与机制修正
│   ├── 灵犀fork-审计报告.md         相关参考（第三方插件审计）
│   └── 真机反馈与根因分析.md        ★ 上线后群里的真实反馈 + 逐层根因 + 该改哪个字段
├── life/       插件本体（可整包拷进 data/plugins/）
│   ├── metadata.yaml
│   ├── _conf_schema.json            配置面板（含 session_cooldown）
│   ├── main.py                      只做事件适配与钩子注册，不写判定规则
│   ├── core/                        判定逻辑，零框架依赖，可单测
│   ├── webapi.py                    插件页面的后端接口（register_web_api）
│   └── pages/                       WebUI 页面（自动出现在侧边栏）
│       ├── schedule.py              作息表 → 0~1 活跃度（切点插值 + 周末系数）
│       ├── classify.py              事件标志位 → 被点名 / 群聊插话
│       ├── gate.py                  判定链 + 循环熔断 + 会话冷却 + 连续丢弃兜底
│       ├── queue.py                 睡眠队列 + 起床补发文案 + 定时判定
│       ├── delay.py                 按作息的回复延迟
│       └── coerce.py                配置值容错（配置填错不许把插件搞崩）
├── tests/                           126 个单测（含桩掉 astrbot 的接线测试）
└── scripts/
    └── deploy.sh                    部署到 WSL 并重启 AstrBot
```

## 跑测试

不需要 AstrBot 环境、不需要装 pytest（只用标准库）：

```powershell
python -m unittest discover -s tests -v
```

`core/` 里的模块**一律不 import astrbot**（时间和随机数从外面注入），所以纯逻辑可以离线测；
`tests/test_main_wiring.py` 会把 `astrbot` 的符号桩掉，真跑一遍 main.py 的钩子，
验证「被 @ 绝不用概率」「丢弃时 stop_event」「放行时把内置概率顶成 1.0」这些接线是否成立。

---

## 关键结论速览（新会话不用重查）

三条已经验证过的代码事实，**都是有行号证据的**：

| # | 结论 | 证据 |
|---|---|---|
| 1 | `active_reply.possibility_reply` **每轮重读内存配置对象**，插件可动态改写 | `group_chat_context.py:58` 每轮调 `get_config()`；`astrbot_config_mgr.py:161` 返回缓存的同一对象 |
| 2 | 改内存**不落盘**，零 I/O | `AstrBotConfig(dict)` 未重写 `__setitem__`（`astrbot_config.py:33`） |
| 3 | 插件 handler 在**第 1 阶段**派发，LLM 在**第 7 阶段**，`stop_event()` 可完全跳过 LLM | `stage_order.py` + `scheduler.py:76` |

→ 结论 3 意味着「已读不回」是**真的 0 token**，不是少花点钱。

**唯一未验证**：主动消息插件（proactive_chat / 灵犀）的 `task_scheduler` 是否也每轮重读配置。开工第一件事验这个。

---

## 运行环境

| | |
|---|---|
| AstrBot | v4.28.2，跑在 WSL `Ubuntu-24.04` 里（Docker 容器 `astrbot` / `napcat`） |
| 插件目录（WSL） | `/home/bot/bot/data/plugins/` |
| 插件目录（容器内） | `/AstrBot/data/plugins/` |
| 改完生效 | `docker restart astrbot` |
| WSL 常驻 | 启动文件夹里的 `astrbot-wsl.vbs`（`sleep infinity` 撑住发行版） |
| AstrBot 面板 | http://127.0.0.1:6185 |
| NapCat 面板 | http://127.0.0.1:6099 |

### 部署

在 PowerShell 里：

```powershell
wsl -d Ubuntu-24.04 -u root -e bash /mnt/c/<你的用户目录>/Projects/life/scripts/deploy.sh
```

> `deploy.sh` 通过 `BASH_SOURCE` 自己推导项目根目录，所以**换机器/换路径不用改脚本**，
> 上面这条命令里的路径换成你自己的即可。

---

## 进度

设计文档第六节列的七步，现在的状态：

| # | 步骤 | 状态 |
|---|---|---|
| 1 | 验主动消息插件的调度层 | ✅ **已定案：路线 A 不成立**，必须自研调度（证据见设计文档第三节） |
| 2 | `core/schedule.py` + 单测 | ✅ 完成（28 测） |
| 3 | `core/gate.py` + 单测（含连续丢弃） | ✅ 完成（31 测），另加循环熔断与会话冷却 |
| 4 | 高优先级 handler + 动态改写 `possibility_reply` | ✅ 代码完成、接线已用桩测过（**未在真容器验证**） |
| 5 | 睡眠拦截 + 队列 + 起床补发 | ✅ 完成（补发为 MVP 文案版，见下） |
| 6 | 回复延迟 | ✅ 完成 |
| 7 | 端到端观察一天 | ⬜ **未做——下一步** |

**已知待办**（按重要性）：

1. **真机验证已完成**（2026-10-07）：闸门判定、内置概率改写、按发送者连丢兜底、低活跃度攒批、
   插件页面全部实测生效；被点名消息还加了时效上限 `addressed_max_age_seconds`（默认 180 秒，过期就丢）。
   群里反馈的根因分析与待收敛的配置见 `docs/真机反馈与根因分析.md`，
   一键脚本 `scripts/apply_feedback_config.sh`（默认 dry-run）。
2. **主动开口（PROACTIVE）没有实现**：它不走事件，需要自研调度器。`gate.py` 已经支持
   `MessageKind.PROACTIVE`，缺的是「什么时候该主动找人说话」的调度 + 话题生成。
3. **起床补发目前只发文案**（「刚看到…」+ 转述积压消息），没有把消息回炉给 LLM。
   要做真回复得接 `context.get_using_provider(umo)` 再 `text_chat()`。

---

## 相关外部文件

| 文件 | 说明 |
|---|---|
| `C:\<你的用户目录>\Documents\deepseek-harness\default-workspace\神秘猫娘-人设提示词-v4.md` | 机器人人格 |
| `...\plugin_at_inline\main.py` | 已有插件：@ 同行 + 接管引用（含「不 @ 自己」守卫） |
| `...\plugin_sender_qq\main.py` | 已有插件：注入发言人 QQ 供认人 |
| `C:\<你的用户目录>\.dsh\skills\qq-chat-decrypt\SKILL.md` | QQ 聊天记录解密 skill |

---

## 插件页面（AstrBot WebUI 侧边栏）

插件只有**一个**页面：`pages/面板/`。侧边栏里「拟人」点进去就是它（框架只会打开排在最前的那个 page，现在也只有这一个），**页内左侧还有一排菜单**（形态参照 `astrbot_plugin_steam_status_monitor_V3`：竖排、图标+文字、当前项高亮、窄屏堆叠）：**作息活跃度 / 基础闸门 / 回话策略 / 防刷与睡眠 / 主动开口 / 打字与拟人**。页面显示名走插件 i18n（`life/.astrbot-plugin/i18n/zh-CN.json` 的 `pages.面板.title/.description`，现在叫「拟人 · 控制台」），取不到才回退目录名。

| 页内菜单项 | 干什么 | 键数 |
|---|---|---:|
| **作息活跃度** | 48 个节点的折线图（每半小时一个），**上下拖节点**改权重，实时预览插值后的真实曲线，带 `夜猫子 / 早睡早起 / 全天半强度 / 全天静音` 预设；拖完自动保存 | 4 |
| **基础闸门** | 总开关、已读不回日志、延迟埋点、基础概率（插话/主动/被 @）、连丢兜底、静默名单、会话白名单 | 10 |
| **回话策略** | 被点名消息时效上限、被点名分档、回复延迟 | 10 |
| **防刷与睡眠** | 会话冷却、睡眠队列补发、循环熔断 | 12 |
| **主动开口** | 白名单、可打扰度时间线、四档概率、预算/冷却/抖动、超时与提示词 | 16 |
| **打字与拟人** | 分段与打字节奏（切分方式 regex/chars、切分正则或字符表、读打字速度、段数上限）、正在输入状态 | 19 |

**整页合计 71 个叶子键**（= schema 全部键），底部「保存」一次提交全部。

- 后端接口注册在 `webapi.py`，路由 `/life/page/*`（`context.register_web_api`）。
- **页面上改完立即生效**：保存后写进插件配置 → 重建内存对象 → 落盘，不用重启机器人。
- 保存只走白名单键（`SETTINGS_KEYS`），页面传什么都改不动别的配置；嵌套节是**就地合并**，不会把没显示的键抹掉。
- 折线图存的是 48 段半小时的 `time_weights`；插值半径默认 15 分钟（节点间距 30 分钟的一半），
  再大就会让相邻节点互相干扰。折线图拖动期间只请求预览曲线，**松手会立即落盘**，连续改动 900ms 防抖后再落盘一次。

---

## 许可

本项目以 **GNU General Public License v3.0** 发布，全文见 [`LICENSE`](LICENSE)。

```text
Copyright (C) 2026  NS9927

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

> 选择理由：本项目会调用 AstrBot 插件生态里的 AGPL-3.0 组件（如主动消息 / 灵犀 fork），
> GPL-3.0 第 13 条明确允许与 AGPL-3.0 作品链接合并，方向兼容。
