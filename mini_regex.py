#!/usr/bin/env python3
"""mini_regex.py —— 迷你正则匹配引擎（纯 Python 标准库，单文件）。

支持的语法：
    普通字符            匹配自身
    .                   匹配任意单个字符
    X*                  前一个单元重复零次或多次
    X+                  前一个单元重复一次或多次
    [abc]  [a-z]  [^0-9] 字符类：枚举、范围、取反（^ 必须紧跟 [）
    ( ... )             分组，量词可叠加在分组上，如 (ab)+ 、(a[0-9])*
    \\ 后跟 . * + [ ] ( ) \\ 之一表示转义，其余为非法转义

匹配策略：左起第一个可匹配的起点 + 贪婪回溯（与 Python re 默认语义一致）。

用法：
    python3 mini_regex.py <模式> <文本>     匹配一次并输出位置
    python3 mini_regex.py --demo            运行内置样例（含错误定位样例）
"""

import sys


class PatternError(Exception):
    """模式语法错误。pos 为 1 起始的字符位置（第几个字符）。"""

    def __init__(self, pos, message):
        self.pos = pos
        self.message = message
        super().__init__(f"第 {pos} 个字符: {message}")


# 允许被反斜杠转义的字符
ESCAPABLE = set(".*+[]()\\")

# ----------------------------------------------------------------------
# 解析器：模式文本 -> AST
# AST 节点（元组）：
#   ('char', c)                 普通字符
#   ('any',)                    点号
#   ('class', negated, chars)   字符类，chars 为 frozenset
#   ('seq', [node, ...])        序列（分组解析后也是 seq）
#   ('repeat', child, min)      量词，min 为 0(*) 或 1(+)，上不封顶
# ----------------------------------------------------------------------


class Parser:
    def __init__(self, pattern):
        self.s = pattern
        self.i = 0  # 当前解析下标（0 起始）

    def parse(self):
        return self.parse_seq(top=True)

    def parse_seq(self, top=False):
        items = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == ")":
                if top:
                    raise PatternError(self.i + 1, "多余的 ')'：没有对应的 '('")
                break  # 交给 parse_atom 中处理 '(' 的代码消费
            atom = self.parse_atom()
            # 量词可叠加（如 a*+ 、(ab)+* ），逐个包一层 repeat
            while self.i < len(self.s) and self.s[self.i] in "*+":
                q = self.s[self.i]
                atom = ("repeat", atom, 0 if q == "*" else 1)
                self.i += 1
            items.append(atom)
        return ("seq", items)

    def parse_atom(self):
        pos = self.i  # 当前字符的 0 起始下标
        c = self.s[self.i]
        if c in "*+":
            raise PatternError(pos + 1, f"量词 '{c}' 前面没有可重复的单元")
        if c == "(":
            self.i += 1
            inner = self.parse_seq()
            if self.i >= len(self.s):  # 没遇到 ')' 就到末尾了
                raise PatternError(pos + 1, "'(' 分组未闭合：缺少对应的 ')'")
            self.i += 1  # 消费 ')'
            return inner
        if c == "[":
            return self.parse_class()
        if c == "\\":
            return self.parse_escape()
        if c == ".":
            self.i += 1
            return ("any",)
        self.i += 1
        return ("char", c)

    def parse_escape(self):
        bs = self.i  # 反斜杠的位置
        self.i += 1
        if self.i >= len(self.s):
            raise PatternError(bs + 1, "非法转义：'\\' 位于模式末尾，后面没有字符")
        e = self.s[self.i]
        if e not in ESCAPABLE:
            raise PatternError(
                self.i + 1,
                "非法转义 '\\%s'：只能转义 %s" % (e, " ".join(sorted(ESCAPABLE))),
            )
        self.i += 1
        return ("char", e)

    def parse_class(self):
        start = self.i  # '[' 的位置
        self.i += 1
        negated = False
        if self.i < len(self.s) and self.s[self.i] == "^":
            negated = True
            self.i += 1
        chars = set()
        while True:
            if self.i >= len(self.s):
                raise PatternError(start + 1, "'[' 字符类未闭合：缺少对应的 ']'")
            if self.s[self.i] == "]":
                self.i += 1
                return ("class", negated, frozenset(chars))
            lo = self.class_char(start)
            # 形如 a-z 的范围：'-' 后面还有字符且不是 ']'
            if (
                self.i < len(self.s)
                and self.s[self.i] == "-"
                and self.i + 1 < len(self.s)
                and self.s[self.i + 1] != "]"
            ):
                self.i += 1
                hi = self.class_char(start)
                if ord(lo) > ord(hi):
                    raise PatternError(
                        self.i, f"非法范围 '{lo}-{hi}'：起点大于终点"
                    )
                chars.update(chr(x) for x in range(ord(lo), ord(hi) + 1))
            else:
                chars.add(lo)

    def class_char(self, start):
        """读取字符类中的一个字符（处理转义），越界报未闭合。"""
        if self.i >= len(self.s):
            raise PatternError(start + 1, "'[' 字符类未闭合：缺少对应的 ']'")
        c = self.s[self.i]
        if c == "\\":
            self.i += 1
            if self.i >= len(self.s):
                raise PatternError(start + 1, "'[' 字符类未闭合：缺少对应的 ']'")
            e = self.s[self.i]
            if e not in ESCAPABLE and e != "-":
                raise PatternError(self.i + 1, f"非法转义 '\\{e}'")
            self.i += 1
            return e
        self.i += 1
        return c


