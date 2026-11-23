#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键编排：txt → 蒸馏稿 → 语义 md → 分镜计划 → 1 分钟 JSON → 工作流 JSON → 人读脚本。

    python3 pipeline.py init      --src 小说.txt --work work --name 片名 --template 模板.json
    python3 pipeline.py distill   --work work [--episodes E01] [--workers 2] [--force]
    python3 pipeline.py semantic  --work work [--raw]        # 默认由蒸馏稿投影，不再调模型
    python3 pipeline.py plan      --work work [--videos E01=3]
    python3 pipeline.py assemble  --work work
    python3 pipeline.py workflow  --work work
    python3 pipeline.py render    --work work
    python3 pipeline.py verify    --work work
    python3 pipeline.py run       --work work [--stages all]

模型只负责 distill / plan 两处；semantic 是蒸馏稿的确定性投影，
assemble / workflow / render / verify 全是确定性 Python。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import assemble as A            # noqa: E402
import contract as C            # noqa: E402
import distill as D             # noqa: E402
import prompts as P             # noqa: E402
from llm import Client, JSONError, LLMError, parse_json  # noqa: E402
from workflow import Builder    # noqa: E402

SEG_HEAD = re.compile(r"^#\s*(E\d{2})\s*(.*?)\s+源行\s*R(\d{4})\s*[–\-~]\s*R(\d{4})")
SEG_ERR = re.compile(r"^第(\d+)条视频 第(\d+)段")
AUTO_PAT = ("原文不符", "超长")
DEFAULT_CFG = {
    "name": "未命名",
    "endpoint": os.environ.get("STORYBOARD_ENDPOINT", "http://127.0.0.1:1919/v1"),
    "model": os.environ.get("STORYBOARD_MODEL", "local-model"),
    "api_key": os.environ.get("STORYBOARD_API_KEY", ""),
    "template": "",
    "tables": "tables.py",
    "workers": 3,
    "repair_rounds": 3,
    "timeout": 1200,
    "max_tokens": 16384,
    "temperature": 0.2,
    "wps": 4.5,
    "videos": {},
}


def log_factory(work):
    path = os.path.join(work, "run.log")
    f = open(path, "a", encoding="utf-8")

    def log(msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        f.write(line + "\n")
        f.flush()
    return log


def load_cfg(work):
    path = os.path.join(work, "story.json")
    if not os.path.exists(path):
        raise SystemExit("缺 %s，先跑 `pipeline.py init`" % path)
    cfg = dict(DEFAULT_CFG)
    cfg.update(json.load(open(path, encoding="utf-8")))
    if not os.path.isabs(cfg["tables"]):
        cfg["tables"] = os.path.join(work, cfg["tables"])
    return cfg


def book_of(cfg):
    return C.Book(C.load_tables(cfg["tables"]))


def discover(work):
    out = []
    seg_dir = os.path.join(work, "segments")
    for fn in sorted(os.listdir(seg_dir)):
        if not re.match(r"^seg\d+\.txt$", fn):
            continue
        text = open(os.path.join(seg_dir, fn), encoding="utf-8").read()
        m = SEG_HEAD.match(text.splitlines()[0])
        if not m:
            print("跳过（头部不合规）：%s" % fn)
            continue
        out.append({"ep": m.group(1), "title": m.group(2).strip(),
                    "r_lo": int(m.group(3)), "r_hi": int(m.group(4)),
                    "seg_file": fn, "text": text})
    return out


def suggest_videos(blocks):
    """一条 1 分钟只承载一条主线：按场景块密度定条数，硬上限 4。"""
    return max(1, min(4, int(round(blocks / 9.0)) or 1))


def auto_fixable(e):
    return any(p in e for p in AUTO_PAT)


def split_errors(errors):
    seg_t, other = [], []
    for e in errors:
        m = SEG_ERR.match(e)
        if m:
            seg_t.append((int(m.group(1)), int(m.group(2))))
        else:
            other.append(e)
    return seg_t, other


def excerpt(plan, targets, limit=12):
    out = []
    for vi, i in targets[:limit]:
        try:
            s = plan["videos"][vi - 1]["segments"][i - 1]
        except (IndexError, KeyError, TypeError):
            continue
        out.append({"video": vi, "i": i, "action": s.get("action"),
                    "dialogue": s.get("dialogue")})
    return out


def apply_patches(plan, obj):
    n = 0
    for fx in obj.get("fixes") or []:
        try:
            s = plan["videos"][int(fx["video"]) - 1]["segments"][int(fx["i"]) - 1]
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if "action" in fx and isinstance(fx["action"], str) and fx["action"].strip():
            s["action"] = fx["action"].strip()
        if "dialogue" in fx:
            d = fx["dialogue"]
            s["dialogue"] = None if not isinstance(d, dict) else d
        n += 1
    return n


def normalize_semantic(text):
    """把「语气：狂傲」这类重复标签清掉，语气列只留纯标签（≤8 字）。"""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*-\s+E\d{2}-L\d{3}\s*\|\s*[^|]*\|\s*[^|]*\|\s*)(.*)$", line)
        if m:
            tone = re.sub(r"^(语气|语态|语调)\s*[：:]\s*", "", m.group(2).strip())
            line = m.group(1) + tone[:8]
        out.append(line)
    return "\n".join(out)


