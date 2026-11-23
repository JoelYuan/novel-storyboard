#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分镜计划 → 1 分钟分镜 JSON（确定性装配，不含任何模型调用）。

产物结构：
{
  id, episode, title, scene, src, logline, duration_sec: 60, segments_count: 12,
  cast: {scene, characters[{slot,kind,picture,ref_file}], props[...]},
  segments: [{i, time, p:[槽位], b:[拍点], beats:[{t,type,...}]}]
}
"""
from __future__ import annotations

import json
import math
import os
import re

WPS = 4.5            # 台词语速：字/秒
SEG_UNITS = 50       # 5.0 秒 = 50 个 0.1 秒


def _chars(s):
    return len(re.sub(r"\s", "", s or ""))


def pick_p(book, v, seg, prev_names):
    """按 action / 台词里出现的人名决定本段上镜对象。"""
    sc = v["scene"]
    act = seg.get("action") or ""
    d = seg.get("dialogue") or {}
    pool = set()
    if isinstance(d, dict) and d.get("speaker"):
        pool.add(d["speaker"])
    pool.update(c for c in v["characters"] if c in act)
    if isinstance(d, dict) and d.get("text"):
        pool.update(c for c in v["characters"] if c in d["text"])
    names = [c for c in v["characters"] if c in pool]
    if not names:
        names = [c for c in prev_names if c in v["characters"]] or v["characters"][:2]
    props = [x for x in v["props"]
             if x in act or (isinstance(d, dict) and x in (d.get("text") or ""))]
    return [sc] + names + props, names


def to_beats(seg, wps=WPS):
    """动作在前、台词在后；>18 字的台词独占整段，避免 动作+台词 超 5 秒。"""
    d = seg.get("dialogue")
    if not d:
        return [["A", seg["action"]]]
    dur = math.ceil(_chars(d["text"]) * 10 / wps)
    if dur <= 40:
        return [["A", seg["action"]],
                ["D", d["speaker"], d.get("delivery", ""), d["text"], d.get("line") or "", ""]]
    return [["D", d["speaker"], d.get("delivery", ""), d["text"], d.get("line") or "",
             seg["action"]]]


def build_video(book, ep, v, vid, wps=WPS):
    sc = v["scene"]

    segs, prev = [], []
    for s in v["segments"]:
        p, names = pick_p(book, v, s, prev)
        prev = names
        beats = to_beats(s, wps)
        dur = [math.ceil(_chars(b[3]) * 10 / wps) if b[0] == "D" else 10 for b in beats]
        if sum(dur) > SEG_UNITS:
            raise ValueError("%s 第%s段拍点 %.1f 秒超 5 秒" % (vid, s["i"], sum(dur) / 10))
        left = SEG_UNITS - sum(dur)
        if left:
            ai = next((k for k, b in enumerate(beats) if b[0] == "A"), len(beats) - 1)
            dur[ai] += left
        rows, tt = [], 0
        for b, dd in zip(beats, dur):
            row = {"t": "%.1f–%.1fs" % (tt / 10, (tt + dd) / 10),
                   "type": "dialogue" if b[0] == "D" else "action"}
            if b[0] == "D":
                row.update({"speaker": b[1], "delivery": b[2], "text": b[3],
                            "line": b[4], "action": b[5]})
            else:
                row["text"] = b[1]
            rows.append(row)
            tt += dd
        segs.append({"i": s["i"], "time": "0.0–5.0s", "p": p, "b": beats, "beats": rows})

    # cast 必须与实际上镜槽位完全一致：scene 恒为 Picture 1，其余按首次出现顺序，
    # 否则 cast.picture 与工作流里的 <Picture k> 对不上，换图时就会贴错人。
    order = []
    for s in segs:
        for x in s["p"]:
            if x not in order:
                order.append(x)
    if order and order[0] != sc:
        order.remove(sc)
        order.insert(0, sc)
    cast = {"scene": {"slot": sc, "kind": "scene", "picture": 1},
            "characters": [], "props": []}
    for i, x in enumerate(order):
        if x == sc:
            continue
        kind = book.slots[x]["kind"]
        cast["characters" if kind == "char" else "props"].append(
            {"slot": x, "kind": kind, "picture": i + 1})
    for item in [cast["scene"]] + cast["characters"] + cast["props"]:
        item["ref_file"] = "image%d.jpg" % item["picture"]

    return {"id": vid, "episode": ep, "title": v["title"], "scene": sc, "src": v["src"],
            "logline": v["logline"], "duration_sec": 60, "segments_count": 12,
            "cast": cast, "segments": segs}


def number_plans(plans):
    """按 episode 顺序给所有视频连续编号 V01、V02……"""
    out = []
    for plan in sorted(plans, key=lambda p: p["episode"]):
        for v in plan["videos"]:
            out.append((plan["episode"], v))
    return [("V%02d" % (i + 1), ep, v) for i, (ep, v) in enumerate(out)]


def build_one(book, ep, v, vid, wps=WPS):
    """给压缩改写的台词补可追溯编号，然后装配单条视频。"""
    for s in v["segments"]:
        d = s.get("dialogue")
        if isinstance(d, dict) and not d.get("line"):
            d["_need_c"] = True
    counters = {}
    for s in v["segments"]:
        d = s.get("dialogue")
        if isinstance(d, dict) and d.pop("_need_c", False):
            counters[ep] = counters.get(ep, 0) + 1
            d["line"] = "%s-C%03d" % (ep, counters[ep])
    return build_video(book, ep, v, vid, wps)


def assemble_all(book, plans, out_dir, wps=WPS):
    os.makedirs(out_dir, exist_ok=True)
    stats = []
    for vid, ep, v in number_plans(plans):
        try:
            obj = build_one(book, ep, v, vid, wps)
        except Exception as e:                      # 单条失败不拖垮整批
            stats.append({"id": vid, "episode": ep, "title": v.get("title", "?"),
                          "error": "%s: %s" % (type(e).__name__, e)})
            continue
        name = "%s_%s.json" % (vid, v["title"])
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        dnum = sum(1 for s in obj["segments"] for b in s["b"] if b[0] == "D")
        slots = 1 + len(v["characters"]) + len(v["props"])
        stats.append({"id": vid, "episode": ep, "title": v["title"], "scene": obj["scene"],
                      "slots": slots, "dialogue": dnum, "file": name,
                      "path": path})
    return stats
