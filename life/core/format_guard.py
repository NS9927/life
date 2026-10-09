"""去格式化：把模型输出里的 Markdown / 编号清单拍平成「群里说人话」的样子。

真机症状（2026-10-08）：

1. 发到 QQ 的内容里有 ``**群聊版：**`` —— QQ 不渲染 Markdown，星号原样显示；
2. 编号清单（``1 控制变量… 2 用 debug tooltip… 3 …``）被分段机制切成 **7~8 条消息、
   跨 34 秒**，观感像「工作助手在群里交作业」。

所以本模块做两件事：**删掉标记**、**把列表/表格拍平**（拍平后换行自然变少 →
``segmented_jitter`` 切出来的段数从 8 掉到 2~3，这正是目的）。

## 原则：只删标记，绝不丢内容

- ``**粗**`` → ``粗``；``*斜*`` / ``_斜_`` → ``斜``；``~~删~~`` → ``删``；
  ``` `代码` ``` → ``代码``；``# 标题`` → ``标题``；``> 引用`` → ``引用``
- ```` ```代码块``` ```` → **内部文本原样保留**，只去掉围栏行；代码内容不会被
  列表拍平或粗体规则二次加工（靠占位符隔离）
- 列表项：``1. 甲`` / ``1、甲`` / ``1) 甲`` / ``① 甲`` / ``- 甲`` / ``* 甲`` / ``• 甲``
  / ``· 甲`` → 取「甲」，多项之间用 ``list_joiner`` 连接；
  **前一项本来以标点结尾就直接拼接**（``先做A。`` + ``再做B。`` → ``先做A。再做B。``），
  不会出现「丢标点」的 ``A，B。``
- 表格 ``| a | b |`` → 每行拍平成 ``a，b``；``|---|`` 这种分隔行没有内容，直接丢掉
- **不动**：URL、颜文字、空行、以及本来没有标记的文本（逐字相同，测试固化）
- ``max_chars > 0``（默认 0 = 关）才截断，且**截到句末**；这是唯一会丢内容的开关

纯逻辑：不 import astrbot、不做 I/O，``clean(text, config) -> (text, stats)`` 可直接单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from . import coerce

SENTENCE_ENDS = "。！？!?…；;"
"""句末标点：列表拼接时「前一项以它结尾就直接接下一项」。"""

ANY_PUNCT = "。！？!?…；;，,、：:"
"""任意标点：前一项以标点结尾就不再补 ``list_joiner``（避免「、，」这种叠标点）。"""

_PLACEHOLDER = "\x00FMT{}\x00"
"""代码内容的占位符。用 NUL 包起来：模型输出里不可能出现，恢复时按序号替换。"""

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*\S*\s*$")
_INLINE_CODE_RE = re.compile(r"(`{1,2})([^`\n]+?)\1")
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_ORDERED_LIST_RE = re.compile(
    rf"^\s*(?:\d{{1,3}}\s*[、）)]\s*|\d{{1,3}}\s*[.](?:\s+|$)|[{_CIRCLED}]\s*)"
)
"""行首编号列表。``.`` 后面必须有空白或行尾——否则 ``3.5 折`` 里的 ``3.`` 会被当标记，
把「3.」当内容删掉。``、``/``）``/``)`` 是中文/全角写法，允许紧跟内容（``1、甲``）。"""

_UNORDERED_LIST_RE = re.compile(r"^\s*(?:[-*]\s+|[•·]\s*)")
"""``-`` / ``*`` 后面必须有空白，否则 ``-5 度`` 的负号会被当标记。``•``/``·`` 无歧义，允许紧跟。"""

_BARE_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s+(?=\S)")
"""真机那种**没有点号**的编号行：``1 控制变量``。

单独一行不敢当列表（``3 人一起`` 里的 3 是内容），只在「连续 ≥2 行、从 1 开始递增」
时才认（见 ``_bare_list_lines``）。"""
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
# 行内列表标记（整行写成一句话的情况，如 "1. 先做A。2. 再做B。"）：
# 标记前面必须是标点或空格——**不能是换行**，行首标记交给整行规则处理。
_INLINE_ORDERED_RE = re.compile(
    rf"(?P<prev>[。！？!?…；;，,、]| )(?:\d{{1,3}}\s*[.、)）]|[{_CIRCLED}])\s+"
)
# 行内 "- " 只在句末标点之后才算列表（避免把 "A - B" 这种破折号当列表）
_INLINE_BULLET_RE = re.compile(r"(?P<prev>[。！？!?…；;]) *[-*•·]\s+")


@dataclass(frozen=True)
class FormatGuardConfig:
    """``format_guard`` 配置组。★默认关闭。"""

    enable: bool = False
    strip_markdown: bool = True
    """粗体/斜体/行内代码/代码块/标题/引用/删除线/分隔线。"""
    flatten_lists: bool = True
    """编号与无序列表、表格拍平。"""
    list_joiner: str = "，"
    """列表项之间的连接符（默认逗号，更像说话）。"""
    max_chars: int = 0
    """0 = 不限；>0 时超长截断到句末（可选，默认不启用）。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "FormatGuardConfig":
        data = coerce.as_mapping(raw)
        joiner = data.get("list_joiner")
        if not isinstance(joiner, str):
            joiner = "，"
        return cls(
            enable=coerce.as_bool(data.get("enable"), False),
            strip_markdown=coerce.as_bool(data.get("strip_markdown"), True),
            flatten_lists=coerce.as_bool(data.get("flatten_lists"), True),
            list_joiner=joiner,
            max_chars=max(0, coerce.as_int(data.get("max_chars"), 0)),
        )


