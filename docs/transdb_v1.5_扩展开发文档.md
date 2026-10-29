# transdb v1.5.0 扩展开发文档：临时错题池（issue_staging）与提炼闭环（评审裁定版）

> 本版 = 原始提案「§8 临时错题池与提炼闭环」+ 对照 v1.4.0 源码两轮评审后的裁定。
> 原提案的悬而未决项已全部裁定（见 §0），按本文档可直接开发。
> 编号说明：原提案自称 §8 与 v1.4 文档既有 §8–§10 冲突，本设计独立成 v1.5 文档。

## 0. 裁定摘要（评审缺口 → 定论）

| # | 原提案缺口 | 裁定 |
|---|---|---|
| 1 | promote 示例直接 `--severity block`，违反 v1.4 §9「先 warn 后 block」铁律（一条误伤 block 规则 × 「必须修到 blocked=0」= 流水线死锁） | promote CLI 默认 **warn**，与 `rule add` 一致；确认无误后走 `rule add`（去重更新）升 block |
| 2 | promote 无 `--cat`/`--note`，四类规则只能转正 trap 形状，syntax/style 样本出不了池 | **默认继承所选错题的 category 与 note**，可显式覆盖；多 id 的 category 不一致且未指定 `--cat` 时 die |
| 3 | promote 只收单个 id，与「归并同类样本、复发 ≥2 次才转正」的复盘工作流相悖 | `--id` 支持逗号列表，全部标记 promoted 并回填同一 rule_id |
| 4 | promote 无命中反馈，规则盲入库（事后正则是过宽/过窄的主要失败点） | 转正返回 **preview**：对来源书目统计 en_pattern 命中数、en+bad 双命中数（= 将打标/拦截的段）、样例段号，复盘当场校准 |
| 5 | `--scope global` 默认，单书风险直接放大为全库 | 复用 `--book` 参数名，**默认单书**（错题来源书目），显式 `--book '*'` 才全局——与「先 warn 后 block」同构的「先窄后宽」 |
| 6 | bad_zh 取值语义与入池边界未定 | 存**「被覆盖字段的旧值」**：写 `refined_zh` 时 bad=旧 refined_zh（无则旧 machine_zh）；写 `machine_zh` 时 bad=旧 machine_zh。被 block 拦截的段不入池（未写库无对照）；未写译文的条目不入池；非法 `issue_cat` 跳过入池但译文照写 |
| 7 | issue list 无体积控制，复盘会话上下文爆炸（v1.4 用 HINTS_MAX 防过的问题在复盘侧重演） | **存储全量、展示截句**：行内冻结全文快照；list 默认展示锚点句（en_text 按 `issue_en` 触发词截句，zh 按 snippet）+ 分页（`--limit` 默认 30）；`--full` 看全文 |
| 8 | AGENTS.md 出现双入口（铁律 3 直接 rule add vs 新记账指令），且「顺手记」易被 Flash 过度触发 | 分工判据写死：**人类指出的错 → 直接 `rule add`**（有人背书）；**Agent 自查发现 → 只入池**。记账纪律：仅类别明确的错误、每批 ≤3 条、`issue_en` 必须点出英文触发词 |
| 9 | 快照存哪：给 paragraphs 加 prev 列会被后续编辑无声覆盖（证据污染） | 快照存进 **issue_staging 行内**，写入即冻结、与活数据解耦；不做版本历史（`updates/*.json` 落盘文件已是完整审计轨迹，深挖随时可翻） |
| 10 | 工程配套（selftest / 版本 / 文档）未提 | `VERSION=1.5.0`、`SCHEMA_VERSION=3`；apply/replace/stats 返回**只增字段**；selftest 追加 `issues:` 命名断言组；README / AGENTS.md / SKILL.md 同步 |

## 1. 目标与设计原则

针对 v1.4 AGENTS.md 铁律 3 要求 Agent **边翻译边写正则**的两个代价（分散翻译注意力、
仓促正则质量差），引入双层漏斗：

1. **翻译/精修途中只记原始错例**（0 额外命令开销，几个短字段顺手带出）；
2. **事后由人类触发复盘**，批量归纳共性、提炼为正式 `rules` 规则。

分层边界：`issue_staging` 是**纯离线层**——不参与 `next` hints、不参与 apply 门禁、
不参与 qc 扫描；闭环终点是 `issue promote` 把样本转正进 `rules`，此后才进入
hints/门禁/qc 三道在线关卡。错题池不设自动清理，promoted/ignored 行留作审计。

## 2. Schema（SCHEMA 常量追加，老库 open_db 自动补建，零迁移）

