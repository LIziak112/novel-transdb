# transdb v1.4.0 扩展开发文档：翻译警惕库与 Agent 自进化闭环（补遗完整版）

> 本版 = 原设计稿 + 逐条对照 v1.3.0 源码后的补遗裁定。补遗内容集中在 §0 摘要，
> 并已直接整合进各章节（裁定处不再保留原稿的歧义表述）。按本文档可直接进入开发。

## 0. 补遗摘要（本轮补充/裁定清单，开发前必读）

原方案主体成立（表结构、四类规则、next 注入 / apply 门禁 / qc 扫描的分工都对），
但有以下缺口，不补会在开发中途卡住或上线后翻车：

| # | 缺口 | 裁定/补充 | 详见 |
|---|---|---|---|
| 1 | §4.2 block 语义留了两个方案（"拒绝推进状态（或者写入译文但打标）"）没定 | 裁定为**拒写方案**：本段本次完全不写库 | §4.2 |
| 2 | flag 命名三处不一致（`rule_trap:#2(银行)` / `trap:#ID(命中词)` / `badword:#ID`），且带 `#()` 的格式会破坏 `merge_flags` 的逗号分隔与前缀删除逻辑，还与既有 `badword:词` 撞名 | 统一为 `rule_<cat>:<rule_id>`，命中词只进 JSON 不进 flags | §2.1 |
| 3 | hit_count 计数口径含糊（§4.1 说 next 挂载就 +1） | 只在**译后校验命中**时 +1，译前挂载不计数 | §4.1 |
| 4 | 标记生命周期只有"打"没有"清" | apply 写回即重算该段规则、修好自动清 `rule_*` 标记；qc 全书 drop-and-recompute | §4.2 / §4.3 |
| 5 | 一条手滑的坏正则会让 next/apply/qc 全线崩溃 | add/load 时编译校验拒绝；运行时 try/except 跳过 + stderr 警告 | §3.4 |
| 6 | rules 表没有去重键，add / load / init-defaults 幂等语义未定义 | UNIQUE(book_id, category, en_pattern, bad_zh)，命中键则更新 | §2 / §3 |
| 7 | apply 返回 JSON 兼容性与退出码未定 | 只增字段不删字段；blocked>0 时退出码仍为 0 | §4.2 |
| 8 | gate 校验哪个字段有歧义（"refined_zh 优先，次选 machine_zh"） | 只校验**本次实际写入**的译文字段 | §4.2 |
| 9 | hints 体积无上限，规则库变大后 next 输出膨胀 | 每批 ≤10 条，按 hit_count 截断 | §4.1 |
| 10 | `--flagged` 行为规格太粗（与 --status 关系、标记怎么展示） | 精确规格 + `flag_details` 规则卡展开 | §4.1 |
| 11 | 旁路写入（`import --dual`、`sync-md --write`）不过 apply 门禁 | 明确 qc 为全量兜底 | §4.3 |
| 12 | v1.3.0 既有 bug：`badword:*` 不在 `QC_OWNED_CODES`，词改掉后旧标记永久残留 | v1.4 顺手修复并回归测试 | §4.3 |
| 13 | 宽 en_pattern 的 block 规则会误伤（"sat by the bank" 真在写银行），配上"必须修到 blocked=0"的铁律会死循环 | --force 逃生通道 + 新规则先 warn 后 block 的升级流程 + 种子库高危词注记 | §5 / §9 |
| 14 | 与 glossary forbidden_zh 功能重叠，边界未说明 | 分工判据：全书固定译名归 glossary，语境歧义归 rules | §1.1 |
| 15 | selftest 写"新增第 11、12 项"，但现有自测是 32 项**命名断言**、无编号体系 | 改为追加命名断言组（15 项） | §7 |
| 16 | AGENTS.md 升级只给了 3 条铁律，缺命令速查行、阶段剧本集成、init-defaults 时机 | 补全 AGENTS.md 改动清单 | §6 |
| 17 | 版本迁移与 README 同步未提 | VERSION/SCHEMA_VERSION、老库自动建表、README 改动清单 | §8 |

---

## 1. 扩展目标与设计原则

在不破坏 `transdb v1.3.0` 现有命令兼容性、保持 **Python 3.8+ 零第三方依赖、单文件 `transdb.py`** 的前提下，针对 **Antigravity + Gemini 3.8 Flash** 工作流引入「翻译警惕库与句式经验库」闭环：