@dataclass
class FormatStats:
    """处理统计（只用于日志，参与不了判定）。"""

    bold: int = 0
    italic: int = 0
    strike: int = 0
    inline_code: int = 0
    code_block: int = 0
    heading: int = 0
    quote: int = 0
    hr: int = 0
    list_items: int = 0
    table_rows: int = 0
    truncated: bool = False
    chars_before: int = 0
    chars_after: int = 0

    @property
    def removed_anything(self) -> bool:
        return any(
            (
                self.bold,
                self.italic,
                self.strike,
                self.inline_code,
                self.code_block,
                self.heading,
                self.quote,
                self.hr,
                self.list_items,
                self.table_rows,
                self.truncated,
            )
        )

    def log_line(self) -> str:
        parts: list[str] = []
        for count, label in (
            (self.bold, "处粗体"),
            (self.italic, "处斜体"),
            (self.strike, "处删除线"),
            (self.inline_code, "处行内代码"),
            (self.code_block, "个代码块"),
            (self.heading, "个标题"),
            (self.quote, "处引用"),
            (self.hr, "条分隔线"),
            (self.list_items, "个列表标记"),
            (self.table_rows, "行表格"),
        ):
            if count:
                parts.append(f"{count} {label}")
        head = "去掉 " + "、".join(parts) if parts else "无需处理"
        tail = f"，文本 {self.chars_before}→{self.chars_after} 字"
        if self.truncated:
            tail += "（已按 max_chars 截断到句末）"
        return head + tail


def clean(text: str, config: FormatGuardConfig) -> tuple[str, FormatStats]:
    """拍平 Markdown。返回 ``(处理后的文本, 统计)``。

    ★只删标记：除 ``max_chars`` 截断外，任何字符都不会凭空消失（空行、URL、
    颜文字、代码块内容都原样保留）。实现内部用占位符隔离代码内容，
    保证它不被后续的拍平规则二次加工。
    """
    original = "" if text is None else str(text)
    stats = FormatStats(chars_before=len(original), chars_after=len(original))
    if not original.strip():
        return original, stats

    out = original
    stash: list[str] = []

    if config.strip_markdown:
        out = _stash_code(out, stash, stats)
        out = _strip_inline(out, stats)
        out = _strip_line_markers(out, stats)

    if config.flatten_lists:
        out = _flatten_blocks(out, config.list_joiner, stats)

    for index, content in enumerate(stash):
        out = out.replace(_PLACEHOLDER.format(index), content)

    if config.max_chars > 0:
        out = _truncate(out, config.max_chars, stats)

    stats.chars_after = len(out)
    return out, stats


# ----------------------------------------------------------------------
# 代码：先摘出来（内容原样保留），最后再放回去
# ----------------------------------------------------------------------
def _stash_code(text: str, stash: list[str], stats: FormatStats) -> str:
    """把代码块主体与行内代码内容换成占位符。

    代码块：整块（含围栏行）换成一个占位符，内部文本原样保留；
    未闭合的围栏按「到结尾都是代码」处理（宁可不加工，也不乱改）。
    """
    lines = text.split("\n")
    out_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line):
            fence_char = line.strip()[0]
            body: list[str] = []
            index += 1
            while index < len(lines):
                if _FENCE_RE.match(lines[index]) and lines[index].strip().startswith(
                    fence_char * 3
                ):
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            stats.code_block += 1
            content = "\n".join(body)
            out_lines.append(_store(stash, content))
            continue
        out_lines.append(line)
        index += 1

    text = "\n".join(out_lines)

    def repl(match: re.Match) -> str:
        stats.inline_code += 1
        return _store(stash, match.group(2))

    return _INLINE_CODE_RE.sub(repl, text)


def _store(stash: list[str], content: str) -> str:
    stash.append(content)
    return _PLACEHOLDER.format(len(stash) - 1)


# ----------------------------------------------------------------------
# 行内标记
# ----------------------------------------------------------------------
def _strip_inline(text: str, stats: FormatStats) -> str:
    text, count = _BOLD_STAR_RE.subn(r"\1", text)
    stats.bold += count
    text, count = _BOLD_UNDER_RE.subn(r"\1", text)
    stats.bold += count
    text, count = _STRIKE_RE.subn(r"\1", text)
    stats.strike += count
    # 粗体先处理掉，剩下的单个 * / _ 才可能是斜体
    text, count = _ITALIC_STAR_RE.subn(r"\1", text)
    stats.italic += count
    text, count = _ITALIC_UNDER_RE.subn(r"\1", text)
    stats.italic += count
    return text


