# 灵犀 fork × 上游 审计报告

> 审计日期：2026-10-07
> 触发原因：`sulingqingQAQ/astrbot_plugin_lingxi` 是个 0 star 的个人 fork，计划跑在本机账号上，先审一遍「它到底改了什么」。

## 审计对象

| | 上游 | 灵犀 fork |
|---|---|---|
| 仓库 | Pancakes-Labs/astrbot_plugin_proactive_chat | sulingqingQAQ/astrbot_plugin_lingxi |
| 版本 | v1.2.6 | v2.1.0-dev.10 |
| star | 405 | 0 |
| 最后推送 | 2026-10-06（活跃） | 2026-09-23 |
| 体量 | 8,172 行 / 20 模块 | 10,500 行 / 27 模块 |
| 许可 | AGPL-3.0 | AGPL-3.0 |

## 方法

- 拉取两边 main 分支全量源码
- 文件级 diff（新增 / 删除 / 改动）
- 静态扫描：网络端点、危险调用（eval / exec / subprocess / base64 / pickle / env）、文件路径访问
- **未做**：逐行语义审查、运行时行为测试

---

## 一、结论摘要

1. **灵犀不是小改** —— 新增 10 个模块（约 5,269 行），删除 3 个上游模块。
2. **静态扫描未发现恶意代码** —— 无 eval/exec、无 subprocess、无 base64 混淆、无 pickle、不读环境变量、不碰 AstrBot 的数据库与密钥。
3. **真正「打电话回家」的是上游** —— 默认开启匿名遥测，把**配置快照**和错误上报到第三方 `plugincenter.aloys23.link`，并从同一服务拉取通知。**灵犀把这套整个删掉了。**
4. 结论：**隐私维度灵犀更干净；维护与稳定性维度上游更强。**

---

## 二、结构差异

### 2.1 灵犀新增的模块（10 个 / 约 5,269 行）

| 模块 | 行数 | 作用 |
|---|---:|---|
| core/group_enhance.py | 1,407 | 群历史注入（带 QQ ID）、图片转述、语音转写、historyPull |
| core/group_chime.py | 1,104 | 群聊接话：闸门、配额、直呼聚合、空行拆分连发 |
| core/forward_msg.py | 789 | 合并转发（聊天记录）解析 |
| core/enhance_web_search.py | 508 | grok 全网搜索 / X 搜索读帖 |
| build_plugin.py | 356 | 打包脚本（非运行时组件） |
| core/followup.py | 353 | 私聊回复后追发 |
| core/enhance_ban.py | 257 | LLM 封禁工具 |
| core/group_activity.py | 220 | 群活跃度数学评分（零 LLM 成本） |
| core/enhance_tag_utils.py | 192 | `<mention id/>` / `<quote id/>` 标签 → At/Reply 组件 |
| core/web_read.py | 83 | Jina Reader 读网页正文 |

### 2.2 被灵犀删除的上游模块（3 个）

| 模块 | 说明 |
|---|---|
| core/web_admin_server.py | 独立 WebUI（监听 127.0.0.1:4100） |
| core/notification_center.py | 从 plugincenter 拉取通知 |
| core/proactive_event.py | 事件定义 |

### 2.3 改动的公共模块

| 文件 | 上游 | 灵犀 | 变化 |
|---|---:|---:|---|
| core/telemetry_manager.py | 519 | 57 | **-462，遥测整体阉割为 no-op** |
| core/plugin_lifecycle.py | 407 | 238 | -169 |
| core/llm_adapter.py | 1,286 | 1,140 | -146 |
| core/chat_flow.py | 468 | 369 | -99 |
| core/session_parser.py | 217 | 203 | -14 |
| utils/time_utils.py | 38 | 25 | -13 |
| core/session_config.py | 143 | 150 | +7 |
| core/message_events.py | 360 | 375 | +15 |
| core/session_override_manager.py | 186 | 208 | +22 |
| core/task_scheduler.py | 748 | 806 | +58 |
| main.py | 262 | 421 | +159 |
| core/message_sender.py | 709 | 876 | +167 |

---

## 三、安全审计（核心）

### 3.1 网络端点（全量提取）

| 域名 | 上游 | 灵犀 | 说明 |
|---|---:|---:|---|
| `plugincenter.aloys23.link` | 2 | **0** | 上游的遥测 + 通知服务 |
| `r.jina.ai` | 0 | 2 | 灵犀的网页正文读取（文档化功能） |
| `127.0.0.1` / `localhost` | 2 | 0 | 上游的本地 WebUI |

**灵犀只多了 `r.jina.ai`，且它是 `enhance_web_read` 工具的实现（仅在用户明确要求读网页时调用）。它没有新增任何「回传」端点。**

### 3.2 危险调用

| 调用类型 | 上游 | 灵犀 |
|---|---:|---:|
| eval / exec | 0 | **0** |
| subprocess / os.system | 2 | **0** |
| base64 编解码 | 1 | **0** |
| pickle | 0 | 0 |
| 十六进制解码 | 0 | 0 |
| socket 裸连接 | 1 | 1（实为 `sqlite3.connect`，误报） |
| 动态导入 | 1 | 1（读取自身版本号） |
| 读环境变量 | 1 | **0** |
| 写文件 | 4 | 2 |
| 删文件 | 0 | 1（打包脚本清理临时目录） |