1. **译前动态提示（Just-in-Time Hints）**：`next` 取出批次时，自动扫描当前批次原文，仅将被触发的警惕卡与好句范例注入 JSON 输出的 `hints` 字段，避免撑爆 `AGENTS.md` 上下文。
2. **写回实时门禁（Apply-Time Gate）**：`apply` 写回译文时，自动执行毫秒级规则校验。命中致命错译（`block` 级）时直接拦截，并在 `apply` 返回的 JSON 中立刻向 Agent 报错，实现当场返工。
3. **句式与被动语态扫描（Syntax Anti-Patterns）**：扩展 `qc` 支持中文翻译腔、恶性被动句式的正则探测，并支持 `next --flagged` 按批次拉取病灶段落进行微创修复。
4. **跨书全局继承（Global + Book Scope）**：支持全局通用规则（`book_id = '*'`）与单书专属规则并存，积累一次、全书目复用，并统计规则命中次数（`hit_count`）。

### 1.1 与 glossary 的分工边界（补遗）

glossary 的 `forbidden_zh` 与 rules 的 trap/badword 功能有重叠，必须给出判据，
否则使用者不知道往哪边登记：

| | glossary（既有） | rules / trap（新增） |
|---|---|---|
| 匹配粒度 | 英文**整词**（词界正则） | 英文**短语级正则**（可跨词、可带语境窗口） |
| 校验逻辑 | 译名**必须出现**（missing）+ 禁译词不得出现（forbidden） | 仅**禁译词不得出现**，且仅当原文命中 en_pattern 时生效 |
| 适用 | 一个英文词全书一个固定译名：Eve→伊芙，禁 夏娃 | 同一个英文表达在不同语境译法不同：bank、suit |
| 登记命令 | `glossary add` | `rule add --cat trap` |

判据一句话：**全书固定映射进 glossary，语境歧义进 rules**。两边并存互补，
glossary check 继续负责术语一致性，rule gate 负责语境消歧。

---

## 2. 数据库 Schema 变更

在 SQLite 初始化逻辑（`SCHEMA` 常量 + `open_db` 的 executescript）中新增一张
`rules` 表（不影响既有 `books`、`paragraphs`、`glossary` 表；老库打开即自动补建，
无需手工迁移）：

```sql
CREATE TABLE IF NOT EXISTS rules (
    rule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     TEXT NOT NULL DEFAULT '*',  -- '*' 表示全局通用，具体 book_id 表示单书专属
    category    TEXT NOT NULL DEFAULT 'trap', -- 'trap'(双语假朋友/俚语) | 'badword'(纯中文禁词) | 'syntax'(句式/被动反模式) | 'style'(黄金范例)
    en_pattern  TEXT DEFAULT '',            -- 英文触发正则（为空表示纯中文规则；匹配时一律 IGNORECASE）
    bad_zh      TEXT DEFAULT '',            -- 禁用/劣质中文正则（为空表示仅作译前提示，不做译后拦截）
    good_zh     TEXT DEFAULT '',            -- 推荐译法或正面改写示范
    note        TEXT DEFAULT '',            -- 给 Agent 看的简短原因/动作力学解释
    severity    TEXT NOT NULL DEFAULT 'block', -- 'block'(写回拦截) | 'warn'(写入但打标) | 'hint'(仅译前挂载)
    hit_count   INTEGER NOT NULL DEFAULT 0, -- 累计【译后】命中次数（apply 拦截/警告、qc 打标；译前挂载不计）
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (book_id, category, en_pattern, bad_zh)  -- 补遗：去重键，add/load/init-defaults 幂等的基础
);

CREATE INDEX IF NOT EXISTS idx_rules_book ON rules(book_id);
```

### 2.1 error_flags 标记命名（补遗：统一裁定）

v1.3.0 的 `error_flags` 是**逗号分隔**的标记串，`merge_flags()` 按「整码或
`码:` 前缀」做删除重算。因此规则标记必须遵循同一词法，且**命中词/片段只进
JSON 输出、不进 flags**（否则词里带逗号/括号会破坏解析）：

| category | 标记格式 | 示例 |
|---|---|---|
| trap | `rule_trap:<rule_id>` | `rule_trap:2` |
| badword | `rule_badword:<rule_id>` | `rule_badword:5` |
| syntax | `rule_syntax:<rule_id>` | `rule_syntax:7` |
| style | 永不打标 | —— |

- 命中词、命中片段、建议译法放在 `apply` 的 `violations/warnings` 与 `qc`
  的 `rows[].rule_hits` 里（一次性给全 Agent 要的信息），flags 里只留可机器
  解析的 `rule_<cat>:<id>`。
- 新增常量 `RULE_OWNED_CODES = {"rule_trap", "rule_badword", "rule_syntax"}`，
  供 `merge_flags()` 按前缀做 drop-and-recompute（与既有
  `QC_OWNED_CODES` 同一套机制）。

### 2.2 四类规则（`category`）的判定逻辑：

