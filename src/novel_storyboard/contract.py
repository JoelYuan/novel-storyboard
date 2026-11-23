#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约层：设定表加载、语义 md 解析/校验、分镜计划校验与确定性修补。

这一层是「文员」的守则执行者——模型输出一律先过这里，不合规就打回，
能确定性收口的（裁长度、清溯源编号）不浪费模型调用。

设定表（tables.py）必须提供：
    STYLE_BASE, STYLE_TAIL, CANVAS_RULE
    SCENES  dict[key] = (显示名, 文件名, 光照与色调, 环境音)
    CHARS   dict[key] = (显示名, 外貌描述, 发质规则)
    PROPS   dict[key] = (显示名, 文件名, 描述)
    VOICES  dict[key] = (role_id, 中文名, 描述, language, age, gender, pitch, pace)
可选：
    MUTE            list[str]   哑角，不得有台词
    SPEAKER_ALIAS   dict[str,str] 原文称呼 → 表内 key
    LOCATION_HINTS  dict[str,str] 原文地点词 → 场景 key
    ALLOWED_EN      set[str]    允许保留的英文（原剧拟声/台标）
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from collections import OrderedDict

DEFAULT_ALLOWED_EN = {"Ma", "Ha", "Ri", "Yu", "TVBS", "TVIS", "please", "yes",
                      "STOP", "VIP", "NO", "KO", "OK"}

# 侧面描写 / 心理 / 转场 / 因果 / 评价类词：一出现就说明不是「拍得到的画面」
BAN_ACTION = [
    "语毕", "话一说完", "就在此时", "另外这边", "镜头带到", "再看", "心想", "心中",
    "暗忖", "恍然", "意识到", "看出端倪", "得知", "于是", "不料", "岂不是", "既然",
    "果然", "终于", "多时", "多日", "原来", "排山倒海", "惊天动地", "彷佛", "仿佛",
    "宛如", "般的", "似的", "全神贯注", "不可一世", "悻悻然", "狂嚣", "黯然",
    "配乐响起", "紧张紧张", "只见", "因为", "所以", "竟然", "不由", "忍不住",
    "心中一", "心头一", "杀气腾腾", "气势惊人",
]

# 台词/动作里可以留着的中文标点（比较时统一剥掉）
_PUNCT = r"[，。！？；：…、,.!?;:\"“”‘’（）()【】《》\s]"


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|：\s]+", "_", name or "").strip("_") or "untitled"


def load_tables(path: str):
    """按路径导入设定表模块（不复制、不改写）。"""
    if not os.path.exists(path):
        raise FileNotFoundError("设定表不存在：%s" % path)
    mod_name = "story_tables_" + hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Book:
    """槽位书：场景 / 人物 / 道具 / 音色 / 哑角 / 地点别名。"""

    def __init__(self, tables, mute=None):
        self.T = tables
        self.scenes = list(tables.SCENES)
        self.chars = list(tables.CHARS)
        self.props = list(tables.PROPS)
        self.mute = set(mute if mute is not None else getattr(tables, "MUTE", []))
        self.alias = dict(getattr(tables, "SPEAKER_ALIAS", {}))
        self.voices = dict(getattr(tables, "VOICES", {}))
        self.hints = dict(getattr(tables, "LOCATION_HINTS", {}))
        # 白名单取并集：默认拟声/台标永远允许，设定表只能追加、不能收窄
        self.allowed_en = set(DEFAULT_ALLOWED_EN) | set(getattr(tables, "ALLOWED_EN", set()))

        self.slots = OrderedDict()
        for name, v in tables.SCENES.items():
            self.slots[name] = {"kind": "scene", "name": v[0], "file": v[1] + ".png",
                                "light": v[2], "sound": v[3]}
        for key, v in tables.CHARS.items():
            self.slots[key] = {"kind": "char", "name": v[0],
                               "file": "P%02d_%s.png" % (len(self.slots) + 1, key),
                               "appearance": v[1], "hair": v[2]}
        for key, v in tables.PROPS.items():
            self.slots[key] = {"kind": "prop", "name": v[0], "file": v[1] + ".png",
                               "desc": v[2]}
        self.label = {k: "S%d" % (i + 1) for i, k in enumerate(self.slots)}

    def canon(self, name):
        if name in self.slots:
            return name
        if name in self.alias:
            return self.alias[name]
        raise KeyError("角色/说话人 %r 不在设定表" % name)

    def whitelist_text(self):
        return "\n".join([
            "场景（单选，%d 个）：%s" % (len(self.scenes), " ".join(self.scenes)),
            "人物（%d 个）：%s" % (len(self.chars), " ".join(self.chars)),
            "道具（%d 个）：%s" % (len(self.props), " ".join(self.props) or "（本片无道具槽）"),
        ])

    def hint_text(self):
        if not self.hints:
            return ""
        pairs = [("%s→%s" % (k, v)) for k, v in self.hints.items()]
        return "原文地点映射：" + "；".join(pairs) + "。"

    def mute_text(self):
        return "、".join(sorted(self.mute))