# ----------------------------------------------------------------------
# 匹配器：贪婪回溯。每个 match_* 都是生成器，按“优先更长”的顺序
# 产生所有可能的结束位置；search 取左起第一个起点上的第一个结果。
# ----------------------------------------------------------------------


def match_node(node, text, pos):
    tag = node[0]
    if tag == "seq":
        yield from match_seq(node[1], 0, text, pos)
    elif tag == "char":
        if pos < len(text) and text[pos] == node[1]:
            yield pos + 1
    elif tag == "any":
        if pos < len(text):
            yield pos + 1
    elif tag == "class":
        if pos < len(text) and ((text[pos] in node[2]) != node[1]):
            yield pos + 1
    elif tag == "repeat":
        yield from match_repeat(node, text, pos, 0)


def match_seq(items, idx, text, pos):
    if idx == len(items):
        yield pos
        return
    for p in match_node(items[idx], text, pos):
        yield from match_seq(items, idx + 1, text, p)


def match_repeat(node, text, pos, count):
    _, child, min_count = node
    # 贪婪：先尝试“再多匹配一次”， deeper 的结束位置先被 yield
    for p in match_node(child, text, pos):
        if p != pos:  # 子单元匹配了空串，继续迭代会死循环，跳过
            yield from match_repeat(node, text, p, count + 1)
    # 回溯点：少匹配也行（次数已满足下限时）
    if count >= min_count:
        yield pos


def search(ast, text):
    """返回 (start, end)，左起第一个起点 + 该起点上贪婪的第一个结果。"""
    for start in range(len(text) + 1):
        for end in match_node(ast, text, start):
            return (start, end)
    return None


# ----------------------------------------------------------------------
# 命令行与演示
# ----------------------------------------------------------------------


def run(pattern, text):
    print(f"模式: {pattern!r}")
    print(f"文本: {text!r}")
    try:
        ast = Parser(pattern).parse()
    except PatternError as e:
        print(f"语法错误: 第 {e.pos} 个字符: {e.message}")
        print(f"  {pattern}")
        print(f"  {' ' * (e.pos - 1)}^")
        print()
        return
    m = search(ast, text)
    if m is None:
        print("结果: 不匹配")
    else:
        s, e = m
        print(f"结果: 匹配  区间 [{s}, {e})  内容 {text[s:e]!r}")
    print()


DEMO_CASES = [
    ("a.*c", "xxabcyyczz"),       # 贪婪：一路吃到最后一个 c
    ("(ab)+c", "zabababc!"),      # 量词叠加在分组上
    ("[a-z]+", "123abcDEF"),      # 范围字符类
    ("[^0-9]+", "42 is ok"),      # 取反字符类
    ("a\\.b", "xa.by"),           # 转义点号
    ("(a[0-9])\\+", "xa5+b"),     # 分组 + 字符类 + 转义加号
    ("\\[ok\\]", "say [ok]!"),    # 转义方括号
    ("x*y", "aaay"),              # 星号匹配零次
]

DEMO_ERRORS = [
    "*abc",     # 量词前没有可重复单元
    "a(b",      # 分组未闭合
    "[a-z",     # 字符类未闭合
    "a\\q",     # 非法转义
    "abc\\",    # 反斜杠在末尾
]


def demo():
    print("===== 匹配样例 =====")
    for pattern, text in DEMO_CASES:
        run(pattern, text)
    print("===== 语法错误定位样例 =====")
    for pattern in DEMO_ERRORS:
        run(pattern, "")


def main(argv):
    if len(argv) >= 3:
        run(argv[1], argv[2])
    else:
        demo()


if __name__ == "__main__":
    main(sys.argv)
