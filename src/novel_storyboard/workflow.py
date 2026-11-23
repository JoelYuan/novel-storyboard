#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1 分钟分镜 JSON → MiniMax H3 长视频工作流 JSON（与模板严格同构）。

结构约束（实测模板）：
  - 21 nodes / 20 links / 1 group（校验时以此为基线，模板换了自动跟随）
  - 12 个 AICG3D_H3SequenceSegment + 1 个 AICG3D_H3FinalSequenceUpscale
  - 12 段共用同一组参考图（inline_media），<Picture k> 编号整片恒定，
    否则同一编号在不同段指代不同人物 → 人物串脸
  - 每段 prompt 分三通道：C 控制（构图/参考图绑定）、I 画面动作、D 台词(<d>[Chinese]…</d>)

节点按 type 定位，不写死 id：换模板只需保证节点类型一致。
"""
from __future__ import annotations

import copy
import json
import math
import re

WPS = 4.5

READING_RULE = ("朗读约束：仅 <d> 标签内的文本是角色对白，可被朗读；本提示词其余全部文字"
                "（含 delivery 标签、segment_handoff、构图、机位、语态、画面纪律）均为画面与控制描述，"
                "禁止朗读、禁止生成字幕、禁止添加旁白或画外音。")

T_SEG = "AICG3D_H3SequenceSegment"
T_GLOBAL = "AICG3D_H3SequenceGlobal"
T_VOICE = "AICG3D_H3CharacterVoiceLibrary"
T_MEDIA = "AICG3D_H3MediaLoader"
T_SPLIT = "AICG3D_H3PromptSegmentSplit"
T_UPSCALE = "AICG3D_H3FinalSequenceUpscale"
T_SAVE = "SaveVideo"
T_LOADER = "AICG3D_H3Loader"


def _chars(s):
    return len(re.sub(r"\s", "", s or ""))


def beat_units(b, wps=WPS):
    return math.ceil(_chars(b[3]) * 10 / wps) if b[0] == "D" else 10


def layout(beats, vid, si, warn, wps=WPS):
    """把拍点铺到 5.0 秒：动作拍点优先吸收余量，纯台词段微调末拍。"""
    ds = [beat_units(b, wps) for b in beats]
    acts = [i for i, b in enumerate(beats) if b[0] == "A"]
    guard = 0
    while sum(ds) != 50 and guard < 300:
        guard += 1
        diff = 50 - sum(ds)
        if acts:
            pool = [i for i in acts if ds[i] < 50] if diff > 0 else [i for i in acts if ds[i] > 4]
            if not pool:
                warn.append("%s 第%d段 动作拍点无法凑满 5.0 秒（%.1fs）"
                            % (vid, si, sum(ds) / 10))
                break
            i = max(pool, key=lambda j: ds[j]) if diff > 0 else min(pool, key=lambda j: ds[j])
            ds[i] += 1 if diff > 0 else -1
        else:
            i = len(ds) - 1
            if diff > 0 and ds[i] < beat_units(beats[i]) + 10:
                ds[i] += 1
            elif diff < 0 and ds[i] > 4:
                ds[i] -= 1
            else:
                warn.append("%s 第%d段 纯台词段无法对齐 5.0 秒（%.1fs）"
                            % (vid, si, sum(ds) / 10))
                break
    if sum(ds) != 50:
        ds[-1] = max(4, 50 - (sum(ds) - ds[-1]))
    out, t = [], 0
    for i, d in enumerate(ds):
        out.append((t, d, beats[i]))
        t += d
    return out


def resolve_nodes(tpl):
    """按 type 定位节点：返回 {'seg':[id...], 'global':id, ...}。"""
    by_type = {}
    for n in tpl["nodes"]:
        by_type.setdefault(n.get("type"), []).append(n)

    segs = by_type.get(T_SEG, [])

    def title_no(n):
        m = re.search(r"(\d+)", str(n.get("title") or ""))
        return int(m.group(1)) if m else 0

    if segs and all(title_no(n) for n in segs):
        segs = sorted(segs, key=title_no)

    def one(t):
        lst = by_type.get(t) or []
        return lst[0]["id"] if lst else None

    return {"seg": [n["id"] for n in segs],
            "global": one(T_GLOBAL), "voice": one(T_VOICE), "media": one(T_MEDIA),
            "split": one(T_SPLIT), "upscale": one(T_UPSCALE),
            "save": one(T_SAVE), "loader": one(T_LOADER)}


class Builder:
    def __init__(self, book, template_path):
        self.book = book
        self.T = book.T
        self.tpl = json.load(open(template_path, encoding="utf-8"))
        self.ids = resolve_nodes(self.tpl)
        if len(self.ids["seg"]) != 12:
            raise ValueError("模板里 AICG3D_H3SequenceSegment 有 %d 个，应为 12：%s"
                             % (len(self.ids["seg"]), template_path))

    # ---------- prompt ----------
    def _prompt(self, seg, laid, seg_no, idx):
        b = self.book
        p = seg["p"]
        scene = p[0]
        sc = b.slots[scene]

        head = "[Shot 1] "
        if seg_no == 1:
            head += "全片保持统一风格：" + self.T.STYLE_BASE + self.T.STYLE_TAIL + self.T.CANVAS_RULE
        else:
            head += ("承接上一段第12秒，延续前段末帧的镜头视点、光源方向与色调，"
                     "全片保持统一风格：" + self.T.STYLE_BASE + self.T.STYLE_TAIL +
                     self.T.CANVAS_RULE + "（场景12段共用）")

        dot = lambda s: s.rstrip("。")
        comp = ("[C] 构图：%s（see <Picture %d>），%s；构图采用中景加特写交替。"
                % (sc["name"], idx[scene], dot(sc["light"])))

        chars = [x for x in p if b.slots[x]["kind"] == "char"]
        refs = ""
        if chars:
            refs = "[C] " + "；".join(
                "角色%s对应<Picture %d>：%s；发质：%s"
                % (x, idx[x], dot(b.slots[x]["appearance"]), dot(b.slots[x]["hair"]))
                for x in chars) + "。"
        props = [x for x in p if b.slots[x]["kind"] == "prop"]
        if props:
            refs += "[C] " + "；".join(
                "道具%s对应<Picture %d>：%s" % (x, idx[x], dot(b.slots[x]["desc"]))
                for x in props) + "。"

        parts = ["%s（see <Picture %d>）" % (sc["name"], idx[scene])] + \
                ["%s（see <Picture %d>）" % (x, idx[x]) for x in p[1:]]
        constraint = ("[C] 【present-constraint】" + " + ".join(parts) +
                      " 只出现以上图片所代表的对象；所有人物、道具、服饰、武器必须严格遵循对应图片，不得自行新增。")

        lines = []
        for t, d, beat in laid:
            t2 = t + d
            if beat[0] == "A":
                lines.append("%.1f～%.1f秒：%s" % (t / 10, t2 / 10, beat[1].rstrip("。")))
            else:
                sp = b.canon(beat[1])
                act = (beat[5] or "").rstrip("。")
                lines.append("%.1f～%.1f秒：(%s) says: <d>[Chinese]%s</d> delivery: %s%s"
                             % (t / 10, t2 / 10, b.label[sp], beat[3], beat[2],
                                "；" + act if act else ""))

        last = laid[-1][2]
        anchor = last[1] if last[0] == "A" else (last[5] or last[3])
        handoff = ("segment_handoff: %s；人物站位延续，光源方向与色调延续，机位固定不切镜。"
                   % anchor.rstrip("。"))

        return "\n".join([head, comp, *([refs] if refs else []), constraint,
                          "[C] 画面无字幕、无文字、无水印；无旁白、无画外音。",
                          "", "\n".join(lines), "", handoff, READING_RULE])

    def _ref_doc(self, seg_no, seg, prompt, idx, laid, seed):
        parts = [{"type": "text", "content": "【第%d段｜5秒】\n" % seg_no}]
        for x in seg["p"]:
            parts.append({"type": "mention", "mediaIndex": idx[x] - 1, "sourceId": "290",
                          "previewUrl": "/view?filename=image%d.jpg&type=input" % idx[x],
                          "text": "<Picture %d>" % idx[x], "slot": "slot:%d" % (idx[x] - 1)})
            parts.append({"type": "text", "content": " "})
        for t, d, beat in laid:
            if beat[0] == "D":
                sp = self.book.canon(beat[1])
                parts.append({"type": "dialogue", "mediaIndex": idx[sp] - 1, "sourceId": "285",
                              "text": "[Chinese]%s" % beat[3], "slot": "slot:%d" % (idx[sp] - 1)})
        return {"mode": "ref_doc", "version": "minimax_h3_prompt_reference_v2",
                "text": "【第%d段｜5秒】\n%s" % (seg_no, prompt),
                "segments": [{"seg": seg_no, "duration": 5.0, "seed": seed}],
                "parts": parts}

    # ---------- 装配 ----------
    def build(self, video, warn=None):
        warn = warn if warn is not None else []
        vid, title, segments = video["id"], video["title"], video["segments"]
        tpl = copy.deepcopy(self.tpl)
        nodes = tpl["nodes"]
        byid = {str(n["id"]): n for n in nodes}
        ids = self.ids

        def named(n):
            return n.get("widgets_values_named") or n.get("widgets_named")

        def set_widget(nid, key, value, index=None):
            if nid is None:
                return
            n = byid[str(nid)]
            named(n)[key] = value
            if index is not None:
                for lst in ("widgets_values", "widgets_values1"):
                    if lst in n and index < len(n[lst]):
                        n[lst][index] = value

        def set_state(nid, key, prop_key, state, index):
            if nid is None:
                return
            n = byid[str(nid)]
            js = json.dumps(state, ensure_ascii=False)
            named(n)[key] = js
            for lst in ("widgets_values", "widgets_values1"):
                if lst in n and index < len(n[lst]):
                    n[lst][index] = js
            n["properties"][prop_key] = js

        def set_pos(nid, i, v):
            if nid is None:
                return
            n = byid[str(nid)]
            for lst in ("widgets_values", "widgets_values1"):
                if lst in n and i < len(n[lst]):
                    n[lst][i] = v

        global_text = ("%s %s %s 全程保持：运镜连贯，人物站位连续，16:9画幅，24fps，"
                       "12段时长严格一致，色温统一。"
                       % (self.T.STYLE_BASE, self.T.STYLE_TAIL, self.T.CANVAS_RULE))
        set_widget(ids["global"], "prompt", global_text, 0)
        if ids["global"] is not None:
            byid[str(ids["global"])]["properties"]["minimax_h3_prompt_reference_doc"] = json.dumps(
                {"mode": "ref_doc", "version": "minimax_h3_prompt_reference_v2",
                 "text": global_text, "segments": [], "parts": []}, ensure_ascii=False)
        set_widget(ids["global"], "resolution", "620x348", 0)
        set_widget(ids["global"], "aspect_ratio", "16:9", 1)
        set_widget(ids["global"], "fps", "24", 2)
        set_widget(ids["global"], "segment_output_directory", "video/%s_%s" % (vid, title), 18)
        set_widget(ids["save"], "filename_prefix", "video/%s_%s" % (vid, title), 0)
        set_widget(ids["upscale"], "filename_prefix", "video/%s_%s" % (vid, title), 0)
        set_widget(ids["upscale"], "target_width", 1152, 7)
        set_widget(ids["upscale"], "target_height", 640, 8)
        set_pos(ids["loader"], 23, "24")
        set_pos(ids["loader"], 24, 1245)

        # 音色表：按出现顺序注册，picture_index 与参考图槽一致
        used = {x for seg in segments for x in seg["p"]
                if self.book.slots[x]["kind"] == "char"}
        slot_no = {k: i + 1 for i, k in enumerate(self.book.slots)}
        roster = []
        for k in sorted(used, key=lambda x: slot_no[x]):
            vv = self.T.VOICES.get(k)
            if not vv:
                vv = (k.lower(), k, "默认音色。", "Chinese", "adult", "male", "medium", "natural")
            rid, _cname, desc, lang, age, gender, pitch, pace = vv
            roster.append({"role_id": rid, "name": "%d_%s" % (slot_no[k], k),
                           "mode": "described", "language": lang, "age": age,
                           "gender": gender, "pitch": pitch, "pace": pace,
                           "emotion": "neutral", "picture_index": slot_no[k]})
        set_state(ids["voice"], "voice_state", "minimax_h3_character_voice_state",
                  {"version": 1, "mode": "voice_cast", "roles": roster,
                   "source": {"mode": "reference_image", "use_character_reference": True}}, 0)

        # 整条视频固定槽位映射：scene 恒为 Picture 1，其余按首次出现顺序
        scene = segments[0]["p"][0]
        fixed = [scene]
        for seg in segments:
            for x in seg["p"]:
                if x not in fixed:
                    fixed.append(x)
        if len(fixed) > 6:
            raise ValueError("%s 参考槽位 %d 个，超过 MediaLoader 的 6 槽：%s"
                             % (vid, len(fixed), fixed))
        idx = {x: i + 1 for i, x in enumerate(fixed)}
        media = ["image%d.jpg" % i for i in range(1, len(fixed) + 1)]
        set_state(ids["media"], "media_state", "minimax_h3_media_state",
                  {"version": 1, "mode": "media_list",
                   "items": [{"kind": "image", "source": "upload", "filename": f} for f in media]}, 0)

        prompts = []
        for i, seg in enumerate(segments):
            seed = 1245 + i
            laid = layout(seg["b"], vid, i + 1, warn)
            prompt = self._prompt(seg, laid, i + 1, idx)
            prompts.append(prompt)
            nid = ids["seg"][i]
            set_widget(nid, "prompt", prompt, 0)
            set_widget(nid, "seconds", 5, 1)
            set_widget(nid, "seed", seed, 2)
            set_widget(nid, "control_after_generate", "fixed", 3)
            set_state(nid, "inline_media_state", "minimax_h3_inline_media_state",
                      {"version": 1, "mode": "media_list",
                       "items": [{"kind": "image", "source": "upload", "filename": f}
                                 for f in media]}, 5)
            node = byid[str(nid)]
            node["properties"]["minimax_h3_prompt_reference_doc"] = json.dumps(
                self._ref_doc(i + 1, seg, prompt, idx, laid, seed), ensure_ascii=False)
            nm = named(node)
            nm["subgraph"] = "1125"
            for inp in node["inputs"]:
                if inp.get("name") == "subgraph":
                    inp.setdefault("config", {})["name"] = "1125"

        split_text = "\n".join("【第%d段｜5秒】\n%s" % (i + 1, p) for i, p in enumerate(prompts))
        set_widget(ids["split"], "text", split_text, 2)
        prefix = "video/%s_%s" % (vid, title)
        set_widget(ids["save"], "filename_prefix", prefix, 0)
        set_widget(ids["upscale"], "filename_prefix", prefix, 0)
        return tpl, prompts
