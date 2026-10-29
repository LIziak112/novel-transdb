# AGENTS.md —— 翻译任务执行规则（本文件给智能体读）

你在本工作区的职责是长篇小说翻译流水线。**所有中间产物（原文、机翻、精修稿、
进度、术语）一律存取于 SQLite 数据库，通过 `transdb.py` 操作。**

## 硬规则（违反任何一条都算任务失败）

1. **禁止**创建/读写任何中间 txt、md、csv 来存放段落译文或进度。段落数据只活在数据库里。
2. 取任务只用 `next`，写回只用 `apply`（JSON 文件）。不要手拼 SQL 写库。
3. `apply` 的 JSON 必须先落盘到 `updates/` 目录再执行（文件名带批次号，如
   `updates/mt_0200_0219.json`），这既是操作日志也是审计记录。
   **禁止** `echo … | apply -`、把 JSON 内联进命令行、任何管道写回——Windows
   PowerShell 5.1 的管道按 ASCII 重编码会损坏中文，命令行引号转义也极易出错。
4. 每完成一个批次必须运行 `stats` 并向用户汇报进度（已完成 X / 总 Y 段）。
5. 人名、地名、专有名词的译名必须与 `glossary list` 一致；遇到新术语先
   `glossary add` 登记再翻译。
6. 全局改译名必须用 `replace --if-en`（先 `--dry-run` 预览，给用户看过再实执行），
   禁止按段落循环手工改。
7. 导出交付前必须：`glossary check` 为 0、`qc` 标记清零（或用户明确豁免）、
   导出 JSON 摘要中 `missing` 为 0。缺一段都不算交付。
8. 任何时刻不确定状态，先跑 `stats` / `books`，不要猜。
9. `next` 返回的 `context_before` / `context_after` **仅供参考理解：绝不翻译、绝不写回、
   绝不出现在 updates JSON 里**。只翻译 `target_chunk` 里的段，`id` 原样带回。

## 命令速查（默认库 translations.db，在项目根目录执行）

```bash
python transdb.py next 书目ID --n 20 --max-chars 6000 [--status translated]  # 取滑动窗口批次
python transdb.py next 书目ID --flagged --n 10                    # 取带质检标记的病灶段
python transdb.py apply updates/xxx.json [--force|--no-gate]      # 写回（状态自动推进，id 即段号）
python transdb.py stats 书目ID                                  # 进度
python transdb.py show 书目ID --index N | --from A --to B       # 看段落全文/上下文
python transdb.py search 书目ID --zh 词 / --en word             # 检索
python transdb.py glossary list 书目ID                          # 术语表
python transdb.py rule list|add|remove|init-defaults [--book 书目ID]  # 警惕库管理
python transdb.py issue list|add|promote|ignore ...              # 错题池：记错例/复盘提炼转正
python transdb.py qc 书目ID [--rules-only]                       # 质检（默认含规则扫描）
python transdb.py export 书目ID --field refined --fallback none --out 成品/书名.md
```

## 工作剧本

### 阶段〇 · 入库（用户给新原文时）
- 新库初始化后先跑一次 `rule init-defaults`（幂等，从本地 `transdb_seeds.json`
  导入种子规则；文件缺失时把指引报给用户，**不要自造规则**）；
- 纯英文生肉：`import 原文.txt --book 书目ID`，报告段数、编码、标题段数。
- 双语对照稿（文件名含 dual / 用户说明是英中对照）：加 `--dual`。
  自带译文直接入 translated；`pending` 段 = 原稿漏译，把数量报给用户。
  入库报告若出现 `suspect_en` > 0，先用 `show` 抽查几个该类段确认没配对错位。

**dual 导入的剧本**：跳过阶段一。先用阶段一剧本把 pending（原稿漏译）段补机翻
（写 `machine_zh`），pending 清零后直接进阶段二精修。

### 阶段一 · 机翻
循环直到 `pending` 清零：
1. `next 书目ID --n 20 --max-chars 6000` 取滑动窗口批次；
2. 参照 `context_before`/`context_after` 理解剧情衔接（它们不是翻译对象），
   按【译文风格要求】翻译 `target_chunk` 每段，产出 `updates/mt_起_止.json`
   （每条：`{"id": N, "machine_zh": "……"}`，id 原样取自 target_chunk）；
3. `apply` 写回 → `stats` 汇报。
中断/报错后：直接重复本剧本，工具自动从未完成处续传，不要自行排查「上次到哪了」。

### 阶段二 · 精修
循环直到 `translated` 清零：
1. `next 书目ID --status translated --n 10 --max-chars 6000`（批次自带机翻稿和上下文）；
2. 需要更多上下文时 `show 书目ID --from N-2 --to N+2`；
3. 产出 `{"id": N, "refined_zh": "……"}` 的 JSON，apply 写回。修正了明显的机翻
   死译（俚语错译、多义词选错义项）、恶性被动句，或写出了惊艳的白描改写时，
   按【警惕库铁律】第 4 条在同一条目顺手加 `issue_*` 错题字段（每批至多 3 条）；