def find_sem(work, ep):
    d = os.path.join(work, "semantic")
    if not os.path.isdir(d):
        return None
    cand = [f for f in os.listdir(d) if f.startswith(ep + "_") and f.endswith(".md")]
    return os.path.join(d, sorted(cand)[0]) if cand else None


def find_distill(work, ep):
    d = os.path.join(work, "distill")
    if not os.path.isdir(d):
        return None
    cand = [f for f in os.listdir(d) if f.startswith(ep + "_") and f.endswith(".md")]
    return os.path.join(d, sorted(cand)[0]) if cand else None


def sem_stats(text):
    parsed = C.parse_semantic(text)
    return {"blocks": len(parsed["blocks"]),
            "lines": sum(len(b["lines"]) for b in parsed["blocks"])}


# ---------------- 阶段 0：蒸馏（角色 / 剧情线 / 可拍场景卡） ----------------
def stage_distill(client, book, item, work, rounds, log, force=False):
    ep, title = item["ep"], item["title"]
    old = find_distill(work, ep)
    if old and not force:
        p = D.parse(open(old, encoding="utf-8").read())
        log("%s 蒸馏稿已存在，跳过（--force 可重出）" % ep)
        return {"ep": ep, "title": p["title"], "file": old,
                "blocks": len(p["blocks"]), "lines": sum(len(b["lines"]) for b in p["blocks"]),
                "chars": len(p["roster"]), "errors": [], "seconds": 0, "skipped": True}

    hint = D.chars_hint(os.path.join(work, "distill"), before_ep=ep)
    messages = [
        {"role": "system", "content": P.DISTILL_SYSTEM},
        {"role": "user", "content": P.distill_user(ep, title, item["r_lo"], item["r_hi"],
                                                  item["text"], hint)},
    ]
    t0 = time.time()
    text = client.chat(messages)
    errors, stats = ["未校验"], {}
    want_name = bool(title) and title.startswith("第") and title.endswith("段")
    for r in range(rounds + 1):
        text = D.fix_head(text, ep, title, item["r_lo"], item["r_hi"])
        errors, warns, stats = D.validate(text, ep, item["r_lo"], item["r_hi"],
                                          book.allowed_en, want_name=want_name)
        log("%s 蒸馏第%d版：角色 %s，场景卡 %s，可拍画面 %s，台词 %s，问题 %d 条"
            % (ep, r + 1, stats.get("chars", "?"), stats.get("blocks", "?"),
               stats.get("shots", "?"), stats.get("lines", "?"), len(errors)))
        if not errors:
            break
        log("  ↳ " + "；".join(errors[:4]))
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": P.distill_repair_user(errors)},
        ]
        text = client.chat(messages)

    p = D.parse(text)
    name = "%s_%s.md" % (ep, C.safe_name(p["title"] or title))
    path = os.path.join(work, "distill", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(text.strip() + "\n")
    return {"ep": ep, "title": p["title"], "file": path,
            "blocks": stats.get("blocks", 0), "lines": stats.get("lines", 0),
            "chars": stats.get("chars", 0), "shots": stats.get("shots", 0),
            "errors": errors, "seconds": round(time.time() - t0, 1)}


# ---------------- 阶段 1：语义 md（蒸馏稿投影） ----------------
def stage_semantic(client, book, item, work, rounds, log, force=False, raw=False):
    ep, title = item["ep"], item["title"]
    dpath = None if raw else find_distill(work, ep)
    if dpath:
        dt = D.parse(open(dpath, encoding="utf-8").read())
        if dt["title"]:
            title = dt["title"]
    path = os.path.join(work, "semantic", "%s_%s.md" % (ep, C.safe_name(title)))
    stale = bool(dpath) and os.path.exists(path) and \
        os.path.getmtime(dpath) > os.path.getmtime(path)
    if os.path.exists(path) and not force and not stale:
        parsed = C.parse_semantic(open(path, encoding="utf-8").read())
        log("%s 语义 md 已存在，跳过（--force 可重出）" % ep)
        return {"ep": ep, "title": title, "file": path,
                "blocks": len(parsed["blocks"]),
                "lines": sum(len(b["lines"]) for b in parsed["blocks"]),
                "errors": [], "seconds": 0, "skipped": True}

    if dpath:
        text = D.project(open(dpath, encoding="utf-8").read())
        errors, _warns, stats = C.validate_semantic(text, ep, item["r_lo"], item["r_hi"],
                                                    book.allowed_en)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(normalize_semantic(text))
        log("%s 语义 md 由蒸馏稿投影：%d 块 / %d 台词，问题 %d 条（不调模型）"
            % (ep, stats.get("blocks", 0), stats.get("lines", 0), len(errors)))
        for e in errors[:4]:
            log("  ↳ " + e)
        return {"ep": ep, "title": title, "file": path,
                "blocks": stats.get("blocks", 0), "lines": stats.get("lines", 0),
                "errors": errors, "seconds": 0, "projected": True}

    messages = [
        {"role": "system", "content": P.SEM_SYSTEM},
        {"role": "user", "content": P.semantic_user(ep, title, item["r_lo"], item["r_hi"],
                                                    item["text"])},
    ]
    t0 = time.time()
    text = client.chat(messages)
    errors, stats = ["未校验"], {}
    for r in range(rounds + 1):
        errors, _warns, stats = C.validate_semantic(text, ep, item["r_lo"], item["r_hi"],
                                                    book.allowed_en)
        log("%s 语义第%d版：场景块 %s，台词 %s，问题 %d 条"
            % (ep, r + 1, stats.get("blocks", "?"), stats.get("lines", "?"), len(errors)))
        if not errors:
            break
        log("  ↳ " + "；".join(errors[:4]))
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": P.semantic_repair_user(errors)},
        ]
        text = client.chat(messages)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(normalize_semantic(text.strip()) + "\n")
    return {"ep": ep, "title": title, "file": path, "blocks": stats.get("blocks", 0),
            "lines": stats.get("lines", 0), "errors": errors,
            "seconds": round(time.time() - t0, 1)}


