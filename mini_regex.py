#!/usr/bin/env python3
"""mini_regex.py —— 迷你正则匹配器（仅使用 Python 标准库）

支持的语法：
    普通字符          逐字匹配
    .                 匹配任意一个字符
    X*  X+            量词：X 零次或多次 / 一次或多次（X 可以是字符、点号、字符类或分组）
    [abc]  [a-z]      字符类，支持范围
    [^abc] [^a-z]     取反字符类
    ( ... )           分组，量词可叠加在分组上，如 (ab)+
    \\ . \\ * \\ + \\ [ \\ ] \\ ( \\ ) \\\\   反斜杠转义特殊字符

匹配策略：贪婪 + 回溯（与 Python re / PCRE 的默认语义一致），
取“最左最长”匹配。理由见文件末尾说明。

用法：
    python3 mini_regex.py PATTERN TEXT     # 匹配并输出所有匹配位置
    python3 mini_regex.py --demo           # 运行内置样例（含错误定位样例）
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------

class PatternError(Exception):
    """模式语法错误，pos 为出错字符的下标（0 起）。"""

    def __init__(self, message: str, pos: int):
        self.message = message
        self.pos = pos
        super().__init__(f"位置 {pos + 1}: {message}")


# ---------------------------------------------------------------------------
# AST 节点
# ---------------------------------------------------------------------------

@dataclass
class Seq:
    items: list = field(default_factory=list)


@dataclass
class Lit:
    ch: str


@dataclass
class Dot:
    pass


@dataclass
class CharClass:
    negated: bool
    chars: frozenset


@dataclass
class Group:
    child: object


@dataclass
class Repeat:
    child: object
    min_count: int          # 0 表示 *，1 表示 +（上界均为无穷）


# ---------------------------------------------------------------------------
# 解析器：递归下降，把模式文本编译成 AST
# ---------------------------------------------------------------------------

class Parser:
    # 允许被反斜杠转义的字符（转义后一律按字面值处理）
    ESCAPABLE = set(".*+[]()\\")

    def __init__(self, pattern: str):
        self.s = pattern
        self.i = 0

    def parse(self):
        node = self._parse_seq(top_level=True)
        return node

    # -- 序列 ----------------------------------------------------------------
    def _parse_seq(self, top_level: bool):
        items = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == ")":
                if top_level:
                    raise PatternError("多余的 ')'，没有与之配对的 '('", self.i)
                break  # 交给 _parse_atom 中的分组逻辑收尾
            if c in "*+":
                raise PatternError(f"量词 '{c}' 前面没有可重复的单元", self.i)
            atom = self._parse_atom()
            atom = self._parse_quantifier(atom)
            items.append(atom)
        return Seq(items)

    def _parse_quantifier(self, atom):
        if self.i < len(self.s) and self.s[self.i] in "*+":
            q = self.s[self.i]
            atom = Repeat(atom, min_count=1 if q == "+" else 0)
            self.i += 1
            if self.i < len(self.s) and self.s[self.i] in "*+":
                raise PatternError("量词不能连续叠加（如 '**'）", self.i)
        return atom

    # -- 原子单元 --------------------------------------------------------------
    def _parse_atom(self):
        c = self.s[self.i]
        if c == "(":
            open_pos = self.i
            self.i += 1
            child = self._parse_seq(top_level=False)
            if self.i >= len(self.s):
                raise PatternError("分组未闭合，缺少 ')'", open_pos)
            self.i += 1  # 跳过 ')'
            return Group(child)
        if c == "[":
            return self._parse_class()
        if c == ".":
            self.i += 1
            return Dot()
        if c == "\\":
            return self._parse_escape()
        self.i += 1
        return Lit(c)

    def _parse_escape(self):
        pos = self.i
        self.i += 1
        if self.i >= len(self.s):
            raise PatternError("反斜杠位于末尾，缺少要转义的字符", pos)
        c = self.s[self.i]
        if c not in self.ESCAPABLE:
            raise PatternError(f"非法转义 '\\{c}'", pos)
        self.i += 1
        return Lit(c)

    # -- 字符类 ----------------------------------------------------------------
    def _parse_class(self):
        start = self.i
        self.i += 1  # 跳过 '['
        negated = False
        if self.i < len(self.s) and self.s[self.i] == "^":
            negated = True
            self.i += 1

        chars = set()
        first = True  # POSIX 惯例：紧跟 '[' 或 '[^' 的 ']' 是字面值
        while True:
            if self.i >= len(self.s):
                raise PatternError("字符类未闭合，缺少 ']'", start)
            if self.s[self.i] == "]" and not first:
                self.i += 1
                break
            first = False
            lo = self._class_char(start)
            # 范围 a-z：'-' 后面必须还有非 ']' 的字符才算范围
            if (self.i < len(self.s) and self.s[self.i] == "-"
                    and self.i + 1 < len(self.s) and self.s[self.i + 1] != "]"):
                dash_pos = self.i
                self.i += 1
                hi = self._class_char(start)
                if ord(lo) > ord(hi):
                    raise PatternError(
                        f"字符范围 '{lo}-{hi}' 的起点大于终点", dash_pos)
                chars.update(chr(x) for x in range(ord(lo), ord(hi) + 1))
            else:
                chars.add(lo)
        return CharClass(negated, frozenset(chars))

    def _class_char(self, class_start: int) -> str:
        """读取字符类中的一个字符（允许 \\] \\\\ \\- \\[ 转义）。"""
        pos = self.i
        c = self.s[self.i]
        if c == "\\":
            self.i += 1
            if self.i >= len(self.s):
                raise PatternError("字符类未闭合，缺少 ']'", class_start)
            c = self.s[self.i]
            if c not in "]\\-[^":
                raise PatternError(f"字符类中的非法转义 '\\{c}'", pos)
        self.i += 1
        return c


# ---------------------------------------------------------------------------
# 匹配器：生成器式回溯，按贪婪顺序产生所有可能的结束位置
# ---------------------------------------------------------------------------

def gen_match(node, text: str, pos: int):
    """产生 node 在 text[pos:] 上所有可能的匹配结束位置（贪婪优先）。"""
    if isinstance(node, Seq):
        yield from _match_seq(node.items, 0, text, pos)
    elif isinstance(node, Lit):
        if pos < len(text) and text[pos] == node.ch:
            yield pos + 1
    elif isinstance(node, Dot):
        if pos < len(text):
            yield pos + 1
    elif isinstance(node, CharClass):
        if pos < len(text) and ((text[pos] in node.chars) != node.negated):
            yield pos + 1
    elif isinstance(node, Group):
        yield from gen_match(node.child, text, pos)
    elif isinstance(node, Repeat):
        yield from _match_repeat(node, text, pos, 0)


def _match_seq(items, idx, text, pos):
    if idx == len(items):
        yield pos
        return
    for end in gen_match(items[idx], text, pos):
        yield from _match_seq(items, idx + 1, text, end)


def _match_repeat(rep, text, pos, count):
    # 贪婪：先尝试“再匹配一次”，失败后再考虑“到此为止”
    for end in gen_match(rep.child, text, pos):
        if end == pos:
            continue  # 子单元匹配了空串，继续会死循环，跳过
        yield from _match_repeat(rep, text, end, count + 1)
    if count >= rep.min_count:
        yield pos


def find_all(ast, text: str):
    """在 text 中查找所有不重叠匹配，返回 [(start, end), ...]（0 起，左闭右开）。"""
    results = []
    pos = 0
    while pos <= len(text):
        end = next(gen_match(ast, text, pos), None)
        if end is None:
            pos += 1
            continue
        results.append((pos, end))
        pos = end if end > pos else pos + 1  # 空匹配时前进一格，避免死循环
    return results


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

def show_error(pattern: str, err: PatternError) -> str:
    caret = " " * err.pos + "^"
    return f"模式错误：{err}\n  {pattern}\n  {caret}"


def run(pattern: str, text: str) -> str:
    lines = [f"模式: {pattern!r}", f"文本: {text!r}"]
    try:
        ast = Parser(pattern).parse()
    except PatternError as err:
        lines.append(show_error(pattern, err))
        return "\n".join(lines)
    matches = find_all(ast, text)
    if not matches:
        lines.append("结果: 不匹配")
    else:
        lines.append(f"结果: 共 {len(matches)} 处匹配")
        for start, end in matches:
            lines.append(f"  [{start}, {end}) 匹配子串: {text[start:end]!r}")
    return "\n".join(lines)


DEMO_CASES = [
    # (说明, 模式, 文本)
    ("星号：零次或多次", "ab*c", "xabbbc y ac"),
    ("加号：一次或多次", "ab+c", "xabbbc y ac"),
    ("点号：任意字符", "a.c", "aXc a-c"),
    ("分组 + 量词", "(ab)+c", "zababc!"),
    ("字符类与范围", "[a-z]+@[a-z]+", "mail: bob@xyz!"),
    ("取反字符类", "[^0-9]+", "123abc45def"),
    ("转义：匹配字面点号", "a\\.c", "a.c aXc"),
    ("转义：匹配字面星号和反斜杠", "a\\*b\\\\", "a*b\\ aXb"),
    ("嵌套分组", "(a(bc)+)+d", "abcbcd"),
    ("空匹配（a* 匹配零个 a）", "a*", "bbb"),
]

ERROR_CASES = [
    ("量词前没有单元", "*abc", ""),
    ("量词连续叠加", "a**", ""),
    ("字符类未闭合", "ab[cd", ""),
    ("分组未闭合", "a(b[c]", ""),
    ("非法转义", "a\\qb", ""),
    ("反斜杠在末尾", "ab\\", ""),
    ("多余的右括号", "ab)c", ""),
    ("范围起点大于终点", "[z-a]", ""),
]


def demo() -> None:
    print("=" * 60)
    print("匹配样例")
    print("=" * 60)
    for title, pattern, text in DEMO_CASES:
        print(f"\n--- {title} ---")
        print(run(pattern, text))
    print()
    print("=" * 60)
    print("错误定位样例")
    print("=" * 60)
    for title, pattern, text in ERROR_CASES:
        print(f"\n--- {title} ---")
        print(run(pattern, text))


def main(argv) -> int:
    if len(argv) == 2 and argv[1] == "--demo":
        demo()
        return 0
    if len(argv) != 3:
        print(__doc__)
        return 2
    print(run(argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