| `category` | `en_pattern` | `bad_zh` | 译前 `next` 行为 | 译后 `apply` / `qc` 行为 | 典型场景 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`trap`**<br>双语消歧/俚语 | 必填（支持跨词正则，如 `\bthe\s+bank\b`） | 必填（如 `银行`） | 原文匹配 `en_pattern` 时，将该卡加入批次 `hints` | 原文匹配 `en_pattern` **且** 译文匹配 `bad_zh` 时触发 `rule_trap:#ID` | `bank` 误译银行（河岸语境）、`suit` 误译西装（同词不同译） |
| **`badword`**<br>纯中文禁词 | 必须为空（需要英文门控就归 trap 类） | 必填（如 `俗套词甲\|俗套词乙`） | 不挂载（节省 Token） | 只要译文匹配 `bad_zh` 即触发 `rule_badword:#ID` | 恶俗网文套话、低级死译残留词 |
| **`syntax`**<br>句式/被动反模式 | 可选（通常为空；非空时行为同 trap 的英文门控） | 必填（中文句式正则，如 `被[^，。！？]{1,8}所`） | 不挂载（或仅挂载 Top 3 常驻范例） | 译文匹配 `bad_zh` 时触发 `rule_syntax:#ID`，附上 `good_zh` 改写示范 | 恶性被动句、弱动词堆叠（`进行了…的观察`） |
| **`style`**<br>黄金白描范例 | 建议填写（为空则永不挂载，`rule add` 时警告） | 必须为空 | 原文匹配 `en_pattern` 时挂载到 `hints` 供润色参考 | 不做译后校验、永不打标 | 动作戏白描示范、特定语境语感定调 |

> `severity` 的实际效果按 category 收敛：`style` 只能是 `hint`（无 bad_zh 可拦）；
> `trap/badword/syntax` 三档均可。`hint` 级 = 永不拦截、永不打标、只挂载。

---

## 3. 新增子命令：`rule`（经验库管理与快速沉淀）

新增 `python transdb.py rule <subcmd>` 子命令族，全部输出 JSON 到 `stdout`：

### 3.1 添加规则（`rule add`）
智能体或人工在改稿时发现错误，一条命令即可沉淀入库：

```bash
# 1. 添加双语消歧卡（默认全局 '*'，可通过 --book 指定单书）
python transdb.py rule add \
  --cat trap \
  --en '\bthe\s+bank\b' \
  --bad '银行' \
  --good '河岸/堤岸' \
  --note '河岸语境下 bank 指岸，严禁译成金融机构' \
  --severity block

# 2. 添加单书特定场景消歧卡（如某书河岸场景的 the bank）
python transdb.py rule add --book novel_p1 \
  --cat trap \
  --en '\bthe\s+bank\b' \
  --bad '银行' \
  --good '河岸/堤岸' \
  --note '河岸场景 bank 严禁译成银行'

# 3. 添加中文被动/翻译腔句式探测器
python transdb.py rule add \
  --cat syntax \
  --bad '被[^，。！？“”]{1,10}所[a-zA-Z一-鿿]{1,4}' \
  --good '改为主动句或零主语顺承短句（如：“他被她的话所触动” -> “听到这话，他心头一震”）' \
  --note '消除欧化“被…所…”被动句式' \
  --severity warn
```

**补遗：`rule add` 的参数校验与语义（全部强制，防手滑）**

1. **正则编译校验**：`--en` 与 `--bad` 先 `re.compile`，非法立即 `die` 报错并
   给出该正则的编译错误信息——绝不让一条坏正则进库毒死整条流水线。
2. **字段组合校验**：trap 要求 `--en`+`--bad` 必填；badword 要求 `--bad`
   必填且 `--en` 必须为空；syntax 要求 `--bad` 必填；style 要求 `--good`
   必填、`--bad` 必须为空、`--en` 为空时警告"该规则永远不会被挂载"。
3. **`--severity` CLI 默认 `warn`**（注意：不是表结构的 DEFAULT block——
   CLI 默认 block 太危险，随手加的规则不该直接获得拦截权；`rule add` 不传
   severity 时按 warn 落库，确信无误后再显式升 block）。
4. **去重更新语义**：命中 UNIQUE 键 `(book_id, category, en_pattern, bad_zh)`
   时不重复插入，改为更新 `good_zh / note / severity`（hit_count 保留），
   返回 JSON 带 `"updated": true`。这也是规则迭代（收窄正则、升降级）的
   正规途径。
5. **PowerShell 注意**：正则参数一律用**单引号**包裹（PS 双引号会吃反斜杠
   和 $，bash 无此问题）——与 AGENTS.md 硬规则 3 同源的 Windows 坑。

### 3.2 批量导入与导出（`rule load` / `rule dump`）
```bash
python transdb.py rule load rules_seed.json     # 批量导入（按 UNIQUE 键去重更新）
python transdb.py rule dump --out rules_backup.json  # 导出全部规则（--book 可过滤；不给 --out 则打到 stdout）
```