# ---------------- 语义 md ----------------
HEAD_RE = re.compile(r"^#\s+(E\d{2})\s*(.*)$", re.M)
BLOCK_RE = re.compile(r"^###\s+(E\d{2}-S\d{2,3})\s*(.*)$", re.M)
LINE_RE = re.compile(r"^\s*-\s+(E\d{2}-L\d{3})\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*$", re.M)
REF_RE = re.compile(r"\bR(\d{4})\b")
EN_RE = re.compile(r"[A-Za-z]{2,}")
FIELDS = ["场景", "时段", "出场", "剧情", "镜头密度", "叙述素材", "台词"]


def parse_semantic(text):
    """把语义 md 解析为结构化对象。"""
    m = HEAD_RE.search(text)
    ep = m.group(1) if m else None
    heads = list(BLOCK_RE.finditer(text))
    blocks = []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        item = {"sid": h.group(1), "title": h.group(2).strip(), "raw": body}
        for f in FIELDS:
            # [^\S\n]* 而不是 \s*：\s 会跨行吞掉下一行的子项（`- 台词：` + `  - 无`）
            mm = re.search(r"^\s*-\s*%s[：:][^\S\n]*(.*)$" % f, body, re.M)
            item[f] = mm.group(1).strip() if mm else ""
        item["refs"] = REF_RE.findall(body)
        # 叙述素材常写成「- 叙述素材：」+ 缩进子项，字段行本身为空；用子项回填
        if not item["叙述素材"]:
            subs = re.findall(r"^[ \t]+-[ \t]*(R\d{4}[^\n]*)$", body, re.M)
            if subs:
                item["叙述素材"] = "；".join(x.strip() for x in subs)
        item["lines"] = [{"id": a, "speaker": b.strip(), "text": c.strip(), "tone": d.strip()}
                         for a, b, c, d in LINE_RE.findall(body)]
        blocks.append(item)
    return {"episode": ep, "title": (m.group(2).strip() if m else ""), "blocks": blocks}


def sem_index(text):
    """台词编号 → 原文，用于核对「逐字引用」是否失真。"""
    return {ln["id"]: ln["text"] for b in parse_semantic(text)["blocks"] for ln in b["lines"]}


def validate_semantic(text, ep, r_lo, r_hi, allowed_en=None):
    """返回 (errors, warns, stats)。errors 会原样回喂模型修正。"""
    allowed_en = set(allowed_en or DEFAULT_ALLOWED_EN)
    errors, warns = [], []
    parsed = parse_semantic(text)
    if parsed["episode"] != ep:
        errors.append("文件首行应为 `# %s 标题`，当前为 %r" % (ep, parsed["episode"]))
    blocks = parsed["blocks"]
    if not blocks:
        return ["没有任何 `### %s-S01` 场景块" % ep], warns, {}

    sids = [b["sid"] for b in blocks]
    expect = ["%s-S%02d" % (ep, i + 1) for i in range(len(blocks))]
    if sids != expect:
        errors.append("场景块编号不连续：应为 %s 起连续 %d 个，实际 %s"
                      % (expect[0], len(blocks), sids[:8]))

    lines_all = []
    for b in blocks:
        for f in ("场景", "出场", "剧情", "叙述素材"):
            if not b[f]:
                errors.append("%s 缺字段「%s」" % (b["sid"], f))
        if not b["refs"]:
            errors.append("%s 叙述素材没有 R 行号" % b["sid"])
        for r in b["refs"]:
            if not (r_lo <= int(r) <= r_hi):
                errors.append("%s 引用了越界行号 R%s（源范围 R%04d–R%04d）"
                              % (b["sid"], r, r_lo, r_hi))
        if b["台词"] and not b["lines"] and b["台词"] not in ("无", "（无）"):
            errors.append("%s 台词栏非空但没有 `%s-L### | 说话人 | 台词 | 语气` 行"
                          % (b["sid"], ep))
        for ln in b["lines"]:
            lines_all.append(ln)
            if not ln["id"].startswith(ep + "-L"):
                errors.append("%s 台词编号不属于本集：%s" % (b["sid"], ln["id"]))
            if not ln["text"]:
                errors.append("%s %s 台词为空" % (b["sid"], ln["id"]))
            if not ln["speaker"]:
                errors.append("%s %s 缺说话人" % (b["sid"], ln["id"]))

    ids = [x["id"] for x in lines_all]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        errors.append("台词编号重复：%s" % "、".join(dup[:8]))
    if ids:
        expect_ids = ["%s-L%03d" % (ep, i + 1) for i in range(len(ids))]
        if ids != expect_ids:
            bad = next((i for i, (a, b) in enumerate(zip(ids, expect_ids)) if a != b), 0)
            errors.append("台词编号不连续：第 %d 条应为 %s，实际 %s"
                          % (bad + 1, expect_ids[bad], ids[bad]))

    for w in sorted(set(EN_RE.findall(text))):
        if w not in allowed_en:
            errors.append("出现英文残留「%s」，请改写为中文（原剧英文拟声除外）" % w)

    stats = {"blocks": len(blocks), "lines": len(ids),
             "scenes": [b["场景"] for b in blocks]}
    return errors, warns, stats


