#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源文规范化：编码探测 → 统一 UTF-8/LF → 绑定 R#### 行号 → 按字数切段。

默认按字数切段：累计满 2000 字就在当前换行处收尾，一段一个语义单元。
不依赖文档内部的「第N集/章」标记（网文、论坛整理稿的集号经常缺失或错乱）。

    python3 normalize.py --src 小说.txt --workdir work                 # 2000 字/段
    python3 normalize.py --src 小说.txt --workdir work --chunk-chars 1500
    python3 normalize.py --src 小说.txt --workdir work --single        # 整本一段
    python3 normalize.py --src 小说.txt --workdir work --episode-re "第[...]+章"  # 按标题切

产物：
    work/source/normalized.txt   R#### 前缀的全文（UTF-8/LF）
    work/segments/segNN.txt      每段原文（带 R 行号），带 `# E01 标题  源行 R0001–R0287` 头
    work/结构清单.md              编码、行数、各段范围与字数
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ENCODINGS = ["utf-8-sig", "utf-8", "utf-16", "gb18030", "big5", "cp950", "latin-1"]

NOISE = [
    re.compile(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(\s+\d{1,2}:\d{2}(:\d{2})?)?"),
    re.compile(r"^\s*-{10,}\s*$"),
    re.compile(r"^\s*\d+\s*(回复|樓|楼)[：:]"),
    re.compile(r"^\s*(powered by|本帖最后由|编辑于|转载请注明)", re.I),
    re.compile(r"^\s*(https?://|www\.)", re.I),
    re.compile(r"^\s*\[?(原创|转帖|转载|公告)\]?\s*$"),
]

CJK = re.compile(r"[\u4e00-\u9fff]")
# 集号数字集：阿拉伯 + 常用汉字 + 大写/财务写法（第叁集、第肆回）+ 廿卅
NUM = "0-9０-９一二三四五六七八九十百千零〇两叁肆伍陆柒捌玖拾廿卅"
DEFAULT_EP = re.compile(r"第\s*[%s]{1,4}\s*[集章回卷節节]" % NUM)


def read_text(path: str):
    """按候选编码逐个试读，用中文占比打分，返回 (文本, 编码)。"""
    raw = Path(path).read_bytes()
    best = None
    for enc in ENCODINGS:
        try:
            t = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if not t:
            continue
        sample = t[:20000]
        score = len(CJK.findall(sample)) / max(1, len(sample))
        # 明显乱码（替换符多）扣分
        score -= sample.count("\ufffd") * 0.5
        if best is None or score > best[0]:
            best = (score, t, enc)
    if best is None:
        raise SystemExit("无法解码源文件：%s" % path)
    return best[1], best[2]


def clean_lines(text: str):
    """归一换行，剥掉零宽字符；返回原始行列表（索引即源行号-1）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return text.split("\n")


def is_noise(line: str):
    s = line.strip()
    if not s:
        return True
    return any(p.match(s) for p in NOISE)


def find_episodes(lines, ep_re):
    """找出标题行：短行 + 匹配集号模式；返回 [(行号, 标题)]。"""
    pat = re.compile(ep_re) if ep_re else DEFAULT_EP
    out = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or len(s) > 60:
            continue
        if is_noise(line):
            continue
        # 正文里的「第三集」不可能是标题：排除以引号/对白开头或含句末标点的长句
        if s[0] in "『「“（(":
            continue
        if len(pat.findall(s)) == 1 and pat.search(s):
            if out and i - out[-1][0] < 2:
                continue
            out.append((i, s))
    return out


def line_weight(line: str) -> int:
    """按汉字/字符计字，空白不计。"""
    return len(re.sub(r"\s", "", line))


def chunk_by_chars(lines, target):
    """累计满 target 字就在当前行末收尾——分段边界永远落在换行处。

    返回 [(起行, 止行)]，行号为源文件 1 基行号。噪音行不计字数，但不会切断段落。
    """
    chunks, start, cnt = [], None, 0
    for i, line in enumerate(lines, 1):
        if is_noise(line):
            continue
        if start is None:
            start = i
        cnt += line_weight(line)
        if cnt >= target:
            chunks.append((start, i))
            start, cnt = None, 0
    if start is not None:
        chunks.append((start, len(lines)))
    return [(a, b) for a, b in chunks
            if any(not is_noise(x) for x in lines[a - 1:b])]


def chunk_title(lines, a, b, idx):
    """段内若出现「第N集/章」短标题就用它，否则用「第NN段」。"""
    for j in range(a, b + 1):
        s = lines[j - 1].strip()
        if s and len(s) <= 40 and DEFAULT_EP.search(s):
            return s
    return "第%02d段" % idx


def _title_blocks(lines, ep_re):
    """按标题行切分，返回 [(起行, 止行, 标题)]。"""
    titles = find_episodes(lines, ep_re)
    if not titles:
        return []
    bounds = [t[0] for t in titles] + [len(lines) + 1]
    return [(t[0], bounds[i + 1] - 1, t[1]) for i, t in enumerate(titles)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--chunk-chars", type=int, default=2000,
                    help="每段目标字数，满额后在当前换行处收尾；0 表示按标题切")
    ap.add_argument("--episode-re", default="", help="改按标题切分时使用的正则")
    ap.add_argument("--single", action="store_true", help="整本作为一段")
    ap.add_argument("--encoding", default="", help="强制指定编码")
    args = ap.parse_args()

    wd = Path(args.workdir)
    (wd / "source").mkdir(parents=True, exist_ok=True)
    (wd / "segments").mkdir(parents=True, exist_ok=True)

    if args.encoding:
        text = Path(args.src).read_bytes().decode(args.encoding)
        enc = args.encoding
    else:
        text, enc = read_text(args.src)
    lines = clean_lines(text)
    n_total = len(lines)

    norm = wd / "source" / "normalized.txt"
    with norm.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# 规范化源文件（%s → UTF-8/LF），R#### 为原文件行号\n" % enc)
        for n, line in enumerate(lines, 1):
            f.write("R%04d\t%s\n" % (n, line.rstrip()))

    stem = Path(args.src).stem
    if args.single:
        blocks = [(1, n_total, stem)]
        mode = "整本一段"
    elif args.episode_re:
        blocks = _title_blocks(lines, args.episode_re)
        mode = "按标题正则切分"
    elif args.chunk_chars and args.chunk_chars > 0:
        blocks = [(a, b, chunk_title(lines, a, b, i + 1))
                  for i, (a, b) in enumerate(chunk_by_chars(lines, args.chunk_chars))]
        mode = "按字数 %d 字/段（满额后在换行处收尾）" % args.chunk_chars
    else:
        blocks = _title_blocks(lines, "")
        mode = "按标题切分"
    if not blocks:
        blocks = [(1, n_total, stem)]
        mode = "整本一段（未切出有效分段）"

    rows = []
    for i, (start, end, title) in enumerate(blocks):
        eid = "E%02d" % (i + 1)
        body = [j for j in range(start, end + 1) if not is_noise(lines[j - 1])]
        chars = sum(line_weight(lines[j - 1]) for j in body)
        seg = wd / "segments" / ("seg%02d.txt" % (i + 1))
        with seg.open("w", encoding="utf-8", newline="\n") as f:
            f.write("# %s %s  源行 R%04d–R%04d\n" % (eid, title, start, end))
            for j in range(start, end + 1):
                f.write("R%04d\t%s\n" % (j, lines[j - 1].rstrip()))
        rows.append({"ep": eid, "title": title, "r_lo": start, "r_hi": end,
                     "lines": len(body), "chars": chars, "seg": str(seg.relative_to(wd))})

    md = ["# 结构清单", "",
          "- 源文件：`%s`" % args.src,
          "- 探测编码：**%s**（候选 %s）" % (enc, "/".join(ENCODINGS)),
          "- 总行数：%d（splitlines 归一，空行计入源行号）" % n_total,
          "- 分段方式：**%s**" % mode,
          "- 分段数：%d｜总字数：%d" % (len(rows), sum(r["chars"] for r in rows)), "",
          "| 段 | 标题 | 源行范围 | 正文字数 | 正文行数 | 片段文件 |",
          "|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| %s | %s | R%04d–R%04d | %d | %d | `%s` |"
                  % (r["ep"], r["title"], r["r_lo"], r["r_hi"], r["chars"],
                     r["lines"], r["seg"]))
    if mode.startswith("按标题") and len(rows) == 1:
        md += ["", "> 未探测到集标题，整本作为单段处理。可用 `--episode-re` 指定模式，"
               "或改用默认的按字数切段。"]
    (wd / "结构清单.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("编码 %s｜%d 行｜%s｜%d 段" % (enc, n_total, mode, len(rows)))
    for r in rows:
        print("  %s %s  R%04d–R%04d  %d 字"
              % (r["ep"], r["title"], r["r_lo"], r["r_hi"], r["chars"]))


if __name__ == "__main__":
    main()