def _strip_line_markers(text: str, stats: FormatStats) -> str:
    """行首标记：``# 标题`` / ``> 引用`` / ``---`` 分隔线。"""
    out: list[str] = []
    for line in text.split("\n"):
        if _HR_RE.match(line):
            stats.hr += 1
            continue  # 整行只有标记，没有内容可留
        heading = _HEADING_RE.match(line)
        if heading:
            stats.heading += 1
            line = line[heading.end() :]
        while True:
            quote = _QUOTE_RE.match(line)
            if not quote:
                break
            stats.quote += 1
            line = line[quote.end() :]
        out.append(line)
    return "\n".join(out)


# ----------------------------------------------------------------------
# 列表 / 表格拍平
# ----------------------------------------------------------------------
def _list_item_text(line: str) -> str | None:
    """这一行是列表项吗？是的话返回去掉标记后的内容。"""
    for pattern in (_ORDERED_LIST_RE, _UNORDERED_LIST_RE):
        match = pattern.match(line)
        if match:
            return line[match.end() :].strip()
    return None


def _table_cells(line: str) -> list[str] | None:
    match = _TABLE_ROW_RE.match(line)
    if not match:
        return None
    return [cell.strip() for cell in match.group(1).split("|")]


def _join_items(items: list[str], joiner: str) -> str:
    """列表项连接：前一项以标点结尾就直接拼，否则补 ``joiner``。"""
    out = items[0]
    for item in items[1:]:
        if out and out[-1] in ANY_PUNCT:
            out += item
        else:
            out += joiner + item
    return out


def _flatten_inline_markers(line: str, joiner: str, stats: FormatStats) -> str:
    """把**同一行里**的列表标记也拍平。

    真机案例 ``1. 先做A。2. 再做B。`` 是一整行：行首那个由整行规则处理，
    后面那个 ``2.`` 必须在这里处理掉，否则会原样留在正文里。
    ``prev`` 是句末标点 → 直接去掉标记（``先做A。再做B。``，不丢标点）；
    否则补 ``joiner``（``甲 2. 乙`` → ``甲，乙``）。
    """

    def repl(match: re.Match) -> str:
        stats.list_items += 1
        prev = match.group("prev")
        # ★把 prev 原样还回去：它是标点（要保留标点）或一个空格（用 joiner 替换掉）
        return prev if prev in ANY_PUNCT else joiner

    return _INLINE_BULLET_RE.sub(repl, _INLINE_ORDERED_RE.sub(repl, line))


def _bare_list_lines(lines: list[str]) -> set[int]:
    """找出「像无点号编号清单」的行下标：连续 ≥2 行、从 1 开始、逐个 +1。

    单独一行 ``1 个方法`` / ``3 人一起`` 不算（那是内容）；只有成串的
    ``1 …`` ``2 …`` ``3 …`` 才当列表处理——这是真机 8 段案例的元凶之一。
    """
    found: set[int] = set()
    index = 0
    while index < len(lines):
        match = _BARE_NUMBER_RE.match(lines[index])
        if not match or int(match.group(1)) != 1:
            index += 1
            continue
        run = [index]
        expected = 2
        cursor = index + 1
        while cursor < len(lines):
            nxt = _BARE_NUMBER_RE.match(lines[cursor])
            if not nxt or int(nxt.group(1)) != expected:
                break
            run.append(cursor)
            expected += 1
            cursor += 1
        if len(run) >= 2:
            found.update(run)
            index = cursor
        else:
            index += 1
    return found


def _flatten_blocks(text: str, joiner: str, stats: FormatStats) -> str:
    """把连续的列表行合成一句；表格行拍平成 ``a，b``。"""
    lines = text.split("\n")
    bare = _bare_list_lines(lines)
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            out.append(_join_items(pending, joiner))
            pending.clear()

    for index, line in enumerate(lines):
        line = _flatten_inline_markers(line, joiner, stats)
        if index in bare:
            item = _BARE_NUMBER_RE.sub("", line, count=1).strip()
        else:
            item = _list_item_text(line)
        if item is not None:
            if item:  # 只有标记、没有内容的项没有内容可留
                pending.append(item)
                stats.list_items += 1
            continue

        cells = _table_cells(line)
        if cells is not None:
            flush()
            if cells and all(_TABLE_SEP_CELL_RE.match(cell) for cell in cells if cell):
                continue  # |---|---| 分隔行没有内容 → 丢掉
            rendered = joiner.join(cell for cell in cells if cell)
            out.append(rendered)
            stats.table_rows += 1
            continue

        stripped = line.strip()
        if pending and stripped:
            # 列表块里的续行（缩进换行）：并进当前项，内容不丢
            pending[-1] = pending[-1] + stripped
            continue
        flush()
        out.append(line)

    flush()
    return "\n".join(out)


# ----------------------------------------------------------------------
# 截断（默认关闭）
# ----------------------------------------------------------------------
def _truncate(text: str, limit: int, stats: FormatStats) -> str:
    """截到 ``limit`` 以内、并尽量落在句末（达不到句末就硬截）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max((head.rfind(char) for char in SENTENCE_ENDS), default=-1)
    stats.truncated = True
    return head[: cut + 1] if cut >= 0 else head