### 3.3 文件访问

扫描所有指向 `data_v4.db` / `cmd_config.json` / `.astrbot` / `api_key` / `token` / `credential` 的路径字面量：

**灵犀侧没有任何一条命中 AstrBot 的数据库、配置或密钥。** 命中的全部是它自己的文件或配置字段名：

| 位置 | 内容 |
|---|---|
| core/group_enhance.py:564 | `enhance_bans.db`（它自己的封禁库） |
| core/group_enhance.py:305 | `jina_api_key`（配置字段名） |
| core/web_read.py:24 | `Bearer {api_key}`（配置字段名） |

### 3.4 三处细节核查

- **唯一的删除调用**：`build_plugin.py:350 shutil.rmtree(tmp_parent, ignore_errors=True)` —— 打包脚本清理自己的暂存目录，非运行时路径。
- **build_plugin.py 是什么**：一个打包工具，docstring 里自己解释了为什么写（曾经发过结构错误的 zip，解压会污染 `data/plugins/` 根目录）。非运行时组件。
- **动态导入**：`utils/version.py:74 __import__(module_name, fromlist=["VERSION"])` —— 读取自身版本号。

---

## 四、意外发现：上游默认开启遥测

这是本次审计最有价值的发现，**且方向和预期相反**。

上游 `core/telemetry_manager.py`：

```python
_ENDPOINT = "https://plugincenter.aloys23.link/api/ingest"
_ENCODED_APP_KEY = "dGtfTUlMdzk5NTZ4aGNZTm54RzBzSHZzdFl3VjhnN0YtUmc="
_APP_KEY = base64.b64decode(_ENCODED_APP_KEY).decode()
```

类文档原文：**「遥测管理器，负责匿名上报插件运行状态、配置快照与错误信息」**。

上报事件包括：启动、停止、**心跳**、功能使用、**配置快照**（`track_config`）、错误。
并且**持久化一个跨重启的实例 ID**，用于聚合同一实例的遥测数据。

配置默认值：

| 配置项 | 默认 | 官方说明 |
|---|---|---|
| `telemetry_config.enabled` | **true** | 「启用后，插件会匿名上报配置统计与错误信息」 |
| `notification_settings.enabled` | **true** | 从同一服务拉取通知 |

另外上游保留了 `core/web_admin_server.py`：独立 WebUI，监听 `127.0.0.1:4100`，**`password` 默认空字符串**。

**灵犀的处理**：`core/telemetry_manager.py` 被重写成 57 行空实现，文件头直接写明：

> 本插件是从 astrbot_plugin_proactive_chat 深度改造而来的社区分支，不再向任何第三方服务上报运行状态、配置快照或错误信息。

所有 `track_*` 方法变成 no-op，`enabled` 恒为 False，但保留原接口以兼容调用方。

---

## 五、结论与建议

### 5.1 这个 fork 干净吗？

**静态层面：干净。** 没有外传、没有执行任意代码、没有碰你的密钥和数据库。

**但「干净」不等于「可以放心跑」：**

1. 它是 **dev 版本**，2 周没更新，而上游同期还在修 bug
2. 行为风险没变：核心功能是**主动发消息**，QQ 风控最敏感的行为
3. 静态扫描证明不了 10,500 行的**行为正确性**

### 5.2 那上游呢？

**功能与维护上游更强，但默认会把配置快照传到第三方服务器。** 要用的话建议先关掉：

```yaml
telemetry_config:
  enabled: false
notification_settings:
  enabled: false
web_admin:
  enabled: false      # 或至少设个密码
```

### 5.3 修正上一轮的判断

我上一轮说「405 star 所以更可信」——**在隐私维度上这是错的**，diff 给出了反证。修正后：

| 维度 | 胜出 |
|---|---|
| 隐私 / 外传 | **灵犀** |
| 维护活跃度 | 上游 |
| 文档与生态 | 上游 |
| 功能覆盖面（群聊接话等） | 灵犀 |
| 代码成熟度 | 上游（正式版 vs dev） |

**实操建议：**

- 要「主动消息」→ 用上游，但**先关遥测和通知**，WebUI 设密码或关掉
- 要「群聊接话 + 历史带 QQ」→ 只有灵犀有；可考虑**只搬那几个模块**，或接受它的 dev 风险
- 无论装哪个，主动消息都先拿不重要的号 / 小群试

---

## 六、审计局限性（须知）

1. **静态扫描 ≠ 安全证明。** 我查的是网络端点、危险调用、路径字面量；没有逐行读完 10,500 行，也没做运行时沙箱验证。
2. **上游只审了安全相关面**，未做功能正确性审计。
3. **依赖未审计**：`requirements.txt` 里的 `apscheduler` / `aiofiles` / `fastapi` / `uvicorn` 等第三方包未做供应链检查。
4. **动态行为未验证**：运行时是否发起扫描中未出现的网络请求（例如经由第三方库），静态扫描无法排除。
5. 结论仅针对 **2026-10-07** 时点的两个 commit。
