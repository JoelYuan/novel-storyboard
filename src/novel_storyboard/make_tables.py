#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已核对的语义 md 汇总出设定表骨架 tables.py（人物/场景/道具/音色）。

    python3 make_tables.py --semantic work/semantic --out work/tables.py [--force]

这是「文员」之前的设定整理助手。原文地点/人物写法通常很碎（天机湖 / 天机湖畔 /
天机湖·湖畔钓台；一页书 / 页书），先聚类归并、滤掉「手下/众人」这类群演，
只把出现 ≥2 次的建成槽位，低频角色留在注释里备查。
"""
from __future__ import annotations

import argparse
import os
import re
from collections import Counter

import contract

GENERIC = ("手下", "部下", "人马", "门下", "众人", "一群人", "等人", "群众",
           "士兵", "弟子们", "围观")
DROP_NAMES = {"人", "众人", "人群", "所有人", "身影", "声音", "对方", "双方"}
MAX_CHARS = 40       # 人物槽上限：超出的按出现次数截断，避免设定表无限膨胀
SCENE_SUFFIX = ("总坛神殿", "总坛", "神殿", "大殿", "内堂", "殿堂", "内殿", "外殿",
                "房间", "葬地", "墓前", "奠堂", "广场", "入口", "门外", "门口")
SCENE_EDGE = ("之外", "之内", "旁边", "附近", "外", "内", "前", "后", "旁", "畔", "上", "中")
# 人物名 + 建筑后缀（宇文知日葬地、半部禅房间）→ 只保留建筑部分
SUFFIX_MAP = {"房间": "室内", "葬地": "墓地", "墓前": "墓地", "奠堂": "灵堂"}


def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _sim(a, b):
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / max(1, len(A | B))


def norm_scene(s, char_names=()):
    """去掉『·子标题』『/并列』与方位/建筑后缀，让同一地点收敛成同一个词。

    若去掉后缀后剩下的是人名（『宇文知日葬地』→『宇文知日』），说明这是
    「某人的某处」，只保留建筑部分（墓地/室内/灵堂），不要把人名当场景。
    """
    s = re.split(r"[·•/／|]", s)[0].strip()
    s = re.sub(r"^(在|于|来到|回到)", "", s)
    for suf in SCENE_SUFFIX:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            base = s[:-len(suf)]
            if base in char_names:
                return SUFFIX_MAP.get(suf, suf)
            s = base
            break
    for suf in SCENE_EDGE:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            base = s[:-len(suf)]
            if base in char_names:
                return SUFFIX_MAP.get(suf, suf)
            s = base
            break
    return s.strip()


def cluster(counter, min_count=2, use_sim=True, drop=(), prefer_short=True):
    """按包含关系 / bigram 相似度聚类。

    prefer_short=True 用于场景（天机湖 优于 天机湖畔）；
    prefer_short=False 用于人物（病兰花 优于 兰花，保留高频全称）。
    """
    reps = []
    for name, n in counter.most_common():
        if len(name) < 2 or name in drop or any(g in name for g in GENERIC):
            continue
        placed = False
        for r in reps:
            b = r["rep"]
            if name in b or b in name or (use_sim and _sim(name, b) >= 0.5):
                r["total"] += n
                r["variants"][name] = n
                if prefer_short and len(name) < len(b):
                    r["rep"] = name
                placed = True
                break
        if not placed:
            reps.append({"rep": name, "total": n, "variants": {name: n}})
    main = [r for r in reps if r["total"] >= min_count]
    minor = [r for r in reps if r["total"] < min_count]
    main.sort(key=lambda r: -r["total"])
    return main, minor


def scan(sem_dir):
    """两遍扫描：先统计人物名，再用人物名修正『人名+地点』这类场景写法。"""
    chars, scenes = Counter(), Counter()
    blocks = []
    for fn in sorted(os.listdir(sem_dir)):
        if not fn.endswith(".md"):
            continue
        text = open(os.path.join(sem_dir, fn), encoding="utf-8").read()
        blocks += contract.parse_semantic(text)["blocks"]

    for b in blocks:
        for x in re.split(r"[、,，/]", b["出场"]):
            x = x.strip()
            if x:
                chars[x] += 1
        for ln in b["lines"]:
            sp = re.sub(r"[（(].*?[）)]", "", ln["speaker"]).strip()
            if sp:
                chars[sp] += 1

    names = {n for n in chars if len(n) >= 2}
    for b in blocks:
        sc = norm_scene(b["场景"], names)
        if sc and "不明" not in sc:
            scenes[sc] += 1
    return chars, scenes


def q(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def gen(chars, scenes, story_name):
    # 场景全部建槽：一个 2000 字段常常每个地点只出现 1 次，滤掉就等于没有场景可用
    sc_main, sc_minor = cluster(scenes, 1, True, drop=DROP_NAMES)
    # 人物不按出现次数过滤：2000 字段里主角也可能只在一场出现；改为按次数取前 40 个
    ch_main, ch_minor = cluster(chars, 1, False, drop=DROP_NAMES, prefer_short=False)
    ch_minor = ch_main[MAX_CHARS:]
    ch_main = ch_main[:MAX_CHARS]

    L = []
    A = L.append
    A("# -*- coding: utf-8 -*-")
    A('"""%s 设定表（由语义 md 自动汇总，占位描述待补）。' % story_name)
    A("")
    A("槽位顺序 = 参考图编号顺序：场景在前，人物其次，道具最后。")
    A("补齐要求见 references/04_设定表与参考图.md。")
    A('"""')
    A("")
    A("STYLE_BASE = (")
    A('    "16:9，东方古风武侠写实CG，次世代3D渲染，HDR光追，32K超清质感："')
    A('    "皮肤毛孔、单根睫毛、唇纹与唇部湿润高光、织物经纬与针脚清晰可辨；"')
    A('    "浅景深，背景虚化成柔光斑。"')
    A(")")
    A('STYLE_TAIL = "全员发质真实柔软，不油亮、不结壳、无定型感，发丝滞后头部动作约零点二秒跟随。"')
    A('CANVAS_RULE = "画面无字幕、无文字、无水印。"')
    A("")
    A("# 场景槽：key → (显示名, 文件名, 光照与色调, 环境音)")
    A("# 同一地点的原文变体已归并，括号内是出现次数。")
    A("SCENES = {")
    for i, r in enumerate(sc_main, 1):
        name = r["rep"]
        A("    %s: (%s, %s," % (q(name), q(name), q("P%02d_%s" % (i, name))))
        A('        "（待补：主光方向与色温、明暗关系、跨段锁曝光）",')
        A('        "（待补：环境音）"),   # %d 次' % r["total"])
    if not sc_main:
        A('    "场景一": ("场景一", "P01_场景一", "（待补）", "（待补）"),')
    A("}")
    if sc_minor:
        A("")
        A("# 只出现 1 次的场景（未建槽，必要时手动提升）：")
        A("#   " + "、".join("%s(%d)" % (r["rep"], r["total"]) for r in sc_minor))
    A("")
    A("# 人物槽：key → (显示名, 外貌描述, 发质规则)")
    A("CHARS = {")
    for name_r in ch_main:
        name = name_r["rep"]
        A("    %s: (%s," % (q(name), q(name)))
        A('        "（待补：年龄、体态、脸型、服饰、武器与随身物；只写看得见的）",')
        A('        "（待补：发色、发型、干湿与蓬松度）"),   # %d 次' % name_r["total"])
    if not ch_main:
        A('    "主角": ("主角", "（待补）", "（待补）"),')
    A("}")
    if ch_minor:
        A("")
        A("# 只出现 1 次的人物（未建槽，群演可合并或删除）：")
        A("#   " + "、".join(r["rep"] for r in ch_minor[:40]))
    A("")
    A("# 道具槽：key → (显示名, 文件名, 描述)；没有就留空 {}")
    A("PROPS = {")
    A("}")
    A("")
    A("# 哑角：可以出场但绝不能有台词")
    A("MUTE = []")
    A("")
    A("# 原文称呼 / 描述性说法 → 表内 key（『轿内人』『老者』这类）")
    A("SPEAKER_ALIAS = {")
    A("}")
    A("")
    A("# 原文地点词 → 场景 key（归并后仍有两套叫法时必填）")
    A("LOCATION_HINTS = {")
    A("}")
    A("")
    A("# 允许保留的英文（原剧拟声 / 台标），其余 ASCII 词视为残留")
    A("ALLOWED_EN = {%s}" % ", ".join(q(x) for x in sorted(contract.DEFAULT_ALLOWED_EN)))
    A("")
    A("# 音色表：key → (role_id, 中文名, 描述, language, age, gender, pitch, pace)")
    A("VOICES = {")
    for i, r in enumerate(ch_main, 1):
        name = r["rep"]
        A("    %s: (%s, %s, \"（待补：音色质感、语速、口音、情绪基调）\"," % (
            q(name), q("role%02d" % i), q(name)))
        A('            "Chinese", "adult", "unknown", "medium", "natural"),')
    A("}")
    A("")
    A("")
    A("def music_line(state: str) -> str:")
    A('    """全片固定情绪音乐层；state ∈ open/keep/tense/soft/close。"""')
    A("    return {")
    A('        "open": "低音弦乐长音打底，加入稀疏鼓点，音量与情绪克制，跨段持续不停。",')
    A('        "keep": "同一段弦乐长音延续，鼓点不变，未换曲、未起新段。",')
    A('        "tense": "同一段音乐加入低音鼓的短击，音量略升，旋律不换，情绪收紧。",')
    A('        "soft": "同一段音乐收进低音区，弦乐变薄，只留一条长音，情绪转缓。",')
    A('        "close": "同一段音乐收束为一次低音，余音收，不从头另起一首。",')
    A("    }[state]")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--semantic", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        raise SystemExit("%s 已存在；补描述请直接编辑，确需重生成加 --force" % args.out)
    chars, scenes = scan(args.semantic)
    name = args.name or os.path.basename(os.path.dirname(os.path.abspath(args.semantic)))
    src = gen(chars, scenes, name)
    try:
        compile(src, args.out, "exec")      # 生成物必须本身可导入，否则链路跑不起来
    except SyntaxError as e:
        raise SystemExit("生成的设定表有语法错误（第 %s 行）：%s" % (e.lineno, e.text))
    open(args.out, "w", encoding="utf-8").write(src)
    sc_main, _ = cluster(scenes, 1, True, drop=DROP_NAMES)
    ch_main, _ = cluster(chars, 1, False, drop=DROP_NAMES, prefer_short=False)
    ch_main = ch_main[:MAX_CHARS]
    print("写出 %s：场景 %d 组｜人物 %d 个（已归并、滤群演）" % (args.out, len(sc_main), len(ch_main)))
    print("待补：SCENES 光照/环境音、CHARS 外貌/发质、VOICES 音色、LOCATION_HINTS、MUTE")


if __name__ == "__main__":
    main()