**补遗：文件格式** = rule 对象数组，字段与表结构一一对应（`book_id, category,
en_pattern, bad_zh, good_zh, note, severity`；`rule_id/hit_count/created_at`
由库自管，load 时忽略）。`load` 逐条走 §3.1 的编译校验与组合校验，
**任何一条非法则整批拒绝**（不半途而废），返回 `{"loaded": n, "updated": m,
"rejected": [...]}`。

### 3.3 查看与删除（`rule list` / `rule remove`）
```bash
python transdb.py rule list [--book BOOK] [--cat trap|badword|syntax|style]
python transdb.py rule remove --id 12
```

- `rule list` 返回 `{"rules": [{rule_id, book_id, category, severity,
  en_pattern, bad_zh, good_zh, note, hit_count, created_at}, ...]}`，按
  `rule_id` 升序；可选 `--top N` 按 `hit_count` 降序取前 N（找最活跃规则）。
- `rule remove` 返回 `{"removed": 0|1}`（id 不存在返回 0，不算错误）。

### 3.4 种子初始化（`rule init-defaults`）与运行时容错（补遗）

- `rule init-defaults`（v1.5.1 起从种子文件加载）按 §3.1 同样的去重键插入，
  **只插不存在的、不覆盖已改过的**（幂等：跑 N 遍结果一致），返回
  `{"inserted": n, "skipped": m}`。
- **运行时容错**（写进 `load_rules()` 公共函数）：`next/apply/qc` 加载规则
  时逐条 `try: re.compile(...) except: 跳过 + stderr 警告一次（带 rule_id）`。
  历史库中即便存在坏正则（如被 `sql --write` 手工塞入的），流水线也绝不崩，
  只是跳过该条。
- 可选（P2，不阻塞本轮）：`rule test --id N --en-text "..." --zh-text "..."`
  对样例文本单条试跑，返回是否挂载/是否命中，方便调试正则。

---

## 4. 现有核心命令增强设计

### 4.1 `next` 命令增强：动态 `hints` 注入与 `--flagged` 病灶拉取

1. **动态扫描并注入 `hints`**：
   - 在 `next` 组装每个 `batch` 时，读取当前生效的规则
     （`book_id IN ('*', 当前书目)` **且** `en_pattern != ''` 且编译通过）。
   - 用正则（IGNORECASE）扫描该批 **`target_chunk` 中所有段落**的 `en_text`
     ——`context_before` / `context_after` **不参与扫描**（它们不是翻译对象，
     见 AGENTS.md 硬规则 9 的同一逻辑）。
   - 若某段命中 `en_pattern`，将该规则组装进该批次顶层的 `"hints"` 数组
     （同一批次内按 `rule_id` 去重，多个段命中合并进 `matched_ids`）。
   - **补遗：hints 体积上限** —— 每批 `hints` ≤ **10** 条，超出按
     （`hit_count` 降序，`rule_id` 升序）截断，批次对象加
     `"hints_truncated": <被省略条数>`（为 0 时省略该字段）。防止规则库
     膨胀后 `next` 输出撑爆上下文——这恰恰是本设计要避免的问题。
   - **补遗：`hit_count` 此处不 +1**（计数口径见 §4.2/§4.3：只在译后命中时
     计）。`next` 的 hints 组装是纯读操作，不写库。
   - **输出的 JSON 新增 `hints` 字段**（若无命中则省略或空列表 `[]`，
     只增字段，老消费者不受影响）：
   ```json
   {
     "book_id": "novel_p1",
     "status": "pending",
     "batches": [
       {
         "ids": [101, 102],
         "chars": 420,
         "hints": [
           {
             "rule_id": 2,
             "category": "trap",
             "severity": "block",
             "matched_ids": [102],
             "en_pattern": "\\bthe\\s+bank\\b",
             "forbid_zh": "银行",
             "suggest_zh": "河岸/堤岸",
             "note": "河岸场景 bank 严禁译成银行"
           }
         ],
         "context_before": [...],
         "target_chunk": [...]
       }
     ]
   }
   ```