# ---------------- 阶段 2：分镜计划 ----------------
def stage_plan(client, book, item, sem_path, work, rounds, log, want=None, force=False):
    ep, title = item["ep"], item["title"]
    path = os.path.join(work, "plans", "%s_plan.json" % ep)
    if os.path.exists(path) and not force:
        plan = json.load(open(path, encoding="utf-8"))
        log("%s 分镜计划已存在，跳过（--force 可重出）" % ep)
        return {"ep": ep, "file": path, "videos": len(plan.get("videos") or []),
                "errors": [], "warns": [], "fixes": [], "seconds": 0, "skipped": True}

    sem_md = open(sem_path, encoding="utf-8").read()
    parsed = C.parse_semantic(sem_md)
    index = {ln["id"]: ln["text"] for b in parsed["blocks"] for ln in b["lines"]}
    want = int(want) if want else suggest_videos(len(parsed["blocks"]))
    log("%s 计划：%d 个场景块 → 恰好 %d 条 1 分钟视频" % (ep, len(parsed["blocks"]), want))

    messages = [
        {"role": "system", "content": P.PLAN_SYSTEM},
        {"role": "user", "content": P.plan_user(book, ep, title, sem_md, want,
                                                len(parsed["blocks"]), bool(index))},
    ]
    t0 = time.time()
    text = client.chat(messages, thinking=False)
    plan, warns, errors = None, [], ["未校验"]
    for r in range(rounds + 1):
        try:
            plan = parse_json(text)
        except JSONError:
            log("%s 计划第%d版：不是合法 JSON，要求重出" % (ep, r + 1))
            messages = messages + [
                {"role": "assistant", "content": text[:6000]},
                {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，"
                                            "不要围栏、不要解释。"}]
            text = client.chat(messages, thinking=False, temperature=0.0)
            continue
        errors, warns = C.validate_plan(plan, book, ep, index, want)
        log("%s 计划第%d版：视频 %d 条，问题 %d 条"
            % (ep, r + 1, len(plan.get("videos") or []), len(errors)))
        if not errors:
            break
        log("  ↳ " + "；".join(errors[:4]))

        manual = [e for e in errors if not auto_fixable(e)]
        if not manual:
            log("  ↳ 仅剩长度/溯源问题，交给确定性收口，不再重试")
            break
        errors = manual

        seg_t, other = split_errors(errors)
        if r < rounds and seg_t and not other:
            msg2 = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": P.patch_user(errors, excerpt(plan, seg_t))},
            ]
            ptext = client.chat(msg2, thinking=False, temperature=0.0)
            try:
                n = apply_patches(plan, parse_json(ptext))
                log("  ↳ 定点修补 %d 段" % n)
                text = json.dumps(plan, ensure_ascii=False)
                continue
            except JSONError:
                log("  ↳ 修补返回非 JSON，改为整份重出")

        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": P.repair_user(errors)},
        ]
        text = client.chat(messages, thinking=False)

    fixes = C.sanitize_plan(plan, book, index) if plan else []
    err2, warn2 = C.validate_plan(plan, book, ep, index, want) if plan else (["无计划"], [])
    if plan and err2:
        manual2 = [e for e in err2 if not auto_fixable(e)]
        seg_t2, other2 = split_errors(manual2)
        if seg_t2 and not other2:
            try:
                msg2 = messages + [
                    {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                    {"role": "user", "content": P.patch_user(manual2, excerpt(plan, seg_t2))},
                ]
                n = apply_patches(plan, parse_json(
                    client.chat(msg2, thinking=False, temperature=0.0)))
                log("  ↳ 收口修补 %d 段" % n)
                fixes += C.sanitize_plan(plan, book, index)
                err2, warn2 = C.validate_plan(plan, book, ep, index, want)
            except (JSONError, LLMError) as e:
                log("  ↳ 收口修补失败：%s" % str(e)[:80])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return {"ep": ep, "file": path, "videos": len(plan.get("videos") or []),
            "errors": err2, "warns": warns + warn2, "fixes": fixes,
            "seconds": round(time.time() - t0, 1)}


# ---------------- 装配 ----------------
def stage_assemble(book, cfg, work, log):
    d = os.path.join(work, "plans")
    plans, problems = [], []
    for f in sorted(os.listdir(d)):
        if not f.endswith("_plan.json"):
            continue
        plan = json.load(open(os.path.join(d, f), encoding="utf-8"))
        plans.append(plan)
        # 先验后装：复用旧计划时也要过一遍校验，否则侧面描写会直接漏进提示词
        ep = plan.get("episode") or ""
        sem = find_sem(work, ep)
        if sem:
            index = C.sem_index(open(sem, encoding="utf-8").read())
            errs, _ = C.validate_plan(plan, book, ep, index, None)
            if errs:
                problems.append((ep, errs))
                log("  ⚠ %s 计划仍有 %d 处问题：%s" % (ep, len(errs), errs[0]))
    if problems:
        L = ["# 计划残余问题", "",
             "> 装配阶段复检发现的问题。带「侧面描写词」的会在工作流提示词里被念出来，"
             "建议 `pipeline.py plan --episodes … --force` 重出该集。", ""]
        for ep, errs in problems:
            L.append("## %s（%d 处）" % (ep, len(errs)))
            L += ["- %s" % e for e in errs]
            L.append("")
        open(os.path.join(work, "计划问题.md"), "w", encoding="utf-8").write("\n".join(L))

    stats = A.assemble_all(book, plans, os.path.join(work, "1min"), cfg["wps"])
    for s in stats:
        if s.get("error"):
            log("  ✗ %s %s" % (s["id"], s["error"]))
            continue
        log("  %s %s %-8s scene=%-6s 槽%d 台词%d"
            % (s["id"], s["episode"], s["title"], s["scene"], s["slots"], s["dialogue"]))
    return stats


def stage_workflow(book, cfg, work, log):
    builder = Builder(book, cfg["template"])
    src = os.path.join(work, "1min")
    out = os.path.join(work, "workflow")
    prm = os.path.join(work, "prompts")
    os.makedirs(out, exist_ok=True)
    os.makedirs(prm, exist_ok=True)
    stats = []
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".json"):
            continue
        video = json.load(open(os.path.join(src, fn), encoding="utf-8"))
        warn = []
        tpl, prompts = builder.build(video, warn)
        name = "%s_%s.json" % (video["id"], C.safe_name(video["title"]))
        json.dump(tpl, open(os.path.join(out, name), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        open(os.path.join(prm, name.replace(".json", ".txt")), "w", encoding="utf-8").write(
            "# %s %s（60秒，逐段粘贴用）\n\n" % (video["id"], video["title"])
            + "\n\n".join("【第%d段｜5秒】\n%s" % (i + 1, p) for i, p in enumerate(prompts)))
        for w in warn:
            log("  ⚠ " + w)
        stats.append({"id": video["id"], "file": os.path.join(out, name),
                      "nodes": len(tpl["nodes"]), "warns": warn})
    return stats


def cmd_render(work, cfg):
    plans = os.path.join(work, "plans")
    out = os.path.join(work, "分镜脚本.md")
    r = subprocess.run([sys.executable, os.path.join(HERE, "render.py"),
                        "--plans", plans, "--out", out,
                        "--one-min", os.path.join(work, "1min"),
                        "--title", "%s 分镜脚本" % cfg.get("name", "")], check=False)
    return r.returncode


def cmd_verify(work, cfg):
    return subprocess.run([sys.executable, os.path.join(HERE, "verify.py"),
                           "--work", work, "--template", cfg["template"]],
                          check=False).returncode


def cmd_init(args):
    work = os.path.abspath(args.work)
    os.makedirs(work, exist_ok=True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "normalize.py"),
                        "--src", os.path.abspath(args.src), "--workdir", work,
                        "--chunk-chars", str(args.chunk_chars)]
                       + (["--single"] if args.single else [])
                       + (["--episode-re", args.episode_re] if args.episode_re else []),
                       check=False)
    if r.returncode != 0:
        raise SystemExit("规范化失败")

    cfg_path = os.path.join(work, "story.json")
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(cfg_path):
        cfg.update(json.load(open(cfg_path, encoding="utf-8")))
    cfg["name"] = args.name or cfg.get("name")
    if args.template:
        cfg["template"] = os.path.abspath(args.template)
    if args.endpoint:
        cfg["endpoint"] = args.endpoint
    if args.model:
        cfg["model"] = args.model
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    tables = os.path.join(work, cfg["tables"])
    if not os.path.exists(tables):
        src = os.path.join(os.path.dirname(HERE), "assets", "tables_template.py")
        open(tables, "w", encoding="utf-8").write(open(src, encoding="utf-8").read())
        print("\n已放置占位设定表：%s" % tables)
    print("\n下一步（分步骤）：")
    print("  1) `python3 pipeline.py distill --work %s`   # 模型读原文，出角色/剧情线/可拍场景卡" % work)
    print("  2) 看 distill/00_角色总表.md、00_剧情线.md，按角色补 %s" % tables)
    print("  3) `python3 pipeline.py run --work %s`" % work)
    return 0