```sql
CREATE TABLE IF NOT EXISTS issue_staging (
    issue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     TEXT NOT NULL,
    para_index  INTEGER,                    -- 关联段落号（复盘需要更多语境时 show 取整段）
    category    TEXT DEFAULT 'trap',        -- 同 rules 四类：trap|badword|syntax|style
    issue_en    TEXT DEFAULT '',            -- 英文触发词（Agent 只写几个词；展示层截句的锚点）
    en_text     TEXT DEFAULT '',            -- 英文段全文快照（冻结）
    bad_zh      TEXT DEFAULT '',            -- 被替换的旧译文全文快照（冻结，语义见 §0-6）
    good_zh     TEXT DEFAULT '',            -- 新译文全文快照（冻结）
    note        TEXT DEFAULT '',            -- 一句话病因分析（promote 时默认继承为规则 note）
    status      TEXT NOT NULL DEFAULT 'open', -- open | promoted | ignored
    rule_id     INTEGER,                    -- 转正后回填 rules.rule_id
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON issue_staging(status, book_id);
```

存储策略（裁定 #9）：**三个文本字段一律存全文快照**，行写入即定格——段落后续
再怎么改（二次精修、replace、sync-md）都不影响这条证据。不做 prev 列、不做
版本链：完整历史已在 `updates/*.json`，行内快照只为复盘就近取材。

## 3. 三种零摩擦写入方式

### 方式 A：apply 写回时顺手记录（主通道，0 额外命令）

`updates` 条目在译文字段之外可带三个短键：`issue_cat`（缺省 trap）、
`issue_en`（英文触发词）、`issue_note`（一句话病因）。apply 对**成功写入译文**
的条目自动入池：

- bad = 被覆盖字段的旧值（写 refined_zh → 旧 refined_zh，无则旧 machine_zh；
  写 machine_zh → 旧 machine_zh，首次机翻时为空串）；
- good = 本次写入的新译文；en_text = 该段原文快照；
- **被 block 拦截的段不入池**（拒写方案下无「改稿后」；返工成功的 fix JSON
  再带 issue 字段即可入池）；条目未写译文（仅 status/notes）不入池；
- 非法 `issue_cat`：stderr 警告并跳过入池，译文照常写入（注记字段不阻断主流程）；
- 返回 JSON 新增 `staged` 计数（只增字段）。

示例（精修批次顺手记一条 trap）：

```json
{
  "book_id": "novel_p1",
  "updates": [
    { "id": 42, "refined_zh": "他在河岸上坐下，望着水面。",
      "issue_cat": "trap", "issue_en": "bank",
      "issue_note": "bank 此处指河岸，原机翻误译为银行" }
  ]
}
```

### 方式 B：`replace --log-issue [备注]` 替换时采样留档

```bash
python transdb.py replace BOOK --find 银行 --replace 河岸 --if-en bank \
  --log-issue "河岸语境 bank 指河岸"
```

实跑时把**首个命中并被改动的段**采样入池（en_text 全文 + 替换前后全文 +
备注），`issue_en` 取 `--if-en` 首词，category 固定 trap。`--dry-run` 不写库
也不入池。返回 JSON 新增 `staged`。

### 方式 C：显式登记 `issue add`

```bash
python transdb.py issue add BOOK --index 102 --cat syntax \
  --en "persuaded" --good "听了她的话，他同意了。" \
  --note "被…所说服 欧化被动，应改主动句"
```

工具自动进库抓该段 `en_text` 与当前译文（refined 优先）作 bad_zh——无需手工
复制长文本。`--bad` 可覆盖自动抓取值，`--good` 可选。

## 4. `issue` 子命令族

```bash
# 查看待提炼错题池（默认 status=open；展示截句见 §5；JSON 输出供复盘 Agent 阅读）
python transdb.py issue list [--book BOOK] [--status open|promoted|ignored|all] \
                             [--cat trap|badword|syntax|style] [--limit 30] [--full]

# 批量转正为正式规则（同时回填 rule_id、置 promoted）
python transdb.py issue promote --id 15,16,18 \
  --en '\bbank\b' \
  --bad '银行' \
  --good '河岸/堤岸' \
  --severity warn            # 默认 warn（裁定 #1），确认无误再升 block

# 忽略偶发/无需入库的条目
python transdb.py issue ignore --id 16,17
```

**promote 规格**（裁定 #1–#5）：

- 校验链**完全复用** `rule add` 的 `_compile_or_die` / `_rule_fields_ok` /
  `_rule_upsert`——正则非法或字段组合不符即 die（整单拒绝，错题保持 open，
  不留半成品）；
- `--cat` / `--note` 缺省继承所选错题（多 id 且 category 不一致又未指定 `--cat`
  → die）；`--good` 不继承（错题里的 good 是整段译文，规则的 suggest_zh 要短）；
- `--book` 缺省 = 错题来源书目（单书起步），`--book '*'` 才全局；
- `--severity` 缺省 warn；style 类固定 hint（同 rule add）；
- `--id` 逗号列表；任一 id 不存在或非 open 状态 → die；
- 返回 JSON 附 **preview**（对来源书目全书实跑候选正则）：

```json
{
  "rule_id": 12, "updated": false, "promoted": [15, 16, 18],
  "book_id": "novel_p1", "category": "trap", "severity": "warn",
  "preview": { "book": "novel_p1", "en_matched": 14,
               "flagged": 3, "sample_ids": [102, 221, 407] }
}
```

