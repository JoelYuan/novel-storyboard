# novel-storyboard

把小说 txt 稳定转成可直接运行的 **MiniMax H3 / AICG3D 60 秒视频工作流 JSON**。

分工原则：**模型只做语义文员，程序只管格式与装配**。本地模型（任何 OpenAI 兼容端点，如 LM Studio / vLLM）不需要强大的指令遵循能力——所有编号、字段、上限、槽位约束都由 Python 契约层校验并回喂修正，模型崩了就定点重写。

```
小说 txt → ①蒸馏(模型) → ②语义 md(程序投影,不调模型) → ③分镜计划(模型) → ④1min/工作流 JSON(程序)
```

## 四个阶段

| 阶段 | 执行者 | 输入 → 输出 | 模型自由度 |
|---|---|---|---|
| ① distill | 模型 | 原文（R#### 行号绑定）→ 蒸馏稿：角色表/剧情线/场景卡/可拍画面/逐字台词 | 只能认人、提炼、抄台词 |
| ② semantic | 程序 | 蒸馏稿 → 语义场景块 md（确定性投影） | 无 |
| ③ plan | 模型 | 语义 md + 设定表 → 1 分钟分镜计划 JSON | 只能选主线、排 12 段 |
| ④ assemble | 程序 | 计划 → `1min/*.json` → `workflow/*.json` → 人读脚本 | 无 |

程序侧全程校验（`contract.py` / `distill.py` / `verify.py`）：编号连续、台词溯源（逐字引用保留 L 号，改写补 C 号）、槽位上限（1 场景 + ≤5 人物道具）、哑角、侧面描写禁用词、拍点时长（12 段 × 5.0 秒）。

## 快速开始

依赖：Python 3.10+（纯标准库）+ 任意 OpenAI 兼容聊天端点。

```bash
S=src/novel_storyboard

# 0. 初始化：探测编码、2000 字/段切段、绑定 R#### 行号
python3 $S/pipeline.py init --src demo/novel.txt --work work \
  --name 片名 --template demo/template_aicg3d_minimax_h3_60s.json

# 1. 蒸馏（模型）
python3 $S/pipeline.py distill --work work --workers 3

# 2. 语义 md（程序投影，秒级）
python3 $S/distill.py project --work work/distill --out work/semantic

# 2.5 设定表：自动汇总 → 手工补描述/音色/哑角
python3 $S/make_tables.py --semantic work/semantic --out work/tables.py
#   然后编辑 work/tables.py：SCENES 光照、CHARS 外貌、VOICES 音色、MUTE、别名

# 3. 分镜计划（模型）
python3 $S/pipeline.py plan --work work --workers 3

# 4. 装配 + 工作流 + 人读脚本 + 三层验收（程序）
python3 $S/pipeline.py assemble --work work
python3 $S/pipeline.py workflow --work work
python3 $S/pipeline.py render  --work work
python3 $S/pipeline.py verify  --work work

# 或一键全链路
python3 $S/pipeline.py run --work work --stages all
```

端点与模型写在 `work/story.json`（`endpoint` / `model` / `api_key` / `workers` / `repair_rounds` 等）。

## demo/

《叶小钗传奇》一篇 7 万字小说的完整实测产物：

| 目录 | 内容 |
|---|---|
| `demo/novel.txt` | 源小说（GB18030 编码，程序自动探测） |
| `demo/tables.py` | 合并设定表：20 场景 / 33 人物 / 4 道具 / 33 音色，哑角与别名归并 |
| `demo/output/distill/` | 16 段蒸馏稿 + 全片角色总表 / 剧情线 |
| `demo/output/semantic/` | 投影语义 md |
| `demo/output/plans/` | 16 份分镜计划 JSON |
| `demo/output/1min/` | 16 条 1 分钟分镜 JSON |
| `demo/output/workflow/` | 16 条可直接灌入 AICG3D 的工作流 JSON |
| `demo/output/分镜脚本.md` | 人读版全片分镜 |
| `demo/output/校验报告.md` | 三层验收：16/16 通过，0 问题 0 告警 |

实测环境：本地 Qwen 端点（约 60 tok/s，3 并发），蒸馏 + 计划全 16 段约 25 分钟。

## 模板说明

`demo/template_aicg3d_minimax_h3_60s.json` 是一个已验证可运行的 AICG3D 工作流模板（21 节点 / 12 段）。灌入程序**按节点 type 定位，不写死 id**——换成你自己的模板只需保证：段节点为 12 个 `AICG3D_H3SequenceSegment`，另有 Global / Voice / Media / FinalSequenceUpscale 各一。

## 文档

- [docs/01_格式契约.md](docs/01_格式契约.md) — 四层产物的全部硬契约
- [docs/02_提示词与修补.md](docs/02_提示词与修补.md) — 提示词设计与三档回喂修补
- [docs/03_硬约束与坑.md](docs/03_硬约束与坑.md) — 实测踩过的坑（串脸、超发、侧面描写…）
- [docs/04_设定表与参考图.md](docs/04_设定表与参考图.md) — 设定表字段与参考图槽位

## 版权声明

- 本仓库**代码**以 MIT 许可发布。
- `demo/` 中的小说文本、蒸馏稿、台词及分镜产物来自霹雳国际多媒体《叶小钗传奇》，版权归原权利方所有，**仅作流水线功能演示，请勿二次分发或商用**。发布到公开平台前建议将 `demo/novel.txt` 与 `demo/output/distill|semantic|plans|1min|workflow|prompts` 替换为你自有的文本。