# ---------------- 分镜计划 ----------------
def _clean(s):
    return re.sub(_PUNCT, "", s or "")


def validate_plan(plan, book, ep, index, want=None):
    """返回 (errors, warns)。errors 需模型修正；warns 可确定性修补。"""
    errors, warns = [], []
    if plan.get("episode") != ep:
        errors.append("episode 应为 %s，实际 %r" % (ep, plan.get("episode")))
    vids = plan.get("videos")
    if not isinstance(vids, list) or not vids:
        return ["videos 必须是非空数组"], warns
    if want is not None and len(vids) != want:
        errors.append("videos 恰好 %d 条，实际 %d 条（多了合并主线、少了拆分）" % (want, len(vids)))
    elif len(vids) > 4:
        errors.append("videos 共 %d 条，超过 4 条，请按主线合并" % len(vids))

    for vi, v in enumerate(vids, 1):
        tag = "第%d条视频" % vi
        shape_bad = False
        for k in ("title", "src", "logline", "scene", "characters", "props", "segments"):
            if k not in v:
                errors.append("%s 缺字段 %s" % (tag, k))
                shape_bad = True
        if not isinstance(v.get("characters"), list):
            errors.append("%s characters 必须是数组" % tag)
            shape_bad = True
        if not isinstance(v.get("props"), list):
            errors.append("%s props 必须是数组" % tag)
            shape_bad = True
        if shape_bad:
            continue
        if v["scene"] not in book.scenes:
            errors.append("%s scene「%s」不在场景槽" % (tag, v["scene"]))
        bad = [c for c in v["characters"] if c not in book.chars]
        if bad:
            errors.append("%s characters 含非法人物：%s" % (tag, "、".join(bad)))
        badp = [x for x in v["props"] if x not in book.props]
        if badp:
            errors.append("%s props 含非法道具：%s" % (tag, "、".join(badp)))
        slots = 1 + len(v["characters"]) + len(v["props"])
        if slots > 6:
            errors.append("%s 槽位 %d 个，超 6（1 场景 + 5 人物道具）" % (tag, slots))
        segs = v.get("segments")
        if not isinstance(segs, list) or len(segs) != 12:
            errors.append("%s segments 必须恰好 12 段，实际 %s"
                          % (tag, len(segs) if isinstance(segs, list) else "非数组"))
            continue
        for i, s in enumerate(segs, 1):
            if s.get("i") != i:
                errors.append("%s 第%d段 i 应为 %d" % (tag, i, i))
            a = (s.get("action") or "").strip()
            n = len(re.sub(r"\s", "", a))
            if n == 0:
                errors.append("%s 第%d段 action 为空" % (tag, i))
            elif n < 5:
                errors.append("%s 第%d段 action 仅 %d 字（需 6–25）：%s" % (tag, i, n, a))
            elif n > 28:
                errors.append("%s 第%d段 action %d 字超长（需 6–25）：%s" % (tag, i, n, a))
            elif not (6 <= n <= 25):
                warns.append("%s 第%d段 action %d 字，已自动修剪到 25 字内" % (tag, i, n))
            hit = [w for w in BAN_ACTION if w in a]
            if hit:
                errors.append("%s 第%d段 action 含侧面描写词「%s」：%s"
                              % (tag, i, hit[0], a))
            if EN_RE.search(a):
                errors.append("%s 第%d段 action 含英文：%s" % (tag, i, a))
            d = s.get("dialogue")
            if d is None:
                continue
            if not isinstance(d, dict):
                errors.append("%s 第%d段 dialogue 必须是 null 或对象" % (tag, i))
                continue
            for k in ("speaker", "delivery", "text"):
                if not d.get(k):
                    errors.append("%s 第%d段 dialogue 缺 %s" % (tag, i, k))
            if not (d.get("speaker") and d.get("text")):
                continue
            if d["speaker"] not in v["characters"]:
                errors.append("%s 第%d段 说话人「%s」不在 characters"
                              % (tag, i, d["speaker"]))
            if d["speaker"] in book.mute:
                errors.append("%s 第%d段 %s 是哑角，不得有台词" % (tag, i, d["speaker"]))
            tn = len(re.sub(r"\s", "", d["text"]))
            if tn > 24:
                errors.append("%s 第%d段 台词 %d 字超 21：%s" % (tag, i, tn, d["text"]))
            elif tn > 21:
                warns.append("%s 第%d段 台词 %d 字，已自动压缩" % (tag, i, tn))
            if EN_RE.search(d["text"]):
                errors.append("%s 第%d段 台词含英文：%s" % (tag, i, d["text"]))
            ln = d.get("line") or ""
            if ln:
                if ln not in index:
                    errors.append("%s 第%d段 line %s 不在语义 md" % (tag, i, ln))
                elif _clean(d["text"]) not in _clean(index[ln]) and \
                        _clean(index[ln]) not in _clean(d["text"]):
                    errors.append("%s 第%d段 台词与 %s 原文不符：%s"
                                  % (tag, i, ln, d["text"]))

    seen = {}
    for vi, v in enumerate(vids, 1):
        for s in v.get("segments") or []:
            d = s.get("dialogue") or {}
            ln = d.get("line") if isinstance(d, dict) else None
            if ln:
                if ln in seen:
                    errors.append("第%d条 的 %s 与第%d条重复使用" % (vi, ln, seen[ln]))
                seen[ln] = vi
    return errors, warns