2. **新增 `--flagged` 参数（专供错句/句式重修）——补遗：精确规格**：
   - `python transdb.py next BOOK --flagged --n 10` 拉取
     `error_flags <> ''` 的段落（**不限状态**：translated/refined 都可能带标）。
   - **与 `--status` 互斥**（同时给出即 `die` 提示二选一）；与
     `--ids / --from / --to` 互斥（显式段号本来就能看任意段）。输出顶层
     `"status"` 字段填 `"flagged"` 以示取段模式。
   - `--n / --max-chars / --before / --after / --claim` 照常生效，
     `context_before / context_after` 照常附带。
   - **`target_chunk` 每项新增 `"error_flags"` 字段**（该段当前标记原串），
     Agent 一眼看到修什么。
   - **批次顶层新增 `flag_details`**：把本批涉及的所有 `rule_*` 标记展开成
     完整规则卡（`{flag, rule_id, category, forbid_zh, suggest_zh, note}`），
     省去 Agent 再跑一次 `rule list` 的来回；非规则标记（too_short 等）
     不展开（自解释）。
   - 修复写回用普通 `apply`；由于 §4.2 的"写回即重算"，修好的段标记自动
     清除，`--flagged` 列表自然缩短。

### 4.2 `apply` 命令增强：写回实时门禁（Gatekeeping）

在 `apply updates.json` 写入数据库时，默认对本次更新的每一段立即执行
`rules` 校验（`--no-gate` 跳过全部校验；`--force` 见下）：

1. **校验范围（补遗裁定）**：
   - **只对本次写入了译文字段的条目跑 gate**（写 `machine_zh` 就校
     `machine_zh`，写 `refined_zh` 就校 `refined_zh`，两者都写则都校）。
     只改 `status` / `error_flags` 的条目不跑 gate。
   - 遍历生效规则（`book_id IN ('*', 该书)` 且编译通过）：
     - 若 `en_pattern` 非空且该段 `en_text` 不匹配 → 跳过该规则；
     - 若 `bad_zh` 非空且被校译文匹配 `bad_zh` → 违规（`style` 类无
       `bad_zh`，天然不参与 gate）。
   - 同段同规则在一次 apply 中只计一次命中，`hit_count` 只 +1（防同段
     重复计数膨胀）。
2. **分级处置（补遗：裁定原稿的二选一，定为"拒写"方案）**：
   - **`severity == 'block'`（硬拦截）**：**该段本次完全不写库**——译文、
     状态、标记全都不动（含 `--claim` 场景下保持 wip，Agent 弃修时由
     `release` 兜底回收）。该段计入返回 JSON 的 `blocked` 与 `violations`。
     - 为什么拒写而不是"写入+打标"：库里**永远不存在** block 级坏译文，
       `export --fallback machine` 不可能带出毒段；审计轨迹已经在
       `updates/*.json`（AGENTS.md 硬规则 3），信息不丢；自愈循环语义
       最简单——看到 blocked 就只重译那几段再 apply。
     - **`--force` 逃生通道**：确认规则误伤（见 §9）时，`apply --force`
       照常写入并推进状态，但**强制打上 `rule_*` 标记**（qc/stats 可见，
       交付前人工复核），返回 JSON 计入 `forced` 列表。`--force` 只豁免
       block 拦截，不豁免 warn 打标。
   - **`severity == 'warn'`（软警告，如被动句式）**：正常写入并推进状态，
     自动追加 `error_flags = 'rule_syntax:#ID'`，返回 JSON 的 `warnings`
     中提示 Agent。
3. **标记生命周期闭环（补遗，这是自愈循环能转起来的关键）**：
   - apply 写入某段译文后，**当场对该段重算全部生效规则**：命中 → 打标
     （warn 级）并计入 warnings；未命中 → **清除该段全部 `rule_*` 标记**
     （通过 `merge_flags(old, RULE_OWNED_CODES, 新命中)`，不动 qc/glossary
     拥有的其他标记）。
   - 效果：`--flagged` 拉出的病灶段修好 apply 后标记自动消失，不需要手工
     传 `error_flags: ""`（README 中 v1.3.0 的手工清标方式仍然兼容）。
   - 用户显式传了 `error_flags` 的条目：先写用户值，再在其上做上述
     rule_* 增删合并（用户值优先于自动计算，rule_* 除外——规则标记的
     生杀权归门禁，防止 Agent 手滑把该留的标清了）。
4. **返回 JSON 与退出码（补遗：兼容性裁定）**：
   - **保留全部既有字段**（`book_id / applied / skipped / by_status`），
     新增 `blocked`（计数）、`forced`（计数）、`violations`、`warnings`。
     `skipped`（非法条目）与 `blocked`（门禁拦截）分开计，`applied` 不含
     被拦截段。
   - **退出码仍为 0**（与 `--allow-partial` 逐条跳过的既有哲学一致：逐条
     结果看 JSON，不靠退出码驱动流程），但 stderr 打一行醒目提示
     （如 `[transdb] 警告：2 段被规则拦截，详见 violations`）。
   - `violations` 按段聚合，一段可含多条规则命中：
   ```json
   {
     "book_id": "novel_p1",
     "applied": 19,
     "blocked": 1,
     "violations": [
       {
         "id": 102,
         "severity": "block",
         "flag": "rule_trap:2",
         "bad_span": "坐在银行上",
         "suggest_zh": "河岸/堤岸",
         "note": "河岸场景 bank 严禁译成银行",
         "action": "请立即针对 id=102 重新翻译并再次 apply"
       }
     ]
   }
   ```
   > **为什么这招对 Gemini 3.8 Flash 极度有效？**
   > Flash 模型在执行完 `apply` 工具调用后，紧接着就会读取 `stdout` 的 JSON 返回。看到 `blocked: 1` 和明确的 `action` 提示，它会在当前对话轮次顺手把第 102 段修好再 `apply` 一次，实现 **0 人工干预的即时自愈**！

