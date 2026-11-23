#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层产物验收：语义 md / 1 分钟 JSON / 工作流 JSON，输出 校验报告.md。

    python3 verify.py --work work

重点检查（都是实测踩过的坑）：
  - 工作流与模板同构（节点/连线/12 段）
  - 每段拍点精确铺满 5.0 秒
  - <Picture k> 在同一条视频 12 段间恒定（否则人物串脸）
  - 画面句无侧面描写词、无英文；台词 ≤21 字且带 [Chinese]
  - 参考图槽位 ≤6
  - 语义 md 编号连续、行号不越界
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import contract as C

TIME_RE = re.compile(r"^(\d+\.\d)～(\d+\.\d)秒：(.+)$", re.M)
PIC_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,8})对应<Picture (\d+)>")
SCENE_RE = re.compile(r"构图：([^（(]+)（see <Picture (\d+)>")
EN_RE = re.compile(r"[A-Za-z]{2,}")


def check_semantic(work, book):
    errs, rows = [], []
    seg_dir = os.path.join(work, "segments")
    sem_dir = os.path.join(work, "semantic")
    for fn in sorted(os.listdir(seg_dir)) if os.path.isdir(seg_dir) else []:
        m = re.match(r"^seg(\d+)\.txt$", fn)
        if not m:
            continue
        head = open(os.path.join(seg_dir, fn), encoding="utf-8").readline()
        h = re.match(r"^#\s*(E\d{2})\s*(.*?)\s+源行\s*R(\d{4})\s*[–\-~]\s*R(\d{4})", head)
        if not h:
            errs.append("%s 头部不合规：%s" % (fn, head.strip()[:60]))
            continue
        ep, title, lo, hi = h.group(1), h.group(2), int(h.group(3)), int(h.group(4))
        cand = [f for f in os.listdir(sem_dir) if f.startswith(ep + "_") and f.endswith(".md")] \
            if os.path.isdir(sem_dir) else []
        if not cand:
            errs.append("%s 缺语义 md" % ep)
            continue
        text = open(os.path.join(sem_dir, cand[0]), encoding="utf-8").read()
        e, _w, st = C.validate_semantic(text, ep, lo, hi, book.allowed_en)
        errs += ["%s：%s" % (ep, x) for x in e]
        rows.append({"ep": ep, "title": title, "blocks": st.get("blocks", 0),
                     "lines": st.get("lines", 0), "file": cand[0]})
    return errs, rows


def check_1min(path, book):
    errs = []
    o = json.load(open(path, encoding="utf-8"))
    segs = o.get("segments") or []
    if len(segs) != 12:
        errs.append("段数 %d（应 12）" % len(segs))
    fixed = [segs[0]["p"][0]] if segs else []
    for s in segs:
        if s["p"][0] != o["scene"]:
            errs.append("第%s段 p[0] 不是 scene" % s["i"])
        for x in s["p"]:
            if x not in book.slots:
                errs.append("第%s段 槽位 %s 非法" % (s["i"], x))
            if x not in fixed:
                fixed.append(x)
    if len(fixed) > 6:
        errs.append("固定槽位 %d 个超 6" % len(fixed))
    for k, v in (o.get("cast") or {}).items():
        if isinstance(v, list):
            for it in v:
                if it["picture"] < 1 or it["picture"] > 6:
                    errs.append("cast %s picture=%d 越界" % (it["slot"], it["picture"]))
    return errs, {"slots": len(fixed),
                  "dialogue": sum(1 for s in segs for b in s["b"] if b[0] == "D")}