def cmd_distill(args):
    work = os.path.abspath(args.work)
    cfg = load_cfg(work)
    log = log_factory(work)
    book = book_of(cfg)
    items = discover(work)
    if args.episodes:
        keep = {x.strip() for x in args.episodes.split(",") if x.strip()}
        items = [i for i in items if i["ep"] in keep]
    if not items:
        raise SystemExit("没有可处理的段落")

    log("蒸馏 %s｜%d 段｜模型 %s" % (cfg.get("name"), len(items), cfg["model"]))

    def one(item):
        client = Client(endpoint=cfg["endpoint"], model=cfg["model"],
                        timeout=cfg["timeout"], retries=cfg.get("retries", 2),
                        max_tokens=cfg["max_tokens"], temperature=cfg["temperature"],
                        thinking=False, tag=item["ep"], api_key=cfg.get("api_key", ""))
        return stage_distill(client, book, item, work, cfg["repair_rounds"], log, args.force)

    workers = args.workers or cfg.get("workers", 1)
    recs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, it): it["ep"] for it in items}
        for fu in as_completed(futs):
            try:
                r = fu.result()
                recs.append(r)
                log("%s 完成：角色 %s / 场景卡 %s / 台词 %s%s"
                    % (r["ep"], r.get("chars"), r.get("blocks"), r.get("lines"),
                       "（跳过）" if r.get("skipped") else ""))
            except (LLMError, RuntimeError) as e:
                log("蒸馏失败：%s" % e)

    roster, segs = D.aggregate(os.path.join(work, "distill"))
    log("汇总：角色 %d 个｜段落 %d 个 → distill/00_角色总表.md、00_剧情线.md"
        % (len(roster), len(segs)))
    mine = [r for r in recs if r.get("errors")]
    for r in mine:
        log("  ⚠ %s 仍有 %d 处问题：%s" % (r["ep"], len(r["errors"]), r["errors"][0]))
    return 0


