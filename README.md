# novel-transdb —— 适配 Antigravity 的长篇小说翻译小工具

以本地 **SQLite** 为唯一中枢的长篇小说翻译流水线：一次入库，之后机翻、精修、
术语统一、质检、导出全部在段落级进行，彻底告别「几十万字大 txt 反复拆分 / 拼接 / 对不齐」。

**为什么存在**：给 Antigravity（反重力）+ Gemini Flash 这类智能体当翻译后端——
**智能体只输出译文本身**，进度、状态、上下文、术语、质检全部住在 SQLite 里：
`next` 按滑动窗口精准投喂原文与机翻稿，`apply` 落盘 JSON 原子写回。
**不再用 Python 脚本反复重写 JSON / txt 中间文件**，把流程 token 消耗压到最低。

为零依赖设计：只需要 **Python 3.8+ 标准库**（`sqlite3` 内置），拷贝一个 `transdb.py` 即可用。
专门为配合智能体（**Antigravity + Gemini 3.8 Flash** 等）批量处理而设计：所有数据结果以
JSON 输出到 stdout，日志走 stderr，智能体可以直接解析。

---

## 目录

1. [为什么用 SQLite 而不是 txt](#1-为什么用-sqlite-而不是-txt)
2. [安装与环境](#2-安装与环境)
3. [30 秒上手（跟 demo 走一遍）](#3-30-秒-上手跟-demo-走一遍)
4. [核心概念：书目 / 段落 / 状态机 / 标记](#4-核心概念)
5. [命令手册](#5-命令手册)
6. [四阶段完整工作流](#6-四阶段完整工作流)
7. [在 Antigravity 中配合 Gemini 3.8 Flash 使用](#7-在-antigravity-中配合-gemini-38-flash-使用)
8. [人名术语统一：夏娃 → 伊芙 实战](#8-人名术语统一夏娃--伊芙-实战)
9. [数据安全与备份](#9-数据安全与备份)
10. [常见问题 FAQ](#10-常见问题-faq)
11. [命令速查表](#11-命令速查表)

---

## 1. 为什么用 SQLite 而不是 txt

| 痛点（txt 方案） | transdb 方案 |
|---|---|
| 每处理一段都要读写整个几十万字文件，反复 `split`/`join` | 一次入库，按段 `UPDATE`，毫秒级 |
| 段落索引靠正则切分，遇到段内空行、`\r\n` 就错位、吞段 | `para_index` 永久固定，段落 **1:1 对齐** |
| 断点续传要额外写进度文件，中断后从头扫描 | 状态就在每段上，`next` 直接从断点取 |
| 全篇排查「夏娃 vs 伊芙」要整本遍历 | 一条带条件的 `replace` / `search` 秒级完成 |
| 机翻腔词（手袋、兜帽）人工逐段找 | `qc` 自动扫描并打标记 |
| 最终拼接输出容易段落漂移 | `export` 按序导出，缺译段显式标记，绝不静默丢段 |

## 2. 安装与环境

- 唯一要求：Python 3.8+（Windows / macOS / Linux 均可，无任何第三方依赖）。
- 把 `transdb.py` 放进工作目录即可。验证安装：

```bash
python transdb.py selftest
```

会跑一遍端到端自测（临时目录里建库 → 入库 → 双语解析 → 写回 → 替换 → 术语/质检 →
译注 → 警惕库门禁/hints/病灶重修 → 错题池入池/提炼转正 → 对齐回写 → 导出），
输出 `73 项通过` 即环境就绪，不碰任何真实数据。

**Windows 控制台提示**：如果中文输出乱码，先执行 `chcp 65001`，或直接用 Windows Terminal /
PowerShell 7。文件读写一律 UTF-8，不受控制台影响。若要在 Windows PowerShell 5.1 里用变量
捕获命令输出再二次处理，先执行 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`，
否则捕获到的 UTF-8 JSON 会乱（PS 7 / Windows Terminal 无需处理）。

**数据库文件**：默认在当前目录生成 `translations.db`。可用 `--db 路径` 或环境变量
`TRANSDB_DB` 指定（`--db` 既可以放在子命令前，也可以放在子命令后）。

## 3. 30 秒上手（跟 demo 走一遍）

在 `demo/` 目录里执行（Windows 把 `python` 换成 `py` 亦可）：

```bash
cd demo
python ../transdb.py --db demo.db import sample_en.txt --book demo   # ① 原文入库
python ../transdb.py --db demo.db stats demo                         # ② 看进度
python ../transdb.py --db demo.db next demo --n 5                    # ③ 取 5 段待译（JSON）
python ../transdb.py --db demo.db apply updates_example.json         # ④ 译文写回（状态自动推进）
python ../transdb.py --db demo.db export demo --out 成品.md          # ⑤ 导出（自动回退机翻稿）
```

① 的输出：

```json
{
  "book_id": "demo",
  "paragraphs": 9,
  "headings": 2,
  "dropped_page_numbers": 1,
  "encoding": "utf-8",
  "split_mode": "blank"
}
```

④ 之后 `stats` 会显示 `translated 5 / pending 4`——这就是断点续传的全部原理：
**状态在每一段上，中断了重发一句「继续翻译」即可，工具自动从未完成处接着取。**

## 4. 核心概念

### 数据模型

```
books（书目）        book_id、段数、切分方式、编码、质检词表
paragraphs（段落）   para_index（唯一排序键）、en_text、machine_zh、refined_zh、
                     notes（译注，独立字段）、status、error_flags、claimed_at、updated_at
glossary（术语表）   book_id + en → zh（如 Eve → 伊芙）+ 禁译词表 forbidden_zh
rules（警惕库）      全局('*')或单书的译前提示/译后拦截规则：en_pattern 触发 + bad_zh 禁译
                     + good_zh 建议 + severity（block/warn/hint）+ hit_count 命中计数
issue_staging(错题池) 翻译/精修途中顺手记的原始错例（原文/旧译文/新译文三份全文快照
                     + 触发词 + 病因），事后批量提炼为 rules 规则（v1.5）
```

`para_index` 从 0 开始、永久不变。任何时刻「第 N 段」都指同一段文字，绝无漂移。

### 状态机（全自动推进，无需手工维护）

```
pending ──写入 machine_zh──▶ translated ──写入 refined_zh──▶ refined
（待处理）                    （已机翻）                      （已精修）
   ▲                                                            │
   └────────── set-status 重置重做 / release 释放卡死 ◀──────────┘

wip    并行处理时 next --claim 的临时占用状态
empty  空段（极少出现）
```

`apply` 写入译文时自动推进状态（写入精修稿自动变 `refined`），智能体不需要、
也不能忘记更新状态。要整段重做时用 `set-status` 显式重置。

### error_flags（质检标记）

`qc` 与 `glossary check` 把问题写进 `error_flags`（逗号分隔），供排查：

| 标记 | 含义 |
|---|---|
| `too_short` / `too_long` | 译文/原文长度比异常（默认 <0.15 或 >3.0，疑似漏译/膨胀） |
| `no_chinese` | 译文里一个汉字都没有 |
| `empty_target` | 状态是已译但译文为空 |
| `truncated` | 句末无法定标点（。！？……”等）——疑似模型输出中途被掐断 |
| `unclosed_quote` | 引号不成对（“”‘’「」『』及英文双引号） |
| `badword:手袋` | 命中机翻腔/危险词表 |
| `webnovel_cliche:词` | 命中俗套词表（`qc --cliche` 启用；词表来自 transdb_seeds.json，不内置） |
| `name_mismatch` | 原文含术语（如 Eve，整词匹配）但译文没有对应译名（如 伊芙） |
| `forbidden_zh:Eve=夏娃` | 译文出现了该术语禁译词表里的译法（双向核验） |
| `rule_trap:N` | 警惕库双语消歧规则命中（如 bank 译成银行）——`next --flagged` 看规则卡 |
| `rule_badword:N` | 警惕库纯中文禁词规则命中 |
| `rule_syntax:N` | 警惕库句式反模式命中（"被…所…"等欧化翻译腔） |

标记是**可自愈**的：段落修好后重新 `apply`（自动清 rule_*）或重跑 `qc`
（自动清 qc 自有标记），不用手工清零。v1.4 起 `badword:*` 也随每次 qc 重算。

### 输出约定（给智能体解析用）

- **stdout = 数据（JSON）**：`next` / `apply` / `search` / `show` / `export` 等的结果。
- **stderr = 人类日志**：`[transdb] ...` 前缀的进度信息。

## 5. 命令手册

以下 `BOOK` 均指书目 ID；`--db` 全局可选，不写则用默认库。

### 入库与总览

```bash
# 导入原文：utf-8 / gbk(gb18030) / utf-16 自动识别；空行分段，英文硬换行自动合并
python transdb.py import 原文.txt --book novel_p1

# 每行一段的 txt（网文抓取常见格式）
python transdb.py import 原文.txt --book novel_p1 --split line

# 双语对照稿（英文段 + 谷歌机翻中文段交替，如 *-zh-CN-dual.txt）
python transdb.py import 双语稿.txt --book novel_p2 --dual
#   → 块级解析：空行分块，块内按「连续同语言行」切分（英文在前中文在后），
#     同时兼容三种底本格式——英中同块成对（对间空行）、英中各占一段（段间空行）、
#     全文无空行逐行交替；export --bilingual 的产物可直接回灌
#   → 中文段直接入 machine_zh、状态 translated（跳过整个机翻阶段！）
#   → 原稿漏译的英文段自动识别为 pending，入库报告给出数量
#   → 漏译处不会级联错位；一段英文的译文被拆成多个中文段时自动并回；
#     入库后留意 suspect_en / suspect_zh 警告（切分抽查提示）

# 其他选项
#   --no-join        空行模式下不合并段内换行（诗歌/诗体文本用）
#   --keep-numbers   保留纯数字行（默认视为页码丢弃）
#   --force          书目已存在时推翻重导（删除该书全部段落与译文，术语表保留）

python transdb.py books          # 列出所有书目与进度
python transdb.py stats BOOK     # 单书状态统计（--json 给机器读）
```

标题（`Chapter 1`、`第三章`、`Epilogue`、全大写短行）导入时自动打 `is_heading` 标记，
质检时长宽比检查自动豁免，导出时可选择加 `## ` 前缀。

### 取批次与写回（翻译主循环）

`next` 取的是**滑动窗口结构化批次**——不是裸段落，自带上下文：

```bash
# 机翻阶段：取批次（默认每批最多 20 段，附带前 2 段上下文 + 后 1 段开头）
python transdb.py next BOOK --n 20

# 按字数自动划分批次（推荐给上下文预算有限的模型）：
# 段落累计字符数超过 6000 就截断，与 --n 同时生效、先到为准
python transdb.py next BOOK --n 20 --max-chars 6000

# 一次取 3 个批次（并行会话/提前规划用）
python transdb.py next BOOK --chunks 3 --max-chars 4000

# 精修阶段：取已机翻的段（批次自带机翻稿）
python transdb.py next BOOK --status translated --n 10

# 取带质检标记的病灶段（不限状态；target_chunk 带 error_flags，
# 批次带 flag_details 规则卡——句式/错译微创重修专用，与 --status 互斥）
python transdb.py next BOOK --flagged --n 10

# 手工指定这一批就是某几段 / 某区间（忽略状态过滤）
python transdb.py next BOOK --ids 101,102,105
python transdb.py next BOOK --from 101 --to 120

# 关闭/调整滑动窗口上下文
python transdb.py next BOOK --before 3 --after 2      # 前 3 段 + 后 2 段开头
python transdb.py next BOOK --before 0 --after 0      # 不要上下文

# 并行处理时防止两个会话拿到同一段（独占事务原子抢占，绝不重复取段）：
python transdb.py next BOOK --n 20 --claim        # 批次同时标为 wip
python transdb.py release BOOK --older-min 30     # 释放卡死超 30 分钟的 wip
#   release 按已有译稿智能恢复：有 refined_zh 回 refined，有 machine_zh 回
#   translated，都没有才回 pending——已机翻的段绝不降级重翻
```

`next` 的输出结构（`context_before` / `context_after` **仅供参考理解，不要翻译、不要写回**）：

```json
{
  "book_id": "demo", "status": "translated", "claimed": false, "remaining": 5,
  "batches": [
    {
      "ids": [2],
      "chars": 136,
      "context_before": [
        { "id": 0, "en": "Chapter 1", "zh": "第一章" },
        { "id": 1, "en": "The Rain",  "zh": "雨" }
      ],
      "target_chunk": [
        { "id": 2, "is_heading": false, "status": "translated",
          "en": "It was a cold morning on the island. Eve wrapped ...",
          "machine_zh": "岛上的清晨很冷。夏娃把围巾裹得更紧……" }
      ],
      "context_after": [
        { "id": 3, "en": "She carried a small handbag and a bottle of brandy, ..." }
      ]
    }
  ]
}
```

- `context_before`：本批之前 N 段（`--before`，默认 2）的原文，**已译过的会附上 zh**，
  供人名、语气衔接参考。
- `context_after`：本批之后 N 段（`--after`，默认 1）的**开头 300 字符**，用于判断时态
  和未完结句意。
- `target_chunk`：真正要翻译的段。`id` 就是 `para_index`。精修阶段会带 `machine_zh`；
  带质检标记的段附 `error_flags`。
- `hints`：**译前动态提示**（v1.4）——警惕库里 `en_pattern` 命中本批原文的规则卡
  （`forbid_zh` 禁用译法 / `suggest_zh` 推荐方向 / `matched_ids` 命中段号），只扫
  target_chunk 不扫上下文，每批上限 10 条。翻译时严禁使用 `forbid_zh` 的译法。
- `--max-chars` 打包规则：从断点开始按序累加，加下一段会超预算就截断；单个超长段落
  独占一批。字符数 = 原文 + 机翻稿（若有）。

写回用 `apply`（原子事务，全部成功或全部回滚，状态自动推进）：

```bash
python transdb.py apply updates.json
```

`updates.json` 格式（`apply` 也接受顶层数组；`-` 表示从 stdin 读；条目里 `id` 与
`para_index` 等价，直接用 `next` 返回的 `id` 即可）：

```json
{
  "book_id": "demo",
  "updates": [
    { "id": 0, "machine_zh": "第一章" },
    { "id": 2, "machine_zh": "岛上的清晨很冷。伊芙把围巾裹得更紧……" },
    { "id": 5, "refined_zh": "精修后的白描译文……" },
    { "id": 7, "machine_zh": "……", "error_flags": "" }
  ]
}
```

- 可写字段：`machine_zh` / `refined_zh` / `notes` / `status` / `error_flags`；传空串 `""` 表示清空。
- **错题字段**（v1.5，可选）：条目可顺手带 `issue_cat`（trap|badword|syntax|style，
  缺省 trap）、`issue_en`（英文触发词）、`issue_note`（一句话病因）——写回成功时
  系统自动把该段原文与旧译文快照一起存入错题池（返回 `staged` 计数），供事后
  批量提炼为警惕库规则。被 block 拦截的段、未写译文的条目不入池。
- `notes` 是**译注**（术语释义、文化背景）：存独立字段，不污染正文、永不参与质检扫描、
  不推进状态；`next` 批次会带出已有 notes 供精修会话参考。导出形态见 `export --notes`。
- 非法的 `para_index` 默认整体报错并列出，`--allow-partial` 可跳过坏条目只写好的。
- **写回门禁**（v1.4）：`apply` 默认对写入译文的段跑警惕库规则校验——
  - 命中 `block` 级：该段**完全不写库**，返回 JSON 里 `blocked` 计数 + `violations`
    明细（含 `suggest_zh` 修改方向），智能体当场返工再 apply 即可（退出码仍为 0，
    逐条结果看 JSON）；确认规则误伤时 `--force` 强制写入（仍打 `rule_*` 标记留痕）；
  - 命中 `warn` 级（如欧化被动句式）：正常写入并自动打 `rule_syntax:N` 标记，
    返回 `warnings` 提示；
  - 写回正确译文时自动清除该段的 `rule_*` 旧标记（幂等自愈）；
  - `--no-gate` 跳过全部校验（不拦、不打标、不清标）。
- 为什么用 JSON 文件而不是命令行参数：Windows 命令行传几千字中文有编码和长度风险，
  落成文件再 `apply` 是对智能体最稳的路径。`-`（stdin）只接受 UTF-8 字节流，
  PS 5.1 管道会损坏中文——Windows 上一律推荐文件方式。

### 查看与检索

```bash
python transdb.py show BOOK --index 42                       # 看某段全文（原文+机翻+精修+状态）
python transdb.py show BOOK --from 40 --to 50                # 看区间（翻译时带上下文）
python transdb.py show BOOK --index 42 --fields refined      # 只看精修稿

python transdb.py search BOOK --zh 手袋                      # 搜译文（机翻+精修都搜）
python transdb.py search BOOK --zh 手袋 --field refined      # 只搜精修稿
python transdb.py search BOOK --en Eve                       # 搜原文（不分大小写）
python transdb.py search BOOK --en Eve --zh 伊芙             # 组合：原文有 Eve 但译文用错译名的段
python transdb.py search BOOK --zh 手袋 --full               # 不截断返回全文
```

### 全局安全替换（`replace`）

```bash
# 把译文里的「夏娃」改成「伊芙」，但只动原文确实含 Eve 的段落——绝不误伤
python transdb.py replace BOOK --find 夏娃 --replace 伊芙 --if-en Eve --dry-run   # 先预览
python transdb.py replace BOOK --find 夏娃 --replace 伊芙 --if-en Eve             # 确认后执行

# 选项：
#   --field refined|machine|both   作用字段，默认 refined（精修稿=最终成品，最安全）
#                                  机翻阶段统一人名请加 --field machine 或 both
#   --if-en 词1,词2                 仅当原文包含任一英文词（整词匹配：Eve 不吃
#                                  evening/seven/Never）才替换
#   --regex                         把 --find 当正则
#   --dry-run                       只预览不写库（强烈建议先跑）
#   --log-issue [备注]              替换执行时把首个命中段采样入错题池（v1.5；
#                                  dry-run 不写库也不入池）
```

### 质检（`qc`）

```bash
# 内置检查：长度比异常 / 无汉字 / 已译但为空 / 句末截断 / 引号未闭合；外加词表
python transdb.py qc BOOK --badwords "手袋,兜帽,精液"          # 检查并写入 error_flags
python transdb.py qc BOOK --badwords-file badwords.txt --save  # 词表存进书目配置，以后免传
python transdb.py qc BOOK                                       # 用已保存的词表复查
python transdb.py qc BOOK --cliche                              # 追加俗套词扫描（词表来自 transdb_seeds.json，工具不内置）
python transdb.py qc BOOK --rules-only                          # 仅跑警惕库规则扫描
python transdb.py qc BOOK --dry-run                            # 只看不写
python transdb.py qc BOOK --min-ratio 0.15 --max-ratio 3.0     # 自定义长度比阈值
python transdb.py qc BOOK --clear                              # 清除该书全部标记
```

v1.4 起 `qc` **默认并入警惕库（rules 表）全书扫描**——命中打 `rule_trap:N` /
`rule_badword:N` / `rule_syntax:N` 标记，rows 里附 `rule_hits`（含命中片段与改写
示范）。这也是 `import --dual`（自带译文直入）和 `sync-md`（外部回写）这类
不走 apply 门禁的旁路写入的全量兜底。修好的段重跑 qc 标记自动清除。

`truncated`（句末无法定标点）专抓大模型输出中途被掐断的段——这类段长度比往往
正常，纯字数检查拦不住；`unclosed_quote` 抓引号没闭合的段。两者都是提示性标记，
按 `show` 原文核对后修补即可。质检只扫译文正文（`refined_zh`/`machine_zh`），
**译注 `notes` 永不参与扫描**——合法的专业释义不会再被当成机翻杂质误报。

### 术语表（`glossary`）

```bash
python transdb.py glossary add BOOK Eve 伊芙 --note 女主
# 登记禁译词表（可选）：原文含该术语的段落，译文里不允许出现这些中文
python transdb.py glossary add BOOK Eve 伊芙 --forbidden 夏娃,前夕,平安夜
python transdb.py glossary load BOOK terms.tsv        # 批量：英文<TAB>中文[<TAB>禁译词表]，# 注释
python transdb.py glossary list BOOK
python transdb.py glossary remove BOOK Eve
python transdb.py glossary check BOOK                 # 全文一致性检查（双向核验）
python transdb.py glossary check BOOK --dry-run       # 只看报告不写标记
```

`glossary check` 双向核验（英文侧为**整词正则匹配**，`Eve` 不会误伤
`evening/seven/Never`；术语本身含大写则区分大小写，专有名词 `Prince` 不吃
普通词 `prince`）：

- **missing**（标 `name_mismatch`）：原文含术语 EN、已有译文，但译文缺少对应
  ZH——揪出「夏娃 vs 伊芙」混用的利器；
- **forbidden**（标 `forbidden_zh:EN=词`）：原文含术语 EN，译文却出现了它的
  **禁译词**（如绑 Eve 时译文里出现「夏娃」）——反方向的安全网。

### 警惕库（`rule`，v1.4）

与 glossary 的分工：**全书固定译名进 glossary（Eve→伊芙），语境歧义进 rule**
（同一个英文表达在不同语境译法不同，如 bank、suit）。规则在 `next`
取批时按原文命中挂载为译前 `hints`，在 `apply` 写回时做实时门禁，在 `qc`
做全书兜底扫描——三道关卡共用一张 `rules` 表，积累一次全书目复用。

```bash
python transdb.py rule init-defaults                        # 从本地 transdb_seeds.json 导入种子规则（幂等，新库先跑这个）
python transdb.py rule list [--book BOOK] [--cat trap] [--top 20]   # 查看（含 hit_count）

# 沉淀一条双语消歧卡（默认全局 '*'；PS 里正则一律用单引号包裹防转义）
python transdb.py rule add --cat trap --en '\bbank\b' --bad '银行' \
  --good '河岸/堤岸' --note '河岸语境严禁译成银行' --severity warn

# 句式反模式（纯中文正则，无英文门控）
python transdb.py rule add --cat syntax --bad '被[^，。！？]{1,10}所[一-鿿]{1,4}' \
  --good '改为主动句' --note '消除欧化被动' --severity warn

python transdb.py rule remove --id 12                       # 删除
python transdb.py rule load rules_seed.json                 # 批量导入（按去重键合并，任一非法整批拒绝）
python transdb.py rule dump --out rules_backup.json         # 备份导出
```

- 四类规则：`trap`（双语消歧，需 `--en`+`--bad`）、`badword`（纯中文禁词，仅 `--bad`）、
  `syntax`（句式反模式，仅 `--bad`）、`style`（黄金范例，仅 `--good`，只挂载不拦截）；
- **工具不内置任何规则/词表数据**（v1.5.1 起）：种子经 `transdb_seeds.json` 加载
  （`{"rules": [...], "cliches": [...]}`，复制 `transdb_seeds.example.json` 起步，
  `TRANSDB_SEEDS` 环境变量可指定路径），该文件属你的本地积累、已默认 gitignore；
- `--severity`：`block`=apply 写回拦截、`warn`=写入打标、`hint`=仅译前挂载；
  **CLI 默认 warn**——新规则先观察 `hit_count` 与误报，确认无误再升 block；
- 去重键 `(book_id, category, en_pattern, bad_zh)`：重复 `rule add` 是**更新**
  （good/note/severity，保留 hit_count），这也是收窄误伤规则、升降级的正规途径；
- 正则入库前编译校验，非法直接拒绝；库内坏正则（如 sql 手工塞的）加载时跳过
  并警告，绝不弄挂 next/apply/qc；
- `hit_count` 只在译后命中（apply 拦截/警告、qc 打标）时递增，译前挂载不计数；
  `rule list --top` 找最活跃规则，配合 `qc --rules-only --dry-run` 抽查复核。

### 错题池（`issue`，v1.5）

翻译/精修途中**不写正则**，顺手记原始错例；事后由人类触发复盘，批量提炼为
正式规则。双层漏斗让「翻译专注」与「规则提炼」解耦到不同会话。

```bash
# 复盘：看待提炼的错题（默认截句展示 + 分页，--full 看全文快照）
python transdb.py issue list [--book BOOK] [--status open|promoted|ignored|all] \
                             [--cat trap|syntax|style|badword] [--limit 30] [--full]

# 显式登记（自动抓取该段原文与当前译文作对照，无需手工复制长文本）
python transdb.py issue add BOOK --index 102 --cat syntax \
  --en "persuaded" --good "听了她的话，他同意了。" --note "被…所说服 欧化被动"

# 批量转正为正式规则（复用 rule add 校验；cat/note 默认继承所选错题）
python transdb.py issue promote --id 15,16,18 \
  --en '\bbank\b' \
  --bad '银行' --good '河岸/堤岸' --severity warn

# 忽略偶发/无需入库的条目
python transdb.py issue ignore --id 16,17
```

三种入池方式（都不打断翻译主流程）：

- **方式 A（主通道）**：`apply` 条目顺手带 `issue_cat`/`issue_en`/`issue_note`
  三个短字段——系统自动存该段原文 + 被替换的旧译文 + 新译文三份**全文快照**
  （写入即冻结，段落后续再改不影响证据）；
- **方式 B**：`replace --log-issue "备注"` 批量替换时采样首个命中段留档；
- **方式 C**：`issue add` 排查时显式登记。

promote 的安全默认值：`--severity` 默认 **warn**（先 warn 后 block，铁律）、
作用域默认**单书**（错题来源书目，`--book '*'` 才全局）、`--cat`/`--note`
默认继承错题。转正即返回 `preview` 命中预览（`en_matched` 原文命中数、
`flagged` 现有译文也将命中的段数、样例段号）——复盘当场校准正则，防止过宽
误伤或过窄失效。`stats` 的 `staged_issues` 字段随时提示错题积压量，攒够了
再复盘。

### 导出（`export`）

```bash
# 标准交付：精修稿为主，缺的地方自动回退机翻稿
python transdb.py export BOOK --out 成品.md

# 严格模式：不要回退，缺译段直接显式标记（宁可醒目、绝不静默丢段）
python transdb.py export BOOK --field refined --fallback none --out 成品.md

# 对照稿（审校用）：每段原文一行 + 译文一行
python transdb.py export BOOK --bilingual --out 对照.md

# 其他选项：
#   --field refined|machine|en     导出哪个字段（默认 refined）
#   --fallback machine|refined|en|none   缺失回退（默认 machine）
#   --md-headings                  标题段加 ## 前缀
#   --sep blank|single             段落间空行（默认）或单换行
#   --notes footnote|none          译注导出（默认 footnote=段尾角标+文末脚注区；
#                                  none=纯净稿不带注）
```

`--notes footnote` 的效果（译注全书连续编号）：

```
……四分卫喊了一声“hike”，中锋把球从胯下递出。[1]

下一句正文……

———
译注
[1] hike 为美式橄榄球开球口令；tight end 即近端锋。
```

导出后务必看一眼 JSON 摘要里的 `missing`：为 0 才是完整交付。

### 成品比对与回写（`diff` / `sync-md`）

导出的成品在外部编辑器里又动过、行数对不上库了？用内容对齐秒级定位错位：

```bash
# 比对外部成品与库内段落（按内容对齐，非死板按行号）：标出差异段、文件缺段、文件多块
python transdb.py diff BOOK --file 成品/书名.md
python transdb.py diff BOOK --file 成品/书名.md --all      # 连一致段也列出

# 把外部手改回写库：仅把「内容不同」的段用文件版覆盖 refined_zh（置 refined）
python transdb.py sync-md BOOK --file 成品/书名.md         # 默认 dry-run 预览
python transdb.py sync-md BOOK --file 成品/书名.md --write # 确认后实写
```

- 比对键为「压缩空白后的全文」，外部硬换行/多空格不影响对齐判断。
- `sync-md` 只更新既有段落，**绝不增删段落**；文件多出/缺失的块只报告，请人工核对。
- 回写前建议先 `diff` 过目，或直接依赖默认的 dry-run 预览。

### 高级

```bash
# 直查 SQL（默认只读；写操作必须显式 --write，请先备份）
python transdb.py sql "SELECT para_index, refined_zh FROM paragraphs WHERE book_id='demo' AND status='pending'"

# 手工重置某段/某区间状态（重做用）
python transdb.py set-status BOOK --index 42 --status pending
python transdb.py set-status BOOK --from 100 --to 150 --status pending
```

## 6. 四阶段完整工作流

```
阶段一 入库     import 一次性把整本书原子化存入 SQLite（2400 段 < 1 秒）
               双语对照稿用 import --dual：自带机翻直接 translated，跳过阶段二
阶段二 机翻     循环：next（pending）→ 译 → apply，每批 20 段（dual 导入可整段跳过）
阶段三 精修     循环：next --status translated → 润色 → apply（写 refined_zh）
阶段四 交付     glossary check + qc + 针对性修补 → replace 统一术语 → export
               错题池攒批复盘：issue list 归纳共性 → issue promote 提炼转正（人工触发）
```

**双语对照稿（`--dual`）的启动方式**：入库后 `stats` 会显示绝大部分段已是
`translated`、少量 `pending`（原稿漏译，如 Part 2 的 91 处）。推荐顺序：
先 `next --status pending` 把漏译段补机翻（写 `machine_zh`），再进精修——
也可以先精修主体、最后补漏，状态机都兜得住。

每个阶段都是**天然可中断、可续传**的：状态就是进度。中途断了、关机了、改主意了，
随时 `stats` 看进度，重发阶段指令即可从断点继续，已完成的段不会被重复处理。

## 7. 在 Antigravity 中配合 Gemini 3.8 Flash 使用

### 目录布局

建议为每本书（或每个工作区）这样组织，Antigravity 打开该文件夹作为工作区：

```
我的翻译工作区/
├── AGENTS.md          ← 智能体规则（本仓库已提供模板，复制过去按需改）
├── transdb.py         ← 工具本体
├── translations.db    ← 数据库（自动生成）
├── 原文/novel_p1.txt
└── updates/           ← 智能体每次 apply 用的 JSON（自动生成，留档即审计）
```

### AGENTS.md 的作用

Antigravity 会自动读取工作区根目录的 `AGENTS.md`。本仓库提供的
[AGENTS.md](AGENTS.md) 已经写好了：硬规则（禁止中间 txt、必须走 apply、批量 20 段、
每批后 stats）、命令速查、四阶段剧本。**把你的译文风格要求粘贴进其中的占位区**，
Gemini 就会全程按规矩干活。

### 各阶段给 Gemini 的话（可直接复制）

**机翻阶段：**

> 继续《novel_p1》的机翻。运行 `python transdb.py next novel_p1 --n 20 --max-chars 6000`，
> 按 AGENTS.md 的风格要求翻译 `target_chunk` 里的每段（context_before/after 仅供参考），
> 写入 `updates/batch_XXXX.json` 后执行 `python transdb.py apply updates/batch_XXXX.json`，
> 然后 `stats` 汇报进度。重复直到 pending 清零。

**精修阶段：**

> 进入精修阶段。循环运行 `next novel_p1 --status translated --n 10 --max-chars 6000`，
> 参照上下文把机翻稿改成流畅的白描文风，写 `refined_zh` 并 apply。
> 遇到人名/术语查 `glossary list`，译名必须与术语表一致。

**双语对照稿（dual 导入）起步：**

> 用 `import 文件.txt --book 书目ID --dual` 入库（自带译文直接 translated），
> 然后 `stats` 汇报 translated / pending 分布；先用机翻阶段提示词把 pending 的
> 原稿漏译段补完，再进入精修阶段。

**交付阶段：**

> 运行 `glossary check` 和 `qc`（v1.4 起含警惕库规则扫描），把所有标记段落逐类修复：
> `rule_*` / 句式病灶优先 `next BOOK --flagged --n 10` 按批拉取微创重写（批次自带
> flag_details 规则卡），其他用 `show` 看全文、`apply` 更正，不要整段重置。清零后运行
> `export novel_p1 --field refined --fallback none --out 成品/novel_p1.md`，
> 确认 JSON 摘要 missing 为 0 再交付。

**批大小建议**：Gemini 3.8 Flash 机翻每批 20 段、精修每批 10 段比较稳；
质量要求高就降到 10 / 5。批太大容易截断，批太小往返开销大。用 `--max-chars 6000`
（机翻）/ `--max-chars 8000`（精修，含机翻稿）按字数兜底，段落长短不一时尤其推荐。

**并行加速**：开多个 Antigravity 会话，都用 `next --claim` 取批，互不冲突
（SQLite WAL 模式支持多读单写，`busy_timeout` 已设 5 秒）。某个会话崩了，
它的批次卡在 wip，`release BOOK --older-min 30` 一键释放。

## 8. 人名术语统一：夏娃 → 伊芙 实战

发现全书 Eve 的译名混乱（夏娃/伊芙并存），标准处置三步：

```bash
# ① 看看到底乱成什么样
python transdb.py search BOOK --zh 夏娃 --en Eve --full   # 原文有 Eve 却译成夏娃的段
python transdb.py search BOOK --zh 夏娃 | head -40        # 所有含夏娃的段（含正常的）

# ② 登记标准译名，让工具以后自动盯防
python transdb.py glossary add BOOK Eve 伊芙

# ③ 全局精准替换：只动原文含 Eve 的段落，其他语境的“夏娃”（如宗教泛指）分毫不动
python transdb.py replace BOOK --find 夏娃 --replace 伊芙 --if-en Eve --dry-run
python transdb.py replace BOOK --find 夏娃 --replace 伊芙 --if-en Eve --field both

# ④ 验收
python transdb.py glossary check BOOK     # 应为 0 mismatch
```

白兰地/布兰迪同理：`glossary add BOOK brandy 白兰地` → `replace BOOK --find 布兰迪 --replace 白兰地 --if-en brandy --field both`。

## 9. 数据安全与备份

- **数据库就是一个文件**（`translations.db`）。备份 = 复制文件；换机器 = 拷走文件。
- WAL 模式下正常运行会伴随 `translations.db-wal` / `-shm` 两个小文件，属正常现象；
  所有命令退出时会自动归并。**备份请三个文件一起拷**，或拷贝前先跑一次任意命令。
- `import --force` 会删该书全部段落与译文（术语表保留）——执行前工具会明说，想清楚再敲。
- `sql --write` 是唯一能绕过所有保护罩的后门，用前先备份。
- 导出永远不丢段：缺译段会写成 `[[缺译 #N]]` 并在 JSON 摘要里列出，宁醒勿漏。

## 10. 常见问题 FAQ

**Q：原文是 GBK 编码的 txt，行吗？**
行。导入时按 utf-8 → gb18030 → utf-16 自动尝试，导入结果 JSON 里的 `encoding` 字段
会告诉你实际用了哪种。导出一律 UTF-8。

**Q：导入时分段不对（一段被切成两段 / 两段粘成一段）？**
每行一段的书用 `--split line`；英文硬换行的书用默认 `--split blank`（段内换行自动合并）。
诗歌等需要保留段内换行的文本加 `--no-join`。

**Q：智能体写回的段落号会不会错？**
`apply` 对非法 `para_index` 默认整体报错（列出坏号、事务回滚），不会错位写入。
配合 `next` 的输出顺序，号段一一对应。

**Q：怎么重做某几段？**
`set-status BOOK --from 120 --to 135 --status pending`（机翻重做）或先清字段：
`sql --write "UPDATE paragraphs SET refined_zh=NULL WHERE book_id='x' AND para_index BETWEEN 120 AND 135"`，
再走对应阶段的 `next`。

**Q：控制台中文乱码？**
`chcp 65001` 或用 Windows Terminal。工具自身强制 UTF-8 输出，只是老控制台显示问题，
文件和 JSON 数据不受影响。

**Q：用 PowerShell 传大 JSON 会不会遇到编码崩溃 / 引号转义错误？**
按推荐用法不会。批量中文内容一律「落盘为 UTF-8 文件 → `apply 文件`」，完全不经过
PowerShell 的管道与命令行引号；`search`/`replace` 的短中文参数走 Windows 的 Unicode
传参通道（`CreateProcessW`），与代码页无关；`next` 的批次输出走 stdout（已强制 UTF-8），
不受命令行 32K 长度限制。明确禁止三件事：`echo '...' | apply -`、把 JSON 内联进命令行、
用 PowerShell 5.1 管道喂中文（其管道默认按 ASCII 重编码，中文先被替换成 `?`，下游救不回）。
`apply -` 只接受 UTF-8 字节流，喂 GBK 会立即报错并提示改用文件（v1.2.1 起），
绝不把乱码写进数据库。

**Q：能在手机/U盘上跑吗？**
能，只要那个环境有 Python 3.8+。数据库文件跟着走就行。

## 11. 命令速查表

```bash
python transdb.py selftest                                  # 自测
python transdb.py import f.txt --book ID [--split line] [--dual]  # 入库（dual=双语对照稿）
python transdb.py books / stats ID                          # 进度
python transdb.py next ID --n 20 --max-chars 6000 [--status translated] [--chunks 3] [--before 2 --after 1] [--ids 5,9] [--claim]  # 滑动窗口批次（带 hints）
python transdb.py next ID --flagged --n 10                  # 取带质检标记的病灶段
python transdb.py apply updates.json [--allow-partial] [--force|--no-gate]      # 写回（规则门禁）
python transdb.py show ID --index 5 / --from 5 --to 9       # 看段
python transdb.py search ID --zh 词 / --en word             # 检索
python transdb.py replace ID --find A --replace B [--if-en w] [--field machine|both] [--dry-run]  # 全局替换
python transdb.py qc ID [--badwords w1,w2 | --badwords-file f] [--cliche] [--rules-only] [--save] [--clear]  # 质检（含规则扫描）
python transdb.py glossary add ID EN ZH [--forbidden w1,w2] / load ID f.tsv / check ID       # 术语
python transdb.py rule init-defaults / list / add --cat trap --en '..' --bad '..' [--severity block] / remove --id N  # 警惕库
python transdb.py issue list --status open / add BOOK --index N --cat trap --en 触发词 --note 病因 / promote --id 15,16 --en '..' --bad '..' / ignore --id N  # 错题池（v1.5）
python transdb.py export ID --out 成品.md [--fallback none] [--bilingual] [--notes none]      # 导出
python transdb.py diff ID --file 成品.md                     # 外部成品与库对齐比对
python transdb.py sync-md ID --file 成品.md [--write]        # 外部手改回写（默认预览）
python transdb.py set-status ID --index 5 --status pending  # 重置
python transdb.py release ID --older-min 30                 # 释放卡死批次
python transdb.py sql "SELECT ..." [--write]                # 直查
```

---

*transdb v1.5.1 · 零依赖 · SQLite 3 内置 —— 问题与改进随时开工。*
