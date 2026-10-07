# life

AstrBot 拟人化**闸门**插件项目。

> 仓库：<https://github.com/NS9927/life> · 许可：GPL-3.0（见 [LICENSE](LICENSE)）

> **当前状态：设计完成，代码未实现。**
> 下一个会话直接从 `docs/可行性报告与设计.md` 开工，本文件只做导航。

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
├── docs/
│   ├── 可行性报告与设计.md          ★ 主文档，开工先读这个
│   └── 灵犀fork-审计报告.md         相关参考（第三方插件审计）
├── astrbot_plugin_reply_gate/       插件本体（可整包拷进 data/plugins/）
│   ├── metadata.yaml
│   ├── _conf_schema.json            按设计草案写好的配置项
│   ├── main.py                      骨架：能加载、能部署、逻辑待填
│   └── core/                        模块目录（待新建 schedule/gate/queue/delay）
├── tests/                           单元测试（schedule / gate 优先）
└── scripts/
    └── deploy.sh                    部署到 WSL 并重启 AstrBot
```

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

## 开工顺序

见 `docs/可行性报告与设计.md` 第六节。摘要：

1. 验主动消息插件的调度层（唯一未验证项）
2. `core/schedule.py` + 单测（纯函数，最容易先跑通）
3. `core/gate.py` + 单测（含连续丢弃逻辑）
4. 接高优先级 handler，验证 `possibility_reply` 被动态改写且生效
5. 睡眠拦截 + 队列
6. 回复延迟
7. 端到端观察一天

---

## 相关外部文件

| 文件 | 说明 |
|---|---|
| `C:\<你的用户目录>\Documents\deepseek-harness\default-workspace\神秘猫娘-人设提示词-v4.md` | 机器人人格 |
| `...\plugin_at_inline\main.py` | 已有插件：@ 同行 + 接管引用（含「不 @ 自己」守卫） |
| `...\plugin_sender_qq\main.py` | 已有插件：注入发言人 QQ 供认人 |
| `C:\<你的用户目录>\.dsh\skills\qq-chat-decrypt\SKILL.md` | QQ 聊天记录解密 skill |

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
