"""Баланс скобок JScript-файла с учётом строк, комментариев и regex-литералов.

cscript не годится: его парсер не принимает кириллические идентификаторы
(падает и на файле, проверенном вживую в Снегопате). Node на машине нет.
"""
import sys

src = open(sys.argv[1], encoding="utf-8-sig").read()
pairs = {"(": ")", "{": "}", "[": "]"}
stack = []
line = 1
i = 0
n = len(src)
prev_sig = ""


def error(msg):
    print(f"{msg} at line {line}")
    sys.exit(1)


while i < n:
    c = src[i]
    if c == "\n":
        line += 1
        i += 1
        continue
    if c in " \t\r":
        i += 1
        continue
    if c in "\"'":
        q = c
        i += 1
        while i < n:
            if src[i] == "\\":
                i += 2
                continue
            if src[i] == q:
                break
            if src[i] == "\n":
                error("незакрытая строка")
            i += 1
        i += 1
        prev_sig = '"str"'
        continue
    if c == "/" and i + 1 < n:
        if src[i + 1] == "/":
            j = src.find("\n", i)
            i = j if j > 0 else n
            continue
        if src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                error("незакрытый блок-комментарий")
            line += src.count("\n", i, j)
            i = j + 2
            continue
        if prev_sig in ("", ":", ",", "=", "(", "[", "!", "&", "|", "?", "{", "}", ";", "return", "&&", "||", "=>"):
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    error("regex через перевод строки (вероятно, не regex)")
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i = j
            prev_sig = "regex"
            continue
    if c in pairs:
        stack.append((c, line))
        i += 1
        prev_sig = c
        continue
    if c in ")}]":
        if not stack or pairs[stack[-1][0]] != c:
            error(f"закрытие {c!r} без пары (стек: {stack[-3:]})")
        stack.pop()
        i += 1
        prev_sig = c
        continue
    j = i
    while j < n and (src[j].isalnum() or src[j] in "_.$"):
        j += 1
    prev_sig = src[i:j] if j > i else c
    i = j if j > i else i + 1

if stack:
    print(f"незакрыто: {stack[-5:]}")
    sys.exit(1)
print("BALANCED OK")