`en_matched` = 原文命中数（影响 hints 挂载面）；`flagged` = 原文命中且现有译文
命中 bad_zh 的段数（= 将被打标/拦截的面）；纯中文规则（syntax/badword）只报
`flagged`。复盘纪律：复发 ≥2 次的先转正、一律 warn + 单书起步、看 preview
命中面校准正则后再决定升降级与作用域。

## 5. 展示层：存储全量、展示截句（裁定 #7）

`issue list` 默认输出紧凑视图，防止几十条 × 三大段的上下文爆炸：

- `en_text` → **锚点句**：包含 `issue_en` 触发词的那个句子（断句为近似切分，
  触发词必落在切出的片段内；无锚点或找不到 → snippet 截断）；
- `bad_zh` / `good_zh` → snippet 截断；
- `--limit`（默认 30）分页，返回 `total` / `returned` 让复盘 Agent 知道翻页；
- `--full` 输出全文快照（校准正则需要更宽语境时配合 `show BOOK --index N`）。

## 6. AGENTS.md 配套（裁定 #8）

警惕库铁律区改动两处：

1. **铁律 3 改为双入口判据**：人类指出的错译 → `apply` 改段 + 直接 `rule add`
   提炼（有人背书，高置信）；Agent 自查发现（精修中修正的机翻死译、恶性被动、
   惊艳白描）→ **不写正则**，updates 条目顺手带 `issue_cat`/`issue_en`/
   `issue_note` 三短字段入池。
2. **新增铁律 4（记账纪律）**：只记类别明确的错误与真正惊艳的改写，每批 ≤3 条；
   `issue_en` 必须点出英文触发词；提炼转正只在人类发出「提炼临时库」指令时
   批量进行。命令速查表追加 `issue` 行；阶段三剧本补一句：交付前
   `issue list --status open` 有积压时向用户报告数量，等用户决定是否复盘。

## 7. selftest 断言组（`issues:` 前缀，沿用临时目录建库模式）

1. `issues：add 自动抓取该段 en_text 与当前译文作 bad`
2. `issues：apply 带 issue 字段入池（首次精修 bad=旧 machine_zh）`
3. `issues：apply 重修场景 bad=旧 refined_zh`
4. `issues：block 拦截段不入池`
5. `issues：未写译文的条目不入池（译文照写）`
6. `issues：非法 issue_cat 跳过入池`
7. `issues：replace --log-issue 采样首个命中段（dry-run 不入池）`
8. `issues：list 截断展示 / --full / 状态过滤`
9. `issues：promote 默认 warn + 默认单书作用域`
10. `issues：promote 多 id 转正回填 rule_id`
11. `issues：promote 坏正则整单拒绝且错题仍 open`
12. `issues：promote category 不一致未指定 --cat 报错`
13. `issues：promote 返回 preview 命中统计`
14. `issues：ignore 批量忽略`

## 8. 兼容与迁移

1. `VERSION = "1.5.0"`；`SCHEMA_VERSION = 3`（常量对齐，不在运行时强制校验）。
2. 老库零迁移：`issue_staging` 经 SCHEMA executescript 自动补建，不碰既有四表。
3. 输出只增字段：apply 增 `staged`、replace 增 `staged`、stats 增
   `staged_issues`（该书 open 错题数，作为人类触发复盘的信号）；退出码语义不变。
4. README / AGENTS.md / SKILL.md 同步（SKILL.md 顺带修复其落后 v1.4 的
   参数名漂移：glossary add 无 `--category`、replace 是 `--find/--replace`）。

## 9. 追记（v1.5.1）：种子数据外置，仓库只保留工具

公开定位确立后，`DEFAULT_RULES` 与 `WEBNOVEL_CLICHES` 两个内置常量从
`transdb.py` 移除——规则词条与俗套词表属于使用方的本地积累，不随仓库分发：

1. **种子文件 `transdb_seeds.json`**：`{"rules": [...], "cliches": [...]}`，
   rules 格式同 `rule load`；默认当前目录，`TRANSDB_SEEDS` 环境变量可改路径；
   已加入 `.gitignore`（本地数据默认不上传）。示例见 `transdb_seeds.example.json`。
2. **`rule init-defaults [file]`**：从种子文件幂等导入（校验链同 `rule load`，
   任一条非法整批拒绝）；文件缺失时 die 并给出复制示例 / 传参 / 环境变量三条指引。
3. **`qc --cliche`**：俗套词表改由种子文件 `cliches` 字段提供；无种子文件时
   stderr 提示并跳过（不 die——qc 不因词表缺席阻断主流程）。
4. **selftest 改用合成种子**：临时目录内自造种子文件（zword / 禁词甲 /
   测试套路词等合成词条）跑全链路，工具源码不再含任何真实词条；断言数不变（73）。
5. 历史 commit（含远端 v1.4-rules / v1.5-issue-staging 分支）中仍可见旧内置
   数据；如需彻底抹除需重写历史 + 强推，另行决定。