def sanitize_plan(plan, book, index=None):
    """确定性修补：只做不改语义的长度裁剪与溯源标记清理，返回修补记录。"""
    fixes = []
    for v in plan.get("videos") or []:
        for s in v.get("segments") or []:
            a = (s.get("action") or "").strip()
            if len(re.sub(r"\s", "", a)) > 25:
                cut = a[:25]
                for p in "，。；：、":
                    k = cut.rfind(p)
                    if k >= 18:
                        cut = cut[:k]
                        break
                s["action"] = cut
                fixes.append("action 截到 %d 字：%s" % (len(cut), cut))
            d = s.get("dialogue")
            if not isinstance(d, dict):
                continue
            if not d.get("speaker"):
                s["dialogue"] = None
                fixes.append("台词缺说话人，整条删除")
                continue
            if d["speaker"] in book.mute or d["speaker"] not in (v.get("characters") or []):
                s["dialogue"] = None
                fixes.append("说话人「%s」不可用（哑角或不在 cast），台词删除" % d["speaker"])
                continue
            if len(d.get("delivery") or "") > 8:
                d["delivery"] = d["delivery"][:8]
                fixes.append("delivery 截到 8 字")
            t = (d.get("text") or "").strip()
            if len(re.sub(r"\s", "", t)) > 21:
                cut = t[:21]
                for p in "，。；：、？！\n":
                    k = cut.rfind(p)
                    if k >= 12:
                        cut = cut[:k]
                        break
                d["text"] = cut
                fixes.append("台词截到 %d 字：%s" % (len(cut), cut))
            # 带原编号但文本已被改写 ⇒ 溯源失真，改记为压缩台词（装配时补 E##-C###）
            ln = d.get("line") or ""
            if ln and index:
                src = index.get(ln)
                if not src:
                    d["line"] = ""
                    fixes.append("%s 不在语义 md，改记为压缩台词" % ln)
                elif _clean(d["text"]) not in _clean(src) and _clean(src) not in _clean(d["text"]):
                    d["line"] = ""
                    fixes.append("%s 文本已改写，改记为压缩台词" % ln)
    return fixes