def check_workflow(path, book, base, expect_d=None):
    errs, warns = [], []
    try:
        o = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return ["JSON 解析失败：%s" % e], warns, {}
    nodes = o.get("nodes") or []
    links = o.get("links") or []
    if len(nodes) != base["nodes"]:
        errs.append("节点 %d 个（模板 %d）" % (len(nodes), base["nodes"]))
    if len(links) != base["links"]:
        errs.append("连线 %d 条（模板 %d）" % (len(links), base["links"]))
    segs = [n for n in nodes if n.get("type") == "AICG3D_H3SequenceSegment"]
    if len(segs) != 12:
        errs.append("段节点 %d 个（应为 12）" % len(segs))

    maps, dcount, full50, media_counts = [], 0, 0, []
    for n in segs:
        named = n.get("widgets_values_named") or {}
        pr = named.get("prompt")
        if not isinstance(pr, str) or not pr.strip():
            errs.append("节点 %s prompt 为空或类型错误" % n.get("id"))
            continue
        hits = TIME_RE.findall(pr)
        if not hits:
            errs.append("节点 %s 没有时间轴拍点" % n.get("id"))
            continue
        if hits[-1][1] == "5.0":
            full50 += 1
        else:
            errs.append("节点 %s 时间轴只到 %ss" % (n.get("id"), hits[-1][1]))
        mp = dict(PIC_RE.findall(pr))
        sm = SCENE_RE.search(pr)
        if sm:
            mp[sm.group(1).strip()] = sm.group(2)
        maps.append(mp)
        for t0, t1, body in hits:
            if "says:" in body:
                dcount += 1
                if "<d>[Chinese]" not in body:
                    errs.append("节点 %s 台词缺 <d>[Chinese]" % n.get("id"))
                m = re.search(r"<d>\[Chinese\](.*?)</d>", body)
                if m and len(re.sub(r"\s", "", m.group(1))) > 21:
                    errs.append("节点 %s 台词 %d 字超 21" % (n.get("id"), len(m.group(1))))
            else:
                for w in C.BAN_ACTION:
                    if w in body:
                        errs.append("节点 %s 画面句含侧面描写词「%s」：%s"
                                    % (n.get("id"), w, body[:40]))
                        break
                if EN_RE.search(body):
                    errs.append("节点 %s 画面句含英文：%s" % (n.get("id"), body[:40]))
        ms = named.get("inline_media_state")
        try:
            items = json.loads(ms)["items"] if isinstance(ms, str) else (ms or {}).get("items", [])
        except Exception:
            items = []
        media_counts.append(len(items))

    slot_total = len(set().union(*[set(m) for m in maps])) if maps else 0
    if media_counts and any(c != media_counts[0] for c in media_counts):
        errs.append("各段 inline_media 图片数不一致：%s" % sorted(set(media_counts)))
    if media_counts and media_counts[0] != slot_total:
        warns.append("inline_media %d 张，固定槽位 %d 个（可能有角色未上镜）"
                     % (media_counts[0], slot_total))

    bad = []
    names = set().union(*[set(m) for m in maps]) if maps else set()
    for name in names:
        nos = {m[name] for m in maps if name in m}
        if len(nos) > 1:
            bad.append("%s→%s" % (name, sorted(nos)))
    if bad:
        errs.append("参考图编号跨段漂移（会串脸）：" + "；".join(bad))
    if expect_d is not None and dcount != expect_d:
        warns.append("台词拍点 %d 个，与 1min JSON 的 %d 个不一致" % (dcount, expect_d))

    return errs, warns, {"nodes": len(nodes), "links": len(links), "segs": len(segs),
                         "full50": full50, "dialogue": dcount, "slots": slot_total}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--tables", default="")
    ap.add_argument("--template", default="")
    args = ap.parse_args()

    work = args.work
    cfg_path = os.path.join(work, "story.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    tables = args.tables or cfg.get("tables") or os.path.join(work, "tables.py")
    template = args.template or cfg.get("template")
    if not os.path.isabs(tables):
        tables = os.path.join(work, tables)
    book = C.Book(C.load_tables(tables))

    tpl = json.load(open(template, encoding="utf-8"))
    base = {"nodes": len(tpl["nodes"]), "links": len(tpl["links"])}

    sem_err, sem_rows = check_semantic(work, book)

    rows, all_err, all_warn = [], [], []
    src = os.path.join(work, "1min")
    if os.path.isdir(src):
        for fn in sorted(os.listdir(src)):
            if not fn.endswith(".json"):
                continue
            v = json.load(open(os.path.join(src, fn), encoding="utf-8"))
            e1, i1 = check_1min(os.path.join(src, fn), book)
            wf = os.path.join(work, "workflow", fn)
            e2, w2, st = (["缺少对应工作流 JSON"], [], {}) if not os.path.exists(wf) \
                else check_workflow(wf, book, base, i1["dialogue"])
            all_err += ["%s：%s" % (v["id"], x) for x in e1 + e2]
            all_warn += ["%s：%s" % (v["id"], x) for x in w2]
            rows.append({"id": v["id"], "title": v["title"], "scene": v["scene"],
                         **st, "slots1": i1["slots"], "d1": i1["dialogue"],
                         "ok": not (e1 + e2)})

    L = ["# 全链路校验报告", "",
         "- 工作目录：`%s`" % work,
         "- 模板：%d nodes / %d links（产物应与之同构）" % (base["nodes"], base["links"]),
         "- 语义：%d 集 / %d 场景块 / %d 台词" % (
             len(sem_rows), sum(r["blocks"] for r in sem_rows),
             sum(r["lines"] for r in sem_rows)),
         "- 视频：%d 条" % len(rows), "",
         "## 语义 md", "",
         "| 集 | 标题 | 场景块 | 台词 | 文件 |", "|---|---|---|---|---|"]
    for r in sem_rows:
        L.append("| %s | %s | %d | %d | `%s` |"
                 % (r["ep"], r["title"], r["blocks"], r["lines"], r["file"]))
    L += ["", "## 视频", "",
          "| 视频 | 标题 | 场景 | 节点 | 连线 | 段 | 满5.0秒 | 台词 | 槽位 | 结论 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                 % (r["id"], r["title"], r["scene"], r.get("nodes", "-"),
                    r.get("links", "-"), r.get("segs", "-"), r.get("full50", "-"),
                    r.get("dialogue", "-"), r.get("slots", r.get("slots1", "-")),
                    "通过" if r["ok"] else "**失败**"))
    L += ["", "## 问题", ""]
    L += ["- %s" % e for e in sem_err + all_err] or ["无"]
    L += ["", "## 告警", ""]
    L += ["- %s" % w for w in all_warn] or ["无"]

    out = os.path.join(work, "校验报告.md")
    open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    print("\n报告：%s" % out)
    sys.exit(1 if (sem_err or all_err) else 0)


if __name__ == "__main__":
    main()
