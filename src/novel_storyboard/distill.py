#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蒸馏层：把一段小说原文的「剧本素材 md」解析、校验、投影、汇总。

模型只负责读文写字；这一层负责：
  - validate   编号/字段/行号/禁词/台词连续性，不合规原样回喂
  - project    蒸馏稿 → 语义 md（下游 plan 的唯一输入，确定性转换）
  - aggregate  全片角色总表 / 剧情线总表 / characters.json / plot.json
  - hint       已出现人物清单，喂给下一段的提示词，减少同一人多种叫法

蒸馏稿结构：
    # E01 段名
    - 源行范围：R0001–R0101
    - 本段主线：……
    ## 角色        （表格：角色｜原文称呼｜身份｜首次｜标志物）
    ## 剧情线      （起／承／转／合／冲突）
    ## 场景卡      （### E01-S01 地点 · 时段，字段同语义 md，可拍画面代替叙述素材）

用法：
    python3 distill.py validate --file distill/E01_*.md --ep E01 --lo 1 --hi 101
    python3 distill.py project  --distill work/distill --out work/semantic
    python3 distill.py aggregate --distill work/distill
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import contract as C  # noqa: E402

HEAD_RE = re.compile(r"^#\s+(E\d{2})\s*(.*)$", re.M)
SEC_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
BLOCK_RE = re.compile(r"^###\s+(E\d{2}-S\d{2,3})\s*(.*)$", re.M)
LINE_RE = re.compile(r"^\s*-\s+(E\d{2}-L\d{3})\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*$", re.M)
SHOT_RE = re.compile(r"^\s*-\s+R(\d{4})\s+(.+?)\s*$", re.M)
RANGE_RE = re.compile(r"^-\s*源行范围[：:]\s*R(\d{4})\s*[–\-~]\s*R(\d{4})", re.M)
MAIN_RE = re.compile(r"^-\s*本段主线[：:]\s*(.+)$", re.M)
BEAT_RE = re.compile(r"^-\s*(起|承|转|合|冲突)[：:]\s*(.*)$", re.M)
DISTILL_FIELDS = ["场景", "时段", "出场", "剧情", "镜头密度", "可拍画面", "台词"]
SEM_FIELDS = ["场景", "时段", "出场", "剧情", "镜头密度", "叙述素材", "台词"]
EN_RE = re.compile(r"[A-Za-z]{2,}")
GENERIC_TITLE = re.compile(r"^(段名|未命名|untitled|第\d+段)$")