### 4.3 `qc` 命令增强：整合 `rules` 表与句式反模式扫描

在 `python transdb.py qc BOOK` 中，除原有检查（长度比、标点截断、引号闭合、
badwords、俗套词）外，**默认并入 rules 表全书扫描**：

- 新增 `--rules-only`：仅跑 rules 表规则检查（跳过原有检查）。
- **标记生命周期与原有机制同构**：rules 检查结果的 drop-and-recompute 以
  `RULE_OWNED_CODES` 为 drop 集——每次 qc 重算 rule_* 标记，修好的段标记
  自动消失；qc 拥有的其他标记（too_short 等）不被 rules 重算波及，反之
  亦然。
- 命中条目统一写入 `error_flags`：`rule_trap:2` / `rule_badword:5` /
  `rule_syntax:7`（格式见 §2.1，命中词进 JSON 不进 flags）。
- 输出 `rows[]` 条目对规则命中新增 `rule_hits: [{flag, matched_span,
  suggest_zh, note}]`，与 glossary check 的 rows 一样一次给全返工所需信息。
- **补遗：qc 是全量兜底** —— `import --dual`（自带译文直入 translated）与
  `sync-md --write`（外部成品回写）都**不经过** apply 门禁，rules 只有在
  qc 全书扫描时才覆盖到它们。AGENTS.md 阶段三剧本因此必须保留 qc 终检，
  不能因为"apply 有门禁"就跳过。
- **补遗：顺手修复 v1.3.0 既有 bug** —— `badword:*` 目前不在
  `QC_OWNED_CODES` 里，词改掉后旧标记永久残留（`merge_flags` 只 drop
  QC_OWNED_CODES）。v1.4 把 `"badword"` 加进 `QC_OWNED_CODES`，badword
  标记随每次 qc 重算自愈，并在 README 标注这一行为变化。
- **补遗：hit_count** —— qc 打标时对命中的规则 `hit_count + 1`
  （段维度去重：同段同规则一次 qc 只 +1）。
- 性能注记见 §10（千段 × 百规则量级秒级完成，无需优化先行）。

---

## 5. 种子库（`rules` 预置条目）

> **v1.5.1 起修订**：种子内容不再随工具内置——规则词条与俗套词表属于使用方的
> 本地积累，公开仓库只保留工具机制。`rule init-defaults` 改为从本地种子文件
> `transdb_seeds.json` 导入（格式 `{"rules": [...], "cliches": [...]}`，rules
> 字段与 `rule load` 相同；示例见仓库内 `transdb_seeds.example.json`，
> 路径可用 `TRANSDB_SEEDS` 环境变量覆盖）。本节保留种子的设计准则与体例。

种子条目体例（示例为通用句式类；trap 类体例见 §2.2，具体词条自备）：

```python
[
    {
        "book_id": "*", "category": "syntax", "severity": "warn",
        "en_pattern": "",
        "bad_zh": r"被[^，。！？；：“”]{1,10}所[一-鿿]{1,4}",
        "good_zh": "删去“被…所…”，改为主动动词链或主谓顺承句",
        "note": "典型欧化被动句式，中文白描应化被动为主动"
    },
]
```

**种子误伤防范准则**（与 §9 同源）：

- `bad_zh` 避免超短高频子串（「把车」「手术」级别的一律不进 block）；
- 新种子一律 `warn` 起步，观察 `hit_count` 与实际命中率、确认精度后再
  `rule add`（去重更新）升 block——先 warn 后 block 是通用流程，种子自己
  也要遵守。

---

## 6. 配套 `AGENTS.md` 升级指南（针对 Antigravity + Gemini 3.8 Flash）

在工作区的 `AGENTS.md` 中，补充以下 **3 条针对 Gemini 3.8 Flash 的铁律**，
让工具与智能体形成闭环：