def cmd_run(args):
    work = os.path.abspath(args.work)
    cfg = load_cfg(work)
    log = log_factory(work)
    stages = {"distill", "semantic", "plan", "assemble", "workflow", "render", "verify"} \
        if args.stages == "all" else {x.strip() for x in args.stages.split(",") if x.strip()}

    lock = os.path.join(work, ".lock")
    if os.path.exists(lock) and not args.force:
        age = time.time() - os.path.getmtime(lock)
        if age < 3600:
            raise SystemExit("工作目录被占用（%.0f 分钟前启动）。确认无进程后加 --force" % (age / 60))
    open(lock, "w").write(str(os.getpid()))

    try:
        book = book_of(cfg)
        items = discover(work)
        if args.episodes:
            keep = {x.strip() for x in args.episodes.split(",") if x.strip()}
            items = [i for i in items if i["ep"] in keep]
        if not items:
            raise SystemExit("没有可处理的段落")

        want_map = dict(cfg.get("videos") or {})
        for part in args.videos.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                want_map[k.strip()] = v.strip()

        log("运行 %s｜模型 %s｜端点 %s｜段落 %s｜阶段 %s"
            % (cfg.get("name"), cfg["model"], cfg["endpoint"],
               ",".join(i["ep"] for i in items), ",".join(sorted(stages))))

        sem_recs, plan_recs, distill_recs = [], [], []

        def one(item):
            ep = item["ep"]
            client = Client(endpoint=cfg["endpoint"], model=cfg["model"],
                            timeout=cfg["timeout"], retries=cfg.get("retries", 2),
                            max_tokens=cfg["max_tokens"], temperature=cfg["temperature"],
                            thinking=False, tag=ep, api_key=cfg.get("api_key", ""))
            rec = {"ep": ep}
            sem_path = find_sem(work, ep)
            if "distill" in stages:
                rec["distill"] = stage_distill(client, book, item, work,
                                               cfg["repair_rounds"], log, args.force)
            if "semantic" in stages:
                rec["semantic"] = stage_semantic(client, book, item, work,
                                                 cfg["repair_rounds"], log, args.force,
                                                 raw=getattr(args, "raw", False))
                sem_path = rec["semantic"]["file"]
            if "plan" in stages:
                if not sem_path:
                    raise RuntimeError("%s 缺语义 md，先跑 semantic" % ep)
                rec["plan"] = stage_plan(client, book, item, sem_path, work,
                                         cfg["repair_rounds"], log,
                                         want_map.get(ep), args.force)
            rec["stats"] = client.stats()
            return rec

        if "semantic" in stages or "plan" in stages:
            workers = args.workers or cfg.get("workers", 1)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(one, it): it["ep"] for it in items}
                for fu in as_completed(futs):
                    ep = futs[fu]
                    try:
                        rec = fu.result()
                        if rec.get("distill"):
                            distill_recs.append(rec)
                        if rec.get("semantic"):
                            sem_recs.append(rec)
                        if rec.get("plan"):
                            plan_recs.append(rec)
                        log("%s 完成：蒸馏 %s 卡 / 语义 %s 块 / 计划 %s 条视频"
                            % (ep, rec.get("distill", {}).get("blocks", "-"),
                               rec.get("semantic", {}).get("blocks", "-"),
                               rec.get("plan", {}).get("videos", "-")))
                    except (LLMError, RuntimeError) as e:
                        log("%s 失败：%s" % (ep, e))

        if "distill" in stages:
            roster, segs = D.aggregate(os.path.join(work, "distill"))
            log("蒸馏汇总：角色 %d 个｜段落 %d 个 → distill/00_角色总表.md、00_剧情线.md"
                % (len(roster), len(segs)))

        if "assemble" in stages:
            log("装配 1 分钟 JSON")
            stage_assemble(book, cfg, work, log)
        if "workflow" in stages:
            log("生成工作流 JSON")
            stage_workflow(book, cfg, work, log)
        if "render" in stages:
            log("生成人读分镜脚本")
            cmd_render(work, cfg)
        if "verify" in stages:
            log("验收")
            cmd_verify(work, cfg)

        summary = {"name": cfg.get("name"), "model": cfg["model"],
                   "endpoint": cfg["endpoint"], "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "episodes": [i["ep"] for i in items], "stages": sorted(stages),
                   "distill": [r.get("distill") for r in distill_recs],
                   "semantic": [r.get("semantic") for r in sem_recs],
                   "plans": [r.get("plan") for r in plan_recs],
                   "warnings": [w for r in plan_recs for w in (r.get("plan", {}).get("warns") or [])]}
        json.dump(summary, open(os.path.join(work, "run.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log("完成。产物目录：%s" % work)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="规范化源文并写入 story.json")
    p.add_argument("--src", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--template", default="")
    p.add_argument("--endpoint", default="")
    p.add_argument("--model", default="")
    p.add_argument("--episode-re", default="")
    p.add_argument("--chunk-chars", type=int, default=2000,
                   help="每段目标字数，满额后在换行处收尾（默认 2000）")
    p.add_argument("--single", action="store_true")

    for name, h in (("run", "全链路（可 --stages 选段）"), ("distill", "蒸馏：原文→角色/剧情线/场景卡"),
                    ("semantic", "语义 md（默认由蒸馏稿投影）"), ("plan", "只做分镜计划"),
                    ("assemble", "计划→1分钟JSON"),
                    ("workflow", "1分钟→工作流JSON"), ("render", "计划→人读脚本"),
                    ("verify", "三层验收")):
        q = sub.add_parser(name, help=h)
        q.add_argument("--work", required=True)
        if name in ("run",):
            q.add_argument("--stages", default="all")
            q.add_argument("--episodes", default="")
            q.add_argument("--videos", default="")
            q.add_argument("--workers", type=int, default=0)
            q.add_argument("--force", action="store_true")
            q.add_argument("--raw", action="store_true", help="语义阶段直接读原文，不用蒸馏稿")
        if name in ("distill", "semantic", "plan"):
            q.add_argument("--episodes", default="")
            q.add_argument("--workers", type=int, default=0)
            q.add_argument("--videos", default="")
            q.add_argument("--force", action="store_true")
        if name == "semantic":
            q.add_argument("--raw", action="store_true", help="直接读原文调模型，不用蒸馏稿")

    args = ap.parse_args()
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "distill":
        return cmd_distill(args)

    work = os.path.abspath(args.work)
    cfg = load_cfg(work)
    log = log_factory(work)

    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "verify":
        return cmd_verify(work, cfg)
    if args.cmd == "render":
        return cmd_render(work, cfg)

    if args.cmd in ("semantic", "plan"):
        book = book_of(cfg)
        items = discover(work)
        if args.episodes:
            keep = {x.strip() for x in args.episodes.split(",") if x.strip()}
            items = [i for i in items if i["ep"] in keep]
        want_map = dict(cfg.get("videos") or {})
        for part in args.videos.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                want_map[k.strip()] = v.strip()

        def one(item):
            ep = item["ep"]
            client = Client(endpoint=cfg["endpoint"], model=cfg["model"],
                            timeout=cfg["timeout"], retries=cfg.get("retries", 2),
                            max_tokens=cfg["max_tokens"], temperature=cfg["temperature"],
                            thinking=False, tag=ep, api_key=cfg.get("api_key", ""))
            if args.cmd == "semantic":
                return stage_semantic(client, book, item, work, cfg["repair_rounds"],
                                      log, args.force, raw=args.raw)
            sem_path = find_sem(work, ep)
            if not sem_path:
                raise RuntimeError("%s 缺语义 md" % ep)
            return stage_plan(client, book, item, sem_path, work, cfg["repair_rounds"],
                              log, want_map.get(ep), args.force)

        workers = args.workers or cfg.get("workers", 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, it): it["ep"] for it in items}
            for fu in as_completed(futs):
                try:
                    r = fu.result()
                    log("%s 完成：%s" % (r["ep"], r.get("file")))
                except (LLMError, RuntimeError) as e:
                    log("失败：%s" % e)
        return 0

    book = book_of(cfg)
    if args.cmd == "assemble":
        stage_assemble(book, cfg, work, log)
    elif args.cmd == "workflow":
        stage_workflow(book, cfg, work, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