def _section(text, name):
    """取 `## name` 到下一个 `## ` 之间的正文。"""
    m = re.search(r"^##\s+%s\s*$" % re.escape(name), text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    n = re.search(r"^##\s+", rest, re.M)
    return rest[:n.start()] if n else rest


def parse_roster(text):
    """解析 ## 角色 的表格，返回 [{name, alias, identity, first, marker}]。"""
    body = _section(text, "角色")
    rows = []
    for line in body.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip().strip("*") for c in s.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue
        if cells[0] in ("角色", "人物", "姓名"):
            continue
        cells += [""] * (5 - len(cells))
        rows.append({"name": cells[0], "alias": cells[1], "identity": cells[2],
                     "first": cells[3], "marker": cells[4]})
    return rows


def parse_beats(text):
    body = _section(text, "剧情线")
    beats = {}
    for k, v in BEAT_RE.findall(body):
        beats.setdefault(k, v.strip())
    return beats


def parse_blocks(text):
    heads = list(BLOCK_RE.finditer(text))
    blocks = []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        item = {"sid": h.group(1), "title": h.group(2).strip()}
        for f in DISTILL_FIELDS:
            # [^\S\n]* 而不是 \s*：否则空字段会跨行吞掉下一行的 `- 无`
            mm = re.search(r"^[ \t]*-[ \t]*%s[：:][^\S\n]*(.*)$" % f, body, re.M)
            item[f] = mm.group(1).strip() if mm else ""
        # 模型常写成 `- 台词：- 无`，把行首破折号剥掉，才能识别成「无台词」
        item["台词"] = re.sub(r"^[-\s—–]+", "", item["台词"])
        shot_body = _field_body(body, "可拍画面")
        item["shots"] = [{"r": int(r), "text": t.strip()} for r, t in SHOT_RE.findall(shot_body)]
        item["lines"] = [{"id": a, "speaker": b.strip(), "text": c.strip(), "tone": d.strip()}
                         for a, b, c, d in LINE_RE.findall(body)]
        item["refs"] = [s["r"] for s in item["shots"]]
        blocks.append(item)
    return blocks


def _field_body(block_body, field):
    """取 `- field：` 之后到下一个同级 `- 字段：` 之间的缩进子项。"""
    m = re.search(r"^[ \t]*-[ \t]*%s[：:][^\n]*$" % field, block_body, re.M)
    if not m:
        return ""
    rest = block_body[m.end():]
    n = re.search(r"^[ \t]*-[ \t]*[^-\s][^：:]{0,6}[：:]", rest, re.M)
    return rest[:n.start()] if n else rest


def parse(text):
    m = HEAD_RE.search(text)
    ep = m.group(1) if m else None
    rng = RANGE_RE.search(text)
    main = MAIN_RE.search(text)
    return {"episode": ep, "title": (m.group(2).strip() if m else ""),
            "range": (int(rng.group(1)), int(rng.group(2))) if rng else None,
            "main": main.group(1).strip() if main else "",
            "roster": parse_roster(text), "beats": parse_beats(text),
            "blocks": parse_blocks(text)}


def fix_head(text, ep, title, r_lo, r_hi):
    """把首行与源行范围改成程序核准值，避免模型抄错。"""
    lines = text.strip().splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^#\s+E\d{2}", ln):
            got = ln.split(None, 2)
            name = got[2].strip() if len(got) > 2 else title
            lines[i] = "# %s %s" % (ep, name or title)
            break
    else:
        lines.insert(0, "# %s %s" % (ep, title))
    rng = "R%04d–R%04d" % (r_lo, r_hi)
    for i, ln in enumerate(lines):
        if re.match(r"^-\s*源行范围[：:]", ln):
            lines[i] = "- 源行范围：" + rng
            break
    else:
        lines.insert(1, "- 源行范围：" + rng)
    return "\n".join(lines).strip() + "\n"


def validate(text, ep, r_lo, r_hi, allowed_en=None, want_name=False):
    """返回 (errors, warns, stats)。errors 原样回喂模型。"""
    allowed_en = set(allowed_en or C.DEFAULT_ALLOWED_EN)
    errors, warns = [], []
    p = parse(text)
    if p["episode"] != ep:
        errors.append("首行应为 `# %s 段名`，当前为 %r" % (ep, p["episode"]))
    if want_name and (not p["title"] or GENERIC_TITLE.match(p["title"])):
        errors.append("首行段名仍是占位词「%s」，请用 4–12 字概括本段事件（如「天机湖垂钓」）"
                      % (p["title"] or "空"))

    roster = p["roster"]
    if not roster:
        errors.append("缺「## 角色」表格，或表格为空（至少列 1 人）")
    for r in roster:
        if not r["name"]:
            errors.append("角色表有一行缺姓名")
            continue
        if r["first"] and not re.match(r"^R\d{4}$", r["first"]):
            errors.append("角色「%s」的首次应是 R#### 行号，当前 %r" % (r["name"], r["first"]))
        elif r["first"] and not (r_lo <= int(r["first"][1:]) <= r_hi):
            errors.append("角色「%s」的首次 R%s 越界（本段 R%04d–R%04d）"
                          % (r["name"], r["first"][1:], r_lo, r_hi))
        if not r["marker"]:
            warns.append("角色「%s」没写标志物" % r["name"])
    names = {r["name"] for r in roster}

    if not p["main"]:
        errors.append("缺「- 本段主线：」，或内容为空")
    elif len(re.sub(r"\s", "", p["main"])) > 60:
        errors.append("本段主线 %d 字，超 40 字，请压到一句话"
                      % len(re.sub(r"\s", "", p["main"])))
    for k in ("起", "承", "转", "合"):
        if not p["beats"].get(k):
            errors.append("剧情线缺「- %s：」或内容为空" % k)

    blocks = p["blocks"]
    if not blocks:
        return errors + ["没有任何 `### %s-S01` 场景卡" % ep], warns, {}
    expect = ["%s-S%02d" % (ep, i + 1) for i in range(len(blocks))]
    if [b["sid"] for b in blocks] != expect:
        errors.append("场景卡编号不连续：应从 %s 起连续 %d 个，实际 %s"
                      % (expect[0], len(blocks), [b["sid"] for b in blocks][:8]))

    all_lines = []
    for b in blocks:
        for f in ("场景", "时段", "出场", "剧情"):
            if not b[f]:
                errors.append("%s 缺字段「%s」" % (b["sid"], f))
        if not b["shots"]:
            errors.append("%s 的可拍画面为空（至少 1 条 `- R#### 动作`）" % b["sid"])
        for s in b["shots"]:
            if not (r_lo <= s["r"] <= r_hi):
                errors.append("%s 可拍画面引用了越界行号 R%04d" % (b["sid"], s["r"]))
            n = len(re.sub(r"\s", "", s["text"]))
            if n < 5:
                errors.append("%s「%s」仅 %d 字，可拍画面需 6–25 字" % (b["sid"], s["text"], n))
            elif n > 28:
                errors.append("%s「%s」%d 字，超 25 字，请拆成两条" % (b["sid"], s["text"], n))
            elif n > 25:
                warns.append("%s 可拍画面 %d 字，略超 25 字" % (b["sid"], n))
            hit = [w for w in C.BAN_ACTION if w in s["text"]]
            if hit:
                errors.append("%s 可拍画面含不可拍词「%s」：%s（改成可见动作）"
                              % (b["sid"], hit[0], s["text"]))
        if b["台词"] and not b["lines"] and b["台词"] not in ("无", "（无）"):
            errors.append("%s 台词栏非空但没有 `%s-L### | 说话人 | 台词 | 语气` 行"
                          % (b["sid"], ep))
        for ln in b["lines"]:
            all_lines.append(ln)
            if not ln["id"].startswith(ep + "-L"):
                errors.append("%s 台词编号不属于本段：%s" % (b["sid"], ln["id"]))
            if not ln["text"]:
                errors.append("%s %s 台词为空" % (b["sid"], ln["id"]))
            if not ln["speaker"]:
                errors.append("%s %s 缺说话人" % (b["sid"], ln["id"]))
            elif names and ln["speaker"] not in names:
                warns.append("%s 说话人「%s」不在角色表" % (b["sid"], ln["speaker"]))

    ids = [x["id"] for x in all_lines]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        errors.append("台词编号重复：%s" % "、".join(dup[:8]))
    if ids:
        exp = ["%s-L%03d" % (ep, i + 1) for i in range(len(ids))]
        if ids != exp:
            bad = next((i for i, (a, b) in enumerate(zip(ids, exp)) if a != b), 0)
            errors.append("台词编号不连续：第 %d 条应为 %s，实际 %s"
                          % (bad + 1, exp[bad], ids[bad]))

    for w in sorted(set(EN_RE.findall(text))):
        if w not in allowed_en:
            errors.append("出现英文残留「%s」，请改写为中文（原剧拟声除外）" % w)

    stats = {"blocks": len(blocks), "lines": len(ids), "chars": len(roster),
             "shots": sum(len(b["shots"]) for b in blocks),
             "scenes": [b["场景"] for b in blocks]}
    return errors, warns, stats


# ---------------- 投影：蒸馏稿 → 语义 md ----------------
def project(text):
    """把蒸馏稿确定性地投影成语义 md（可拍画面 → 叙述素材）。"""
    p = parse(text)
    out = ["# %s %s" % (p["episode"], p["title"]),
           "",
           "- 原标题：%s" % p["title"],
           "- 源行范围：R%04d–R%04d" % p["range"] if p["range"] else "- 源行范围：不明",
           ""]
    for b in p["blocks"]:
        out.append("### %s %s" % (b["sid"], b["title"]))
        for f in ("场景", "时段", "出场", "剧情", "镜头密度"):
            out.append("- %s：%s" % (f, b[f] or "不明"))
        out.append("- 叙述素材：")
        for s in b["shots"]:
            out.append("  - R%04d %s" % (s["r"], s["text"]))
        out.append("- 台词：")
        if b["lines"]:
            for ln in b["lines"]:
                out.append("  - %s | %s | %s | %s"
                           % (ln["id"], ln["speaker"], ln["text"], ln["tone"] or "平"))
        else:
            out.append("  - 无")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def project_dir(distill_dir, out_dir, episodes=None):
    os.makedirs(out_dir, exist_ok=True)
    done = []
    for f in sorted(glob.glob(os.path.join(distill_dir, "E*.md"))):
        ep = os.path.basename(f)[:3]
        if episodes and ep not in episodes:
            continue
        text = open(f, encoding="utf-8").read()
        p = parse(text)
        if not p["blocks"]:
            print("跳过（无场景卡）：%s" % f)
            continue
        name = "%s_%s.md" % (ep, C.safe_name(p["title"]))
        open(os.path.join(out_dir, name), "w", encoding="utf-8").write(project(text))
        done.append(name)
    return done


# ---------------- 汇总：全片角色 / 剧情线 ----------------
def aggregate(distill_dir, out_dir=None):
    out_dir = out_dir or distill_dir
    chars, segs = {}, []
    for f in sorted(glob.glob(os.path.join(distill_dir, "E*.md"))):
        p = parse(open(f, encoding="utf-8").read())
        if not p["episode"]:
            continue
        segs.append({"ep": p["episode"], "title": p["title"], "main": p["main"],
                     "beats": p["beats"],
                     "scenes": [{"sid": b["sid"], "name": b["title"],
                                 "scene": b["场景"], "time": b["时段"],
                                 "cast": b["出场"], "density": b["镜头密度"],
                                 "drama": b["剧情"]} for b in p["blocks"]]})
        for r in p["roster"]:
            if not r["name"]:
                continue
            c = chars.setdefault(r["name"], {"name": r["name"], "aliases": [],
                                             "identity": r["identity"], "marker": r["marker"],
                                             "first": r["first"], "segments": [], "count": 0})
            for a in re.split(r"[、,，/／]", r["alias"]):
                a = a.strip()
                if a and a != r["name"] and a not in c["aliases"]:
                    c["aliases"].append(a)
            if not c["identity"] and r["identity"]:
                c["identity"] = r["identity"]
            if not c["marker"] and r["marker"]:
                c["marker"] = r["marker"]
            if r["first"] and (not c["first"] or
                               int(r["first"][1:]) < int(c["first"][1:])):
                c["first"] = r["first"]
            if p["episode"] not in c["segments"]:
                c["segments"].append(p["episode"])
            c["count"] += 1

    roster = sorted(chars.values(), key=lambda x: (-len(x["segments"]), -x["count"], x["name"]))
    os.makedirs(out_dir, exist_ok=True)
    json.dump(roster, open(os.path.join(out_dir, "characters.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(segs, open(os.path.join(out_dir, "plot.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    md = ["# 全片角色总表", "",
          "- 段落数：%d" % len(segs),
          "- 角色数：%d（按出场段落数排序）" % len(roster), "",
          "| 角色 | 原文称呼 | 身份 | 标志物 | 首次 | 出场段数 | 出场段落 |",
          "|---|---|---|---|---|---|---|"]
    for c in roster:
        md.append("| %s | %s | %s | %s | %s | %d | %s |"
                  % (c["name"], "、".join(c["aliases"]) or "—", c["identity"] or "—",
                     c["marker"] or "—", c["first"] or "—", len(c["segments"]),
                     "、".join(c["segments"])))
    open(os.path.join(out_dir, "00_角色总表.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")

    md = ["# 全片剧情线", ""]
    for s in segs:
        md += ["## %s %s" % (s["ep"], s["title"]), "",
               "- 主线：%s" % (s["main"] or "—")]
        for k in ("起", "承", "转", "合", "冲突"):
            if s["beats"].get(k):
                md.append("- %s：%s" % (k, s["beats"][k]))
        if s["scenes"]:
            md += ["", "| 场景卡 | 地点 | 时段 | 出场 | 密度 | 剧情 |", "|---|---|---|---|---|---|"]
            for sc in s["scenes"]:
                md.append("| %s %s | %s | %s | %s | %s | %s |"
                          % (sc["sid"], sc["name"], sc["scene"], sc["time"],
                             sc["cast"], sc["density"], sc["drama"]))
        md.append("")
    open(os.path.join(out_dir, "00_剧情线.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    return roster, segs


def chars_hint(distill_dir, before_ep=None, limit=40):
    """已蒸馏段落出现过的人物，供下一段提示词参照。"""
    paths = sorted(glob.glob(os.path.join(distill_dir, "E*.md")))
    if before_ep:
        paths = [p for p in paths if os.path.basename(p)[:3] < before_ep]
    names = []
    for f in paths:
        for r in parse_roster(open(f, encoding="utf-8").read()):
            if r["name"] and r["name"] not in names:
                names.append(r["name"])
    if not names:
        return ""
    return "、".join(names[:limit])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    v.add_argument("--file", required=True)
    v.add_argument("--ep", required=True)
    v.add_argument("--lo", type=int, required=True)
    v.add_argument("--hi", type=int, required=True)

    pr = sub.add_parser("project")
    pr.add_argument("--distill", required=True)
    pr.add_argument("--out", required=True)
    pr.add_argument("--episodes", default="")

    ag = sub.add_parser("aggregate")
    ag.add_argument("--distill", required=True)
    ag.add_argument("--out", default="")

    hi = sub.add_parser("hint")
    hi.add_argument("--distill", required=True)
    hi.add_argument("--before", default="")

    args = ap.parse_args()
    if args.cmd == "validate":
        text = open(args.file, encoding="utf-8").read()
        errs, warns, stats = validate(text, args.ep, args.lo, args.hi)
        print(json.dumps({"errors": errs, "warns": warns, "stats": stats},
                         ensure_ascii=False, indent=2))
        return 1 if errs else 0
    if args.cmd == "project":
        eps = {x.strip() for x in args.episodes.split(",") if x.strip()} or None
        for n in project_dir(args.distill, args.out, eps):
            print("投影 → %s" % n)
        return 0
    if args.cmd == "aggregate":
        roster, segs = aggregate(args.distill, args.out or None)
        print("角色 %d 个｜段落 %d 个" % (len(roster), len(segs)))
        return 0
    print(chars_hint(args.distill, args.before or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