```markdown
### 警惕库与质量门禁铁律（v1.4 新增）

1. **必须遵循批次 `hints`**：
   调用 `next` 获取批次时，优先阅读 JSON 返回中的 `"hints"` 数组。若某段命中了 `hints` 中的
   `forbid_zh` 警示，翻译或精修该段时**严禁使用禁用译法**，必须采用 `suggest_zh` 的语境方向。
2. **遇到 `apply` 拦截立即单句返工**：
   执行 `apply updates/batch_XXXX.json` 后，检查返回的 JSON：
   - 若 `"blocked" > 0`（存在 `violations`），**不得继续拉取下一批**！必须立即根据
     `violations` 中的提示，结合上下文重新翻译报错的 `id`，落盘新的 updates JSON 再次
     `apply` 直至 `blocked` 为 0。
   - 若存在 `warnings`（如 `rule_syntax` 提示出现“被…所…”或“进行了…的…”翻译腔），在精修
     阶段必须将该句改写为自然的主动白描句。
   - **唯一例外**：反复确认译文正确、是规则误伤（如原文真的在写银行）时，用
     `apply --force` 写入并在进度汇报中说明原因，同时 `rule add`（去重更新）收窄该规则
     的 `--en`/`--bad` 或降级 `--severity warn`。禁止无脑连刷 --force。
3. **改稿经验随时入库**：
   当人类指出某处死译、俚语误译或句式问题时，除了用 `apply` 修改对应段落外，主动调用
   `python transdb.py rule add ...` 将该案例提炼为正则表达式规则存入数据库，确保后续章节
   不再重蹈覆辙。新规则默认 `--severity warn`，命中过且无误报后再升 block。
```

**补遗：AGENTS.md 其余改动清单（3 条铁律之外）**

1. **命令速查表**追加一行：
   ```bash
   python transdb.py rule list|add|remove|init-defaults ...   # 警惕库：查看/沉淀/删除/种子初始化
   ```
2. **阶段〇剧本**（入库）追加：新库初始化后跑一次 `rule init-defaults`
   （幂等，重复跑无害）。
3. **阶段三剧本**（质检交付）追加：句式/错译类病灶优先用
   `next 书目ID --flagged --n 10` 按批拉取（自带上下文与 `flag_details`
   规则卡）微创重写，替代逐段 `show` 排查；修完 `apply` 标记自动清除，
   再跑 `qc` 确认清零。
4. **硬规则区**不动（门禁不改硬规则 3 的"先落盘再 apply"要求——被拦截
   段的返工 JSON 同样要落盘，文件名如 `updates/fix_0102.json`）。

---

## 7. `selftest` 自测扩展（补遗：改为命名断言组）

现有 selftest 是 **32 项命名断言**（`check("名字", cond)`），没有"第 N 项"
编号体系——原稿"新增第 11、12 项"的说法与现状不符。v1.4 在其后**追加一组
`rules:` 前缀的命名断言**（沿用现有临时目录建库模式），至少覆盖：

1. `rules：add 正则编译失败被拒绝`——`--en '['` 与 `--bad '('` 各一例，断言
   die 且库中无该条。
2. `rules：add 去重键命中时更新而非重复插入`——同键二次 add，条数不变、
   severity 已更新、hit_count 保留。
3. `rules：init-defaults 幂等`——连跑两次，第二次 `inserted == 0`。
4. `rules：next 挂载 hints`——插入 `\bthe\s+bank\b` 规则后对含
   `He sat on the bank.` 的段 `next`，断言 `batches[0]["hints"]`
   非空且 `matched_ids` 正确、`forbid_zh/suggest_zh/note` 字段齐全。
5. `rules：badword/syntax（en 为空）不挂载 hints`。
6. `rules：hints 只扫 target_chunk`——规则只命中 context_before 里的段落时，
   `hints` 为空。
7. `rules：hints 超 10 条被截断`——插 12 条命中规则，断言 `len(hints)==10`
   且 `hints_truncated==2`。
8. `rules：apply block 拦截且不写库`——提交含 `"他坐在银行上。"` 的
   apply，断言返回 `blocked==1`、`violations[0].flag=="rule_trap:N"`、
   该段 `machine_zh` 仍为 NULL、状态未变、**退出码为 0**。
9. `rules：apply 放行`——改交 `"他坐在河岸上。"`，断言 `blocked==0`、
   写入成功且状态推进为 translated。
10. `rules：warn 打标不拦截`——提交含 `被他所说的话所打动` 的译文，断言
    写入成功、状态推进、`error_flags` 含 `rule_syntax:N`、返回 `warnings`。
11. `rules：修好重 apply 标记自动清除`——对第 10 条的段再 apply 主动句译文，
    断言 `rule_syntax` 标记消失、其他标记（如手工加的 name_mismatch）保留。
12. `rules：apply --force 强制写入并打标`——block 规则 + `--force`，断言
    写入、状态推进、`error_flags` 含 `rule_trap:N`、返回 `forced==1`。
13. `rules：apply --no-gate 跳过校验`——block 规则 + `--no-gate`，断言直接
    写入且无标记。