4. 需要给段落加译注（术语释义、文化背景）时在同一条更新里加
   `"notes": "……"`：译注存独立字段，导出为文末脚注，**不写进正文**——
   正文保持纯净，质检也不会把译注当机翻杂质误报。

### 阶段三 · 质检交付
1. `glossary check 书目ID`：对每个 `name_mismatch`，用 `show` 看原文，确认正确译名，
   属于错译的用 `apply` 改写该段，属于可全局统一的先问用户再 `replace --if-en`；
2. `qc 书目ID`：对 `too_short`/`too_long`/`no_chinese` 段逐段核对原文补译或修剪；
   `badword:*` 段按【译文风格要求】改写；
3. `rule_*:*`（警惕规则命中）与句式病灶优先用 `next 书目ID --flagged --n 10`
   按批拉取（自带上下文与 `flag_details` 规则卡）微创重写，修完 `apply`
   写回标记即自动清除；
4. 修完再跑一遍 qc 确认清零（修好的段重跑 qc 标记会自动消失）；
5. `issue list --status open` 查错题池积压：有 open 条目就向用户报告数量
   （"已积累 N 条待提炼错题"），**等用户决定**是否复盘提炼，不要自行转正；
6. `export 书目ID --field refined --fallback none --out 成品/书名.md`，
   确认 JSON 摘要 `missing: 0` 后交付文件路径。

## 警惕库与质量门禁铁律（v1.4 新增，v1.5 扩充错题池）

1. **必须遵循批次 `hints`**：调用 `next` 获取批次时，优先阅读 JSON 返回中的
   `"hints"` 数组。若某段命中了 `hints` 中的 `forbid_zh` 警示，翻译或精修该段时
   **严禁使用禁用译法**，必须采用 `suggest_zh` 的语境方向。
2. **遇到 `apply` 拦截立即单句返工**：执行 `apply` 后检查返回的 JSON：
   - 若 `"blocked" > 0`（存在 `violations`），**不得继续拉取下一批**！必须立即
     根据 `violations` 的提示，结合上下文重新翻译报错的 `id`，落盘新的
     updates JSON（如 `updates/fix_0102.json`）再次 `apply`，直至 `blocked` 为 0；
   - 若存在 `warnings`（如 `rule_syntax` 提示"被…所…""进行了…的…"翻译腔），
     在精修阶段必须将该句改写为自然的主动白描句；
   - **唯一例外**：反复确认译文正确、确属规则误伤（如原文真的在写银行）时，
     用 `apply --force` 写入并在进度汇报中说明原因，同时 `rule add`（去重更新）
     收窄该规则的 `--en`/`--bad` 或降级 `--severity warn`。禁止无脑连刷 --force。
3. **改稿经验双入口入库**（判据看错误是谁发现的）：
   - **人类指出的错译**（死译、俚语误译、句式问题）：除 `apply` 修改对应段落外，
     直接调用 `python transdb.py rule add ...` 提炼为正则规则（PowerShell 里正则
     参数用**单引号**包裹防转义）。有人背书，高置信，立即转正；
   - **自查发现的问题**（精修中修正的机翻死译、恶性被动句、惊艳白描）：
     **翻译途中不写正则**，按第 4 条顺手入错题池，留给复盘批量提炼。
   判据：全书固定译名进 `glossary`，语境歧义（同词不同译）进 `rule`，
   语境歧义的**原始样本**先进 `issue` 池。新规则一律默认 `--severity warn`，
   命中过且无误报后再升 block。
4. **错题池记账纪律**（v1.5）：精修/替换途中发现值得沉淀的案例，在 updates
   JSON 对应条目上顺手加三个短字段（系统自动连旧译文快照一起入池，不占额外命令）：
   ```json
   {"id": 42, "refined_zh": "……",
    "issue_cat": "trap", "issue_en": "bank", "issue_note": "此处指河岸，原机翻误译为银行"}
   ```
   - 只记**类别明确**的错误（`trap` 词义/俚语、`syntax` 句式/被动、`badword`
     禁词）和真正惊艳的改写（`style`），每批至多 3 条，别把普通润色当错题；
   - `issue_en` **必须点出英文触发词**（几个词即可）——它既是展示截句的锚点，
     也是事后提炼 `en_pattern` 的原材料；
   - 批量替换顺手留档用 `replace --log-issue "备注"`（自动采样首个命中段）；
   - **提炼转正只在用户发出「提炼临时库」指令时进行**：
     `issue list --status open` → 归纳共性 → 复发 ≥2 次的用
     `issue promote --id 15,16 --en '..' --bad '..' --good '..'` 转正（默认
     warn + 单书作用域，看返回的 `preview` 命中面校准正则），偶发的
     `issue ignore --id N`。

## 译文风格要求（用户维护区）

<!-- 在下面粘贴你的风格要求。示例： -->

<!--
- 文风：白描、克制、口语自然；不堆形容词，不写欧化长句。
- 人名统一走术语表（如 Eve=伊芙，brandy=白兰地）；译名拿不准先问。
- 对话保留语气词，引号用中文全角“……”。
- 机翻腔禁用词（命中即重写）：手袋、兜帽、……
-->

【在此粘贴你的译文风格要求】
