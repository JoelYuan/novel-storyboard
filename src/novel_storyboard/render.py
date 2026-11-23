#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分镜计划 → 人读的镜头脚本 Markdown（给导演/审核看，不是给程序读的）。

    python3 render.py --plans work/plans --out work/分镜脚本.md [--one-min work/1min]

每条视频一张表：先列场景与人物道具槽，再列 12 段（每段 5 秒）的画面动作与台词。
"""
from __future__ import annotations

import argparse
import json
import os

import contract as C


def load_plans(plan_dir):
    out = []
    for fn in sorted(os.listdir(plan_dir)):
        if fn.endswith("_plan.json"):
            out.append(json.load(open(os.path.join(plan_dir, fn), encoding="utf-8")))
    return sorted(out, key=lambda p: p.get("episode") or "")


def load_cast(one_min_dir):
    """返回 {title: cast}，按文件顺序。"""
    if not one_min_dir or not os.path.isdir(one_min_dir):
        return {}
    m = {}
    for fn in sorted(os.listdir(one_min_dir)):
        if fn.endswith(".json"):
            o = json.load(open(os.path.join(one_min_dir, fn), encoding="utf-8"))
            m[o["title"]] = o.get("cast") or {}
    return m


def render(plans, casts, title="分镜脚本"):
    L = ["# %s" % title, ""]
    total_v = sum(len(p.get("videos") or []) for p in plans)
    L.append("> %d 集 / %d 条视频 / 每条 60 秒（12 段 × 5 秒）。"
             "台词栏标注 `编号` 的为原文逐字引用，`C` 号为压缩改写。" % (len(plans), total_v))
    L.append("")

    n = 0
    for plan in plans:
        L += ["## %s %s" % (plan.get("episode"), plan.get("episode_title", "")), ""]
        for v in plan.get("videos") or []:
            n += 1
            vid = "V%02d" % n
            L += ["### %s　%s" % (vid, v["title"]), ""]
            L.append("- 一句话：%s" % v.get("logline", ""))
            L.append("- 来源：%s" % v.get("src", ""))
            L.append("- 场景：**%s**（参考图 Picture 1）" % v["scene"])

            cast = casts.get(v["title"]) or {}
            people = (cast.get("characters") or []) + (cast.get("props") or [])
            if people:
                tag = "、".join("%s＝P%d" % (x["slot"], x["picture"]) for x in people)
                L.append("- 人物道具：%s" % tag)
            else:
                L.append("- 人物道具：%s" % ("、".join(v["characters"] + v["props"]) or "无"))
            L.append("")

            L.append("| 段 | 时间 | 画面动作（拍得到的） | 台词 | 出处 |")
            L.append("|---|---|---|---|---|")
            for s in v["segments"]:
                d = s.get("dialogue")
                if d:
                    txt = "**%s**（%s）：%s" % (d["speaker"], d.get("delivery", ""), d["text"])
                    src = d.get("line") or ""
                else:
                    txt, src = "—", ""
                L.append("| %d | %d–%ds | %s | %s | %s |"
                         % (s["i"], (s["i"] - 1) * 5, s["i"] * 5, s["action"], txt, src))
            L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--one-min", default="")
    ap.add_argument("--title", default="分镜脚本")
    args = ap.parse_args()
    plans = load_plans(args.plans)
    if not plans:
        raise SystemExit("没有找到 *_plan.json")
    md = render(plans, load_cast(args.one_min), args.title)
    open(args.out, "w", encoding="utf-8").write(md)
    print("写出 %s（%d 集 / %d 条）" % (args.out, len(plans),
                                       sum(len(p.get("videos") or []) for p in plans)))


if __name__ == "__main__":
    main()