14. `rules：qc 打标与自愈`——rules 命中的段跑 `qc` 打标；把译文改成正确译法
    再跑 `qc`，断言标记清除。
15. `rules：badword 残留修复回归`——段带 `badword:手袋` 旧标记，译文改掉
    "手袋"后重跑 `qc`，断言旧标记消失（v1.3.0 此标记会永久残留）。
16. `rules：next --flagged 取病灶段`——对带标记段 `next --flagged`，断言
    `target_chunk[].error_flags` 非空、`flag_details` 展开规则卡、
    `--flagged --status pending` 组合报错。
17. `rules：hit_count 只在译后命中时递增`——`next` 挂载两次后 hit_count
    不变；apply 拦截 / warn / qc 打标各 +1。
18. `rules：坏正则运行时容错`——用 `sql --write` 直接塞一条编译不过的规则，
    断言 `next/apply/qc` 不崩、该规则被跳过、stderr 出现警告。

---

## 8. 兼容性与迁移（补遗）

1. **版本**：`VERSION = "1.4.0"`；`SCHEMA_VERSION` 1 → 2（该常量当前闲置，
   顺手对齐为新增 rules 表的语义，不在运行时强制校验老库）。
2. **老库迁移零操作**：`rules` 表经 `SCHEMA` executescript 在任意老库
   `open_db` 时自动补建；不碰 `books / paragraphs / glossary` 三表结构，
   `migrate_schema()` 无需新增逻辑（rules 是新表，无 ALTER 场景）。
3. **输出兼容**：`next` 只增 `hints` 字段、`apply` 只增
   `blocked/forced/violations/warnings` 字段，均不删改既有字段；所有命令
   退出码语义不变（apply 门禁拦截**不**改退出码，理由见 §4.2）。
4. **README.md 同步清单**（随 v1.4 一起改，不阻塞开发）：
   - §4 `error_flags` 标记表追加 `rule_trap:N / rule_badword:N / rule_syntax:N` 三行；
   - 命令手册新增 `rule` 命令族、`next --flagged`、`apply --force/--no-gate`、
     `qc --rules-only`；
   - 标注行为变化：badword 标记改为每次 qc 重算（§4.3）；
   - 工作流章节补"警惕库沉淀"一步（对应 AGENTS.md 铁律 3）。

---

## 9. 防误伤与防死循环（补遗：block 规则的精度准则）

apply 门禁 + "必须修到 blocked=0"的铁律是一把双刃剑：规则错了，Agent 会在
同一个段上无限返工。四道防线：

1. **block 规则三准则**（写进 `rule add` 的使用习惯，不强制程序校验）：
   - `en_pattern` 尽量带语境词收窄（场景词、动作搭配），别用裸单词；
   - `bad_zh` 避免超短高频子串（"把车"、"手术"级别的一律不进 block）；
   - **新规则先 warn 后 block**：`rule add --severity warn` 落地 → 跑一批
     观察 `hit_count` 与实际命中 → 确认无误报 → `rule add`（去重更新）
     升 block。CLI 默认 warn（§3.1）就是在制度上强制这条。
2. **逃生通道**：`apply --force`（§4.2）——人工/Agent 确认规则误伤时强制
   写入，但强制打标留痕，交付前 qc 复核可见，不会静默漏网。
3. **事后修正**：误伤规则当场 `rule add`（去重更新）收窄 `--en/--bad` 或
   降级 severity；彻底没救的 `rule remove --id`。
4. **观察窗口**：`rule list --top N` 按 hit_count 找最活跃规则，配合
   `qc --rules-only --dry-run` 抽查命中样本，定期人工复核高命中规则。

---

## 10. 性能与实现注记（补遗）

1. **编译缓存**：进程内一次性编译全部规则（`load_rules()` 返回
   `[(row, compiled_en, compiled_bad), ...]`），段循环里复用编译对象；
   `en_pattern` 一律 `re.IGNORECASE`，`bad_zh` 区分大小写（中文无影响，
   顺带让英文残留词的匹配更严格）。
2. **短路顺序**：trap 类先匹配 en（不中直接跳过 bad_zh）；全书 qc 用
   「段外循环 × 规则内循环」，命中即记录不早退（一段可中多条）。
3. **规模预估**：5000 段 × 200 规则 = 10^6 次正则调用，C 级正则引擎秒级
   完成；规则过千再考虑按 category/en 是否为空分组预筛，本轮不做。
4. **hints 组装时机**：`make_batches()` 之后、`--claim` 事务之外（纯读 +
   不写 hit_count），不给并发 claim 添乱。
5. **error_flags 写入**：所有规则打标走既有 `merge_flags()`，与
   qc/glossary 的标记互不踩踏（各自的 OWNED_CODES 做 drop 集）。
