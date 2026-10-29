#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transdb —— 长篇小说「原文 → 机翻 → 精修」流水线数据库工具
================================================================
以本地 SQLite 为唯一中枢：一次入库，段落级更新、断点续传、
全局术语替换、质检过滤、一键导出，彻底告别大 txt 的反复拆分与拼接。

零依赖：仅需 Python 3.8+ 标准库。

用法概览（详见 README.md）：
    python transdb.py import 原文.txt --book book1          # 1. 入库
    python transdb.py next book1 --n 20                      # 2. 取待处理批次（JSON）
    python transdb.py apply updates.json                     # 3. 写回译文（JSON，原子事务）
    python transdb.py stats book1                            #    查看进度
    python transdb.py replace book1 --find 夏娃 --replace 伊芙 --if-en Eve
    python transdb.py issue list --status open              # 错题池复盘（issue promote 提炼转正）
    python transdb.py export book1 --field refined --fallback machine --out 成品.md

输出约定：数据结果（JSON）一律打印到 stdout，人类可读日志走 stderr，
方便智能体（Antigravity / Gemini 等）直接解析 stdout。
"""

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "1.5.1"
SCHEMA_VERSION = 3
VALID_STATUS = ("pending", "translated", "refined", "wip", "empty")
ISSUE_STATUSES = ("open", "promoted", "ignored")  # 错题池状态（promoted 已转正 rules）
QC_OWNED_CODES = {"too_short", "too_long", "no_chinese", "empty_target",
                  "truncated", "unclosed_quote", "webnovel_cliche", "badword"}
RULE_OWNED_CODES = {"rule_trap", "rule_badword", "rule_syntax"}  # rules 表拥有的标记前缀
RULE_CATEGORIES = ("trap", "badword", "syntax", "style")
HINTS_MAX = 10  # next 每批 hints 注入上限（防规则库膨胀撑爆上下文）
AFTER_CHARS = 300  # context_after 每段截取的开头字符数
ZH_END_PUNCT = "。！？!?…；;：:．.）)］]】》〉」』”’\"'—～~"  # 合法句末/段末字符
QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ('"', '"'))
# v1.5.1：工具不再内置任何规则/词表数据（DEFAULT_RULES / WEBNOVEL_CLICHES 已移除）。
# 种子属于使用方的本地积累，经 transdb_seeds.json 加载（见 load_seeds）。

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id       TEXT PRIMARY KEY,
    source_file   TEXT,
    para_count    INTEGER,
    split_mode    TEXT,
    encoding      TEXT,
    qc_badwords   TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS paragraphs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id      TEXT NOT NULL,
    para_index   INTEGER NOT NULL,
    is_heading   INTEGER DEFAULT 0,
    en_text      TEXT,
    machine_zh   TEXT,
    refined_zh   TEXT,
    notes        TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    error_flags  TEXT DEFAULT '',
    claimed_at   TEXT,
    updated_at   TEXT,
    UNIQUE (book_id, para_index)
);
CREATE INDEX IF NOT EXISTS idx_para_status ON paragraphs (book_id, status, para_index);
CREATE TABLE IF NOT EXISTS glossary (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id      TEXT NOT NULL,
    en           TEXT NOT NULL,
    zh           TEXT NOT NULL,
    note         TEXT DEFAULT '',
    forbidden_zh TEXT DEFAULT '',
    UNIQUE (book_id, en)
);
CREATE TABLE IF NOT EXISTS rules (
    rule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     TEXT NOT NULL DEFAULT '*',  -- '*' 全局通用，具体 book_id 单书专属
    category    TEXT NOT NULL DEFAULT 'trap', -- trap|badword|syntax|style
    en_pattern  TEXT DEFAULT '',            -- 英文触发正则（空 = 纯中文规则；匹配一律 IGNORECASE）
    bad_zh      TEXT DEFAULT '',            -- 禁用/劣质中文正则（空 = 仅译前提示，不译后拦截）
    good_zh     TEXT DEFAULT '',            -- 推荐译法或正面改写示范
    note        TEXT DEFAULT '',            -- 给 Agent 看的简短原因
    severity    TEXT NOT NULL DEFAULT 'block', -- block=写回拦截 warn=写入打标 hint=仅译前挂载
    hit_count   INTEGER NOT NULL DEFAULT 0, -- 累计译后命中次数（译前挂载不计）
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (book_id, category, en_pattern, bad_zh)
);
CREATE INDEX IF NOT EXISTS idx_rules_book ON rules(book_id);
CREATE TABLE IF NOT EXISTS issue_staging (
    issue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     TEXT NOT NULL,
    para_index  INTEGER,                    -- 关联段落号（复盘需更多语境时 show 取整段）
    category    TEXT DEFAULT 'trap',        -- 同 rules 四类：trap|badword|syntax|style
    issue_en    TEXT DEFAULT '',            -- 英文触发词（展示层截句的锚点）
    en_text     TEXT DEFAULT '',            -- 英文段全文快照（写入即冻结）
    bad_zh      TEXT DEFAULT '',            -- 被替换的旧译文全文快照（冻结）
    good_zh     TEXT DEFAULT '',            -- 新译文全文快照（冻结）
    note        TEXT DEFAULT '',            -- 一句话病因分析（promote 默认继承为规则 note）
    status      TEXT NOT NULL DEFAULT 'open', -- open(待提炼) | promoted(已转正) | ignored
    rule_id     INTEGER,                    -- 转正后回填 rules.rule_id
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON issue_staging(status, book_id);
"""

HEADING_RE = re.compile(
    r"^(chapter\b|第\s*[\d一二三四五六七八九十百千零两]+\s*[章回节卷篇部]|prologue\b|"
    r"epilogue\b|前言\b|序章\b|楔子\b|尾声\b|后记\b|番外\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 基础工具

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def info(msg):
    print(f"[transdb] {msg}", file=sys.stderr)


def die(msg, code=1):
    print(f"[transdb] 错误：{msg}", file=sys.stderr)
    sys.exit(code)


def force_utf8_stdio():
    # Windows 控制台/管道默认可能是 GBK(cp936)。stdout/stderr 强制 UTF-8 防乱码崩溃；
    # stdin 强制 UTF-8 且 strict：apply - 从管道读 JSON 时，坏编码宁可立刻报错，
    # 也绝不把乱码译文写进数据库（PowerShell 5.1 管道会把中文预损坏成 '?'）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass


def db_path(a):
    return getattr(a, "db", None) or os.environ.get("TRANSDB_DB") or "translations.db"


def load_seeds(path=None):
    """读种子文件（v1.5.1 起工具零内置数据）。
    格式：{"rules": [规则对象…（同 rule load）], "cliches": ["俗套词", …]}。
    默认 ./transdb_seeds.json，可用 TRANSDB_SEEDS 环境变量改路径。
    文件不存在返回 (None, 路径)，由调用方决定 die 还是跳过。"""
    p = Path(path or os.environ.get("TRANSDB_SEEDS") or "transdb_seeds.json")
    if not p.is_file():
        return None, p
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        die(f"种子文件解析失败（{p}）：{e}")
    if not isinstance(data, dict):
        die(f"种子文件应为 JSON 对象：{p}（{{\"rules\": [...], \"cliches\": [...]}}）")
    rules, cliches = data.get("rules", []), data.get("cliches", [])
    if not isinstance(rules, list) or not isinstance(cliches, list):
        die(f"种子文件的 rules / cliches 字段应为数组：{p}")
    return {"rules": rules, "cliches": [str(c) for c in cliches]}, p


def open_db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.create_function("regexp", 2, _sql_regexp)
    migrate_schema(conn)
    conn.commit()
    return conn


def migrate_schema(conn):
    """老库平滑升级（幂等）：补齐后加的列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(glossary)")}
    if cols and "forbidden_zh" not in cols:
        conn.execute("ALTER TABLE glossary ADD COLUMN forbidden_zh TEXT DEFAULT ''")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(paragraphs)")}
    if pcols and "notes" not in pcols:
        conn.execute("ALTER TABLE paragraphs ADD COLUMN notes TEXT DEFAULT ''")


def _sql_regexp(pattern, subject):
    # SQLite 的 REGEXP 运算符需自注册函数；术语表词界匹配用它
    if pattern is None or subject is None:
        return 0
    return 1 if re.search(pattern, subject) else 0


def term_pattern(term):
    """英文术语 → 词界正则（供 REGEXP 用）。
    (?<!\\w)/(?!\\w) 保证整词匹配：Eve 不再误伤 evening/seven/Never。
    术语本身含大写字母时区分大小写（专有名词 Prince 不吃普通词 prince），
    全小写术语则不区分大小写。"""
    pat = r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)"
    if not any(c.isupper() for c in term):
        pat = "(?i)" + pat
    return pat


def book_exists(conn, book_id):
    return conn.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone() is not None


def require_book(conn, book_id):
    if not book_exists(conn, book_id):
        die(f"书目不存在：{book_id}（先运行 books 查看书目列表）")


def read_text_auto(path):
    """按 utf-8-sig / utf-8 / gb18030 / utf-16 依次尝试解码，返回 (文本, 编码名)。"""
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates = ("utf-16",)
    elif data[:3] == b"\xef\xbb\xbf":
        candidates = ("utf-8-sig",)
    else:
        candidates = ("utf-8", "gb18030", "utf-16")
    for enc in candidates:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in text:  # 错误编码解出的内容常带 NUL
            continue
        return text, enc
    die("无法识别文件编码（尝试过 utf-8 / gb18030 / utf-16），请先转存为 UTF-8")


def looks_heading(p):
    if not p or len(p) > 80:
        return 0
    if HEADING_RE.match(p):
        return 1
    # 全大写短行也视为标题（如 "THE RAIN"）
    letters = [c for c in p if c.isalpha()]
    if letters and 3 < len(p) < 60 and p.upper() == p and any(c.isascii() for c in letters):
        return 1
    return 0


def parse_paragraphs(text, mode="blank", join=True, drop_nums=True):
    """把整书文本解析为段落列表。返回 (段落数据, 丢弃的页码行数)。
    blank 模式：空行分段（英文硬换行默认合并为一段）；line 模式：每个非空行一段。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("﻿")
    if mode == "line":
        blocks = [ln.strip() for ln in text.split("\n")]
    else:
        blocks = re.split(r"\n[ \t]*\n+", text)
        if join:
            blocks = [re.sub(r"[ \t]*\n[ \t]*", " ", b) for b in blocks]
        blocks = [b.strip() for b in blocks]
    paras, dropped = [], 0
    for b in blocks:
        if not b:
            continue
        if drop_nums and re.fullmatch(r"\d{1,4}", b):
            dropped += 1
            continue
        paras.append(b)
    return paras, dropped


CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z]")


def is_zh_para(p):
    """按 CJK/拉丁字母数量判定段落语言（含英文名的中文译文段仍判为中文）。"""
    cjk = len(CJK_RE.findall(p))
    latin = len(LATIN_RE.findall(p))
    return cjk > 0 and cjk >= latin


def join_lines(lines):
    """段内硬换行合并：CJK 边界直接拼接（中文不留空格），其余以空格相连。"""
    if not lines:
        return None
    out = lines[0]
    for ln in lines[1:]:
        sep = "" if (CJK_RE.search(out[-1]) and CJK_RE.search(ln[0])) else " "
        out += sep + ln
    return out


def parse_dual_stream(text, drop_nums=True):
    """双语稿 → ((en|None, zh|None) 段流, 丢弃的页码行数)。

    空行分块后，块内按『连续同语言行』成段：含汉字的行属译文，不含的属原文。
    同一块内先英文后中文 → 拆成 (en, zh) 两段；整块无汉字 → (en, None)；
    整块都是中文 → (None, zh)。因此同时兼容三种底本格式：
    英中同块成对（对间空行）、英中各占一段（段间空行）、全文无空行逐行交替。
    要求英文在前、中文译文在后。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    stream, dropped = [], 0
    for block in re.split(r"\n[ \t]*\n+", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if drop_nums:
            kept = [ln for ln in lines if not re.fullmatch(r"\d{1,4}", ln)]
            dropped += len(lines) - len(kept)
            lines = kept
        if not lines:
            continue
        runs = []  # [is_zh, [同行语言的连续行]]
        for ln in lines:
            zh = bool(CJK_RE.search(ln))
            if runs and runs[-1][0] == zh:
                runs[-1][1].append(ln)
            else:
                runs.append((zh, [ln]))
        for zh, lns in runs:
            stream.append((None, join_lines(lns)) if zh else (join_lines(lns), None))
    return stream, dropped


def pair_dual_stream(stream):
    """(en|None, zh|None) 段流 → (pairs, merged, orphan)。pairs 为 (en, zh|None)：
    英文段找紧跟的中文段配对；原稿漏译处 zh 为 None，不级联错位；一个英文段的
    译文被拆成多个中文段时合并回同一对（merged）；开头找不到原文的中文段计
    orphan。"""
    pairs, merged, orphan = [], 0, 0
    pending_en = None
    for en, zh in stream:
        if zh is not None:
            if pending_en is not None:
                pairs.append((pending_en, zh))
                pending_en = None
            elif pairs and pairs[-1][1]:
                pairs[-1] = (pairs[-1][0], pairs[-1][1] + "\n" + zh)
                merged += 1
            else:
                orphan += 1
        else:
            if pending_en is not None:
                pairs.append((pending_en, None))  # 上一个英文段没等到译文
            pending_en = en
    if pending_en is not None:
        pairs.append((pending_en, None))
    return pairs, merged, orphan


def snippet(s, n=160):
    if s is None:
        return None
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def merge_flags(old_flags, drop_codes, add_codes):
    """重算某类质检标记时保留其他标记。drop_codes 里的旧码清除，加入 add_codes。"""
    existing = {f for f in (old_flags or "").split(",") if f}
    kept = {f for f in existing if f not in drop_codes and not any(
        f.startswith(c + ":") for c in drop_codes)}
    return ",".join(sorted(kept | set(add_codes))) if kept | set(add_codes) else ""


# ---------------------------------------------------------------- 规则引擎（rules 表）

def load_rules(conn, book_id):
    """加载生效规则（book_id='*' 全局 + 本书专属），正则编译一次复用。
    库里的坏正则（如被 sql --write 手工塞入的）跳过并警告一次，绝不弄挂流水线。"""
    rows = conn.execute(
        "SELECT rule_id, category, en_pattern, bad_zh, good_zh, note, severity,"
        " hit_count FROM rules WHERE book_id IN ('*', ?) ORDER BY rule_id",
        (book_id,)).fetchall()
    rules = []
    for r in rows:
        en_rx = bad_rx = None
        if r["en_pattern"]:
            try:
                en_rx = re.compile(r["en_pattern"], re.IGNORECASE)
            except re.error as e:
                info(f"警告：规则 #{r['rule_id']} en_pattern 编译失败，已跳过：{e}")
                continue
        if r["bad_zh"]:
            try:
                bad_rx = re.compile(r["bad_zh"])
            except re.error as e:
                info(f"警告：规则 #{r['rule_id']} bad_zh 编译失败，已跳过：{e}")
                continue
        rules.append({"id": r["rule_id"], "category": r["category"],
                      "en_pattern": r["en_pattern"], "bad_zh": r["bad_zh"],
                      "good_zh": r["good_zh"] or "", "note": r["note"] or "",
                      "severity": r["severity"], "hit_count": r["hit_count"] or 0,
                      "en": en_rx, "bad": bad_rx})
    return rules


def check_rule_hits(rules, en_text, zh_text):
    """对一段译文跑全部生效规则的译后校验，返回命中列表。
    trap 类先过英文门控（en 不匹配直接跳过）；hint 级与 style 类（无 bad_zh）不参与。"""
    hits = []
    if not zh_text:
        return hits
    for r in rules:
        if r["bad"] is None or r["severity"] == "hint":
            continue
        if r["en"] is not None and not r["en"].search(en_text or ""):
            continue
        m = r["bad"].search(zh_text)
        if m:
            hits.append({"rule_id": r["id"], "category": r["category"],
                         "severity": r["severity"], "flag": f"rule_{r['category']}:{r['id']}",
                         "bad_span": snippet(m.group(0), 40),
                         "suggest_zh": r["good_zh"], "note": r["note"]})
    return hits


def build_hints(rules, batch):
    """按 target_chunk 的 en_text 组装译前 hints（只扫翻译对象，不扫上下文）。
    按 (hit_count 降序, rule_id 升序) 排序，超 HINTS_MAX 截断。纯读、不写库。"""
    hints = []
    for r in sorted(rules, key=lambda x: (-x["hit_count"], x["id"])):
        if r["en"] is None:
            continue
        matched = [t["id"] for t in batch["target_chunk"] if r["en"].search(t["en"] or "")]
        if matched:
            hints.append({"rule_id": r["id"], "category": r["category"],
                          "severity": r["severity"], "matched_ids": matched,
                          "en_pattern": r["en_pattern"], "forbid_zh": r["bad_zh"] or "",
                          "suggest_zh": r["good_zh"], "note": r["note"]})
    out = {}
    if len(hints) > HINTS_MAX:
        out["hints_truncated"] = len(hints) - HINTS_MAX
        hints = hints[:HINTS_MAX]
    if hints:
        out["hints"] = hints
    return out


def build_flag_details(rules, batch):
    """把批次里出现的 rule_* 标记展开成完整规则卡（规则已删则跳过），
    省掉 Agent 再跑一次 rule list 的来回。"""
    details, seen = [], set()
    for t in batch["target_chunk"]:
        for f in (t.get("error_flags") or "").split(","):
            f = f.strip()
            if not f.startswith(("rule_trap:", "rule_badword:", "rule_syntax:")) or f in seen:
                continue
            seen.add(f)
            try:
                rid = int(f.split(":", 1)[1])
            except ValueError:
                continue
            for r in rules:
                if r["id"] == rid:
                    details.append({"flag": f, "rule_id": rid, "category": r["category"],
                                    "forbid_zh": r["bad_zh"] or "",
                                    "suggest_zh": r["good_zh"], "note": r["note"]})
                    break
    return details


# ---------------------------------------------------------------- 错题池（issue_staging）

SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def stage_issue(conn, book, para_index, category, issue_en, en_text, bad_zh, good_zh, note):
    """错题入池：三个文本字段一律存全文快照，写入即冻结——段落后续再怎么改
    （二次精修/replace/sync-md）都不影响这条证据（v1.5 裁定 #9）。"""
    if category not in RULE_CATEGORIES:
        info(f"警告：错题 category 非法（{category}），未入池（可选 {RULE_CATEGORIES}）")
        return None
    cur = conn.execute(
        "INSERT INTO issue_staging (book_id, para_index, category, issue_en, en_text,"
        " bad_zh, good_zh, note) VALUES (?,?,?,?,?,?,?,?)",
        (book, para_index, category, issue_en or "", en_text or "",
         bad_zh or "", good_zh or "", note or ""))
    return cur.lastrowid


def anchor_excerpt(text, anchor, n=180):
    """展示层病灶句提取：优先返回包含触发词（锚点）的句子，否则按长度截断。
    存储层永远存全文快照，截断只发生在 issue list 的展示上（裁定 #7）。
    断句是近似切分（对白/缩写会切歪），但触发词必落在切出的片段内。"""
    if not text:
        return ""
    if anchor:
        alow = anchor.lower()
        for seg in SENT_SPLIT_RE.split(text):
            if alow in seg.lower():
                return seg.strip() if len(seg) <= n else snippet(seg, n)
    return snippet(text, n)


def parse_id_list(raw, flag="id"):
    """'15,16,18' → [15,16,18]；非法或为空直接 die。"""
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        die(f"--{flag} 需要逗号分隔的整数，如 --{flag} 15,16,18")
    if not ids:
        die(f"--{flag} 为空")
    return ids


def preview_rule_hits(conn, book, en_pat, bad_pat):
    """候选规则对来源书目的命中面预览（promote 返回，复盘当场校准正则）：
    en_matched = 原文命中数（hints 挂载面）；flagged = 现有译文也将命中 bad_zh
    的段数（= 将被打标/拦截的面）；纯中文规则只统计 flagged。"""
    en_rx = bad_rx = None
    if en_pat:
        en_rx = re.compile(en_pat, re.IGNORECASE)
    if bad_pat:
        bad_rx = re.compile(bad_pat)
    en_n = flagged = 0
    samples = []
    for r in conn.execute(
            "SELECT para_index, en_text, machine_zh, refined_zh FROM paragraphs"
            " WHERE book_id=? ORDER BY para_index", (book,)).fetchall():
        zh = r["refined_zh"] or r["machine_zh"] or ""
        en_ok = en_rx is None or bool(en_rx.search(r["en_text"] or ""))
        if en_rx is not None and en_ok:
            en_n += 1
        if en_ok and bad_rx is not None and zh and bad_rx.search(zh):
            flagged += 1
            if len(samples) < 3:
                samples.append(r["para_index"])
    out = {"book": book}
    if en_rx is not None:
        out["en_matched"] = en_n
    out["flagged"] = flagged
    if samples:
        out["sample_ids"] = samples
    return out


# ---------------------------------------------------------------- 命令：init / import / books / stats

def cmd_init(a):
    path = db_path(a)
    conn = open_db(path)
    n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    info(f"数据库已就绪：{Path(path).resolve()}（transdb v{VERSION}，已有书目 {n} 个）")
    out_json({"db": str(Path(path).resolve()), "version": VERSION, "books": n})


def cmd_import(a):
    src = Path(a.source)
    if not src.is_file():
        die(f"源文件不存在：{src}")
    text, enc = read_text_auto(src)
    dual_pairs = dual_merged = dual_orphan = dual_dropped = suspect_en = suspect_zh = 0
    paras = None
    if a.dual:
        if a.split != "blank" or a.no_join:
            info("dual 模式使用块级解析（空行分块 + 块内按语言切分），--split/--no-join 不生效")
        stream, dual_dropped = parse_dual_stream(text, drop_nums=not a.keep_numbers)
        dual_pairs, dual_merged, dual_orphan = pair_dual_stream(stream)
        if not dual_pairs:
            die("dual 模式没配出任何段落：文件里找不到英文段。"
                "dual 要求英文段在前、中文译文段在后")
        suspect_en = sum(1 for en, _ in dual_pairs if CJK_RE.search(en or ""))
        suspect_zh = sum(1 for _, zh in dual_pairs if zh and not is_zh_para(zh))
    else:
        paras, dropped = parse_paragraphs(text, mode=a.split, join=not a.no_join,
                                          drop_nums=not a.keep_numbers)
        if not paras:
            die("解析结果为 0 段：请检查文件内容，或改用 --split line（每行一段）")
    conn = open_db(db_path(a))
    if book_exists(conn, a.book):
        if a.force:
            conn.execute("DELETE FROM paragraphs WHERE book_id=?", (a.book,))
            conn.execute("DELETE FROM books WHERE book_id=?", (a.book,))
            conn.commit()
            info(f"--force：已清除书目 {a.book} 的旧数据（术语表 glossary 保留）")
        else:
            die(f"书目 {a.book} 已存在。确认要推翻重来请加 --force（将删除该书全部段落与译文）")
    ts = now_iso()
    headings = 0
    rows = []
    translated_n = 0
    if a.dual:
        for i, (en, zh) in enumerate(dual_pairs):
            h = looks_heading(en)
            headings += h
            translated_n += bool(zh)
            rows.append((a.book, i, h, en, zh, None,
                         "translated" if zh else "pending", "", None, ts))
    else:
        for i, p in enumerate(paras):
            h = looks_heading(p)
            headings += h
            rows.append((a.book, i, h, p, None, None, "pending", "", None, ts))
    conn.executemany(
        "INSERT INTO paragraphs (book_id, para_index, is_heading, en_text, machine_zh,"
        " refined_zh, status, error_flags, claimed_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO books (book_id, source_file, para_count, split_mode, encoding,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (a.book, str(src), len(rows), "dual" if a.dual else a.split, enc, ts, ts))
    conn.commit()
    if a.dual:
        info(f"双语稿入库：{a.book} 共 {len(rows)} 段（自带译文 {translated_n} 段直接 "
             f"translated，原稿漏译 {len(rows) - translated_n} 段 pending 待补译，"
             f"丢弃页码行 {dual_dropped} 个）")
        if dual_merged:
            info(f"提示：{dual_merged} 个连续中文段已并入前一对（疑为一段英文的译文被拆成多段）")
        if dual_orphan:
            info(f"警告：跳过 {dual_orphan} 个找不到原文的中文段（通常在文件开头）")
        if suspect_en:
            info(f"警告：{suspect_en} 个『英文』段内含汉字，可能切分有误，建议 show 抽查")
        if suspect_zh:
            info(f"警告：{suspect_zh} 个『译文』段几乎没有汉字，可能切分有误，建议 show 抽查")
    else:
        info(f"入库完成：{a.book} 共 {len(paras)} 段（标题 {headings} 段，"
             f"丢弃页码行 {dropped} 个）")
    if a.dual:
        out_json({"book_id": a.book, "paragraphs": len(rows), "headings": headings,
                  "translated": translated_n,
                  "missing_translation": len(rows) - translated_n,
                  "merged_zh": dual_merged, "orphan_zh": dual_orphan,
                  "suspect_en": suspect_en, "suspect_zh": suspect_zh,
                  "dropped_page_numbers": dual_dropped,
                  "encoding": enc, "split_mode": "dual"})
    else:
        out_json({"book_id": a.book, "paragraphs": len(paras), "headings": headings,
                  "dropped_page_numbers": dropped, "encoding": enc, "split_mode": a.split})


def cmd_books(a):
    conn = open_db(db_path(a))
    rows = conn.execute("""
        SELECT b.book_id, b.para_count, b.source_file, b.updated_at,
               COALESCE(SUM(p.status='refined'),0)  AS refined,
               COALESCE(SUM(p.status IN ('translated','refined')),0) AS machined,
               COALESCE(SUM(p.error_flags<>''''),0) AS flagged
        FROM books b LEFT JOIN paragraphs p ON p.book_id=b.book_id
        GROUP BY b.book_id ORDER BY b.updated_at DESC""").fetchall()
    if a.json:
        out_json([dict(r) for r in rows])
        return
    if not rows:
        info("暂无书目。用 import 命令导入第一本书。")
        return
    print(f"{'book_id':<24}{'段数':>7}{'机翻':>7}{'精修':>7}{'标记':>6}  更新时间")
    for r in rows:
        print(f"{r['book_id']:<24}{r['para_count']:>7}{r['machined']:>7}{r['refined']:>7}"
              f"{r['flagged']:>6}  {r['updated_at'] or ''}")


def cmd_stats(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    meta = conn.execute("SELECT * FROM books WHERE book_id=?", (a.book,)).fetchone()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM paragraphs WHERE book_id=? GROUP BY status",
        (a.book,)).fetchall()
    flagged = conn.execute(
        "SELECT COUNT(*) FROM paragraphs WHERE book_id=? AND error_flags<>''",
        (a.book,)).fetchone()[0]
    staged = conn.execute(
        "SELECT COUNT(*) FROM issue_staging WHERE book_id=? AND status='open'",
        (a.book,)).fetchone()[0]
    counts = {s: 0 for s in VALID_STATUS}
    for r in rows:
        counts[r["status"]] = r["n"]
    total = meta["para_count"]
    if a.json:
        out_json({"book_id": a.book, "para_count": total, "by_status": counts,
                  "flagged": flagged, "staged_issues": staged,
                  "source_file": meta["source_file"],
                  "split_mode": meta["split_mode"], "encoding": meta["encoding"],
                  "updated_at": meta["updated_at"]})
        return
    print(f"书目 {a.book}（共 {total} 段，源文件 {meta['source_file']}）")
    label = {"pending": "待处理 pending", "translated": "已机翻 translated",
             "refined": "已精修 refined", "wip": "处理中 wip", "empty": "空段 empty"}
    for s in VALID_STATUS:
        n = counts[s]
        pct = 100.0 * n / total if total else 0
        bar = "█" * int(pct / 5)
        print(f"  {label[s]:<20}{n:>6}  {pct:5.1f}% {bar}")
    print(f"  质检标记段落：{flagged}（运行 qc 可重算清除）")
    print(f"  待提炼错题：{staged} 条（issue list 查看，攒批复盘提炼）")


# ---------------------------------------------------------------- 命令：next / apply / set-status / release

def make_batches(conn, book, status, n, max_chars, chunks, before, after,
                 ids=None, from_idx=None, to_idx=None, flagged=False):
    """构建「滑动窗口 + 上下文」的结构化批次。

    选段方式（互斥，优先级从高到低）：
      ids 显式段号列表 / from_idx..to_idx 区间 / 按 status 顺序流式取；
      flagged=True 时不限状态，专取 error_flags<>'' 的病灶段（--flagged 重修用）。
    按 status 取时：--n 控制每批段数上限，--max-chars 控制每批字符预算
    （两者同时生效，先到为准；单个超长段落独占一批）。
    每个批次带 context_before（前 before 段，含已有译文）与
    context_after（后 after 段开头，截 AFTER_CHARS 字符），仅供参考不翻译。
    target_chunk 各项带标记时附 error_flags。
    """
    base = ("SELECT para_index, is_heading, status, error_flags, en_text, machine_zh,"
            " refined_zh, notes FROM paragraphs WHERE book_id=?")

    def cost(r):
        c = len(r["en_text"] or "")
        if r["machine_zh"]:
            c += len(r["machine_zh"])
        return c

    if ids is not None:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(base + f" AND para_index IN ({ph}) ORDER BY para_index",
                            [book] + list(ids)).fetchall()
        groups = [rows]
    elif from_idx is not None:
        rows = conn.execute(base + " AND para_index BETWEEN ? AND ? ORDER BY para_index",
                            [book, from_idx, to_idx]).fetchall()
        groups = [rows]
    else:
        if flagged:
            rows = conn.execute(base + " AND error_flags<>'' ORDER BY para_index",
                                [book]).fetchall()
        else:
            rows = conn.execute(base + " AND status=? ORDER BY para_index",
                                [book, status]).fetchall()
        groups = []
        i = 0
        for _ in range(max(1, chunks)):
            batch, chars = [], 0
            while i < len(rows):
                r = rows[i]
                c = cost(r)
                if batch and max_chars and chars + c > max_chars:
                    break
                batch.append(r)
                chars += c
                i += 1
                if n and len(batch) >= n:
                    break
            if not batch:
                break
            groups.append(batch)
    batches = []
    for g in groups:
        if not g:
            continue
        first, last = g[0]["para_index"], g[-1]["para_index"]
        context_before = []
        if before > 0:
            cb = conn.execute(base + " AND para_index < ? ORDER BY para_index DESC LIMIT ?",
                              [book, first, before]).fetchall()
            cb.reverse()
            context_before = [{"id": r["para_index"], "en": r["en_text"],
                               "zh": r["refined_zh"] or r["machine_zh"]} for r in cb]
        context_after = []
        if after > 0:
            for r in conn.execute(base + " AND para_index > ? ORDER BY para_index"
                                  " LIMIT ?", [book, last, after]):
                text = r["en_text"] or ""
                if len(text) > AFTER_CHARS:
                    text = text[:AFTER_CHARS] + "…"
                context_after.append({"id": r["para_index"], "en": text})
        chunk = []
        for r in g:
            item = {"id": r["para_index"], "is_heading": bool(r["is_heading"]),
                    "status": r["status"], "en": r["en_text"],
                    "machine_zh": r["machine_zh"], "notes": r["notes"]}
            if r["error_flags"]:
                item["error_flags"] = r["error_flags"]
            chunk.append(item)
        batches.append({
            "ids": [r["para_index"] for r in g],
            "chars": sum(cost(r) for r in g),
            "context_before": context_before,
            "target_chunk": chunk,
            "context_after": context_after,
        })
    return batches


def cmd_next(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    ids = from_i = to_i = None
    if a.ids:
        try:
            ids = [int(x) for x in a.ids.split(",") if x.strip() != ""]
        except ValueError:
            die("--ids 需要逗号分隔的整数，如 --ids 101,102,105")
        if not ids:
            die("--ids 为空")
    elif a.from_idx is not None or a.to_idx is not None:
        if a.from_idx is None or a.to_idx is None:
            die("--from 与 --to 需要成对使用")
        from_i, to_i = a.from_idx, a.to_idx
    if a.flagged:
        if a.status is not None:
            die("--flagged 与 --status 互斥（flagged 段不限状态）")
        if ids is not None or from_i is not None:
            die("--flagged 与 --ids / --from --to 互斥（显式段号本身可看任意段）")
    status = a.status or "pending"
    explicit = ids is not None or from_i is not None
    claimed = False
    if a.claim:
        # BEGIN IMMEDIATE 独占写事务：把「选段 SELECT」与「置 wip UPDATE」原子化。
        # 并行会话不会读到同一批段落重复翻译——第二个会话在此排队，等第一个
        # 提交后自然取到后续段落。
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            die(f"claim 抢占失败（数据库被其他会话锁定：{e}），稍后重试即可")
    batches = make_batches(conn, a.book, status, a.n, a.max_chars, a.chunks,
                           a.before, a.after, ids=ids, from_idx=from_i, to_idx=to_i,
                           flagged=a.flagged)
    # 译前动态提示：只扫 target_chunk 的原文，不扫上下文；纯读、不写 hit_count
    rules = load_rules(conn, a.book)
    for b in batches:
        b.update(build_hints(rules, b))
        if a.flagged:
            details = build_flag_details(rules, b)
            if details:
                b["flag_details"] = details
    if a.claim:
        if batches:
            all_ids = [i for b in batches for i in b["ids"]]
            ts = now_iso()
            conn.execute(
                f"UPDATE paragraphs SET status='wip', claimed_at=?, updated_at=?"
                f" WHERE book_id=? AND para_index IN ({','.join('?' * len(all_ids))})",
                [ts, ts, a.book] + all_ids)
            claimed = True
        conn.commit()
    if a.flagged:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE book_id=? AND error_flags<>''",
            (a.book,)).fetchone()[0]
    else:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE book_id=? AND status=?",
            (a.book, status)).fetchone()[0]
    total = sum(len(b["ids"]) for b in batches)
    scope = ("显式指定段落" if explicit else
             ("带质检标记" if a.flagged else f"状态 {status}，剩余 {remaining} 段"))
    info(f"取到 {len(batches)} 个批次共 {total} 段（{scope}）"
         + ("，已占用为 wip" if claimed else ""))
    out_json({"book_id": a.book, "status": "flagged" if a.flagged else status,
              "claimed": claimed, "batches": batches, "remaining": remaining})
    conn.close()


ALLOWED_APPLY_KEYS = ("machine_zh", "refined_zh", "status", "error_flags", "notes")


def cmd_apply(a):
    if a.file == "-":
        try:
            raw = json.loads(sys.stdin.read())
        except UnicodeDecodeError:
            die("stdin 不是合法 UTF-8（Windows PowerShell 5.1 的管道会先把中文损坏成 '?'）。"
                "请把 JSON 落盘后走文件方式：python transdb.py apply updates/xxx.json")
    else:
        p = Path(a.file)
        if not p.is_file():
            die(f"更新文件不存在：{p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        items, book = raw, a.book
    else:
        book = raw.get("book_id") or a.book
        items = raw.get("updates", [])
    if not book:
        die("缺少 book_id：JSON 里没有 book_id 字段，也没有通过 --book 指定")
    if not items:
        die("updates 为空：没有可写入的更新")
    conn = open_db(db_path(a))
    require_book(conn, book)
    known = {r[0] for r in conn.execute(
        "SELECT para_index FROM paragraphs WHERE book_id=?", (book,))}
    valid, bad = [], []
    for it in items:
        # para_index 与 id 等价：next 输出的 target_chunk 用 id，apply 两种都收
        idx = it.get("para_index", it.get("id"))
        if not isinstance(idx, int) or idx not in known:
            bad.append(idx)
        elif not any(k in it and it[k] is not None for k in ALLOWED_APPLY_KEYS):
            bad.append(idx)
        else:
            valid.append(it)
    if bad:
        msg = f"{len(bad)} 条更新非法（para_index 不存在或字段为空）：{bad[:10]}"
        if a.allow_partial:
            info("跳过非法更新：" + msg)
        else:
            die(msg + "；确认跳过请加 --allow-partial")
    ts = now_iso()
    rules = [] if a.no_gate else load_rules(conn, book)
    applied = blocked = forced_n = staged = 0
    violations, warnings, forced_ids = [], [], []
    hit_incr = {}
    for it in valid:
        idx = it.get("para_index", it.get("id"))
        cur = conn.execute(
            "SELECT status, en_text, error_flags, machine_zh, refined_zh FROM paragraphs"
            " WHERE book_id=? AND para_index=?", (book, idx)).fetchone()
        cur_flags = cur["error_flags"] or ""
        # 写回门禁：只校验本次实际写入的译文字段（空串=清空字段，不校验），
        # 同段同规则只计一次命中
        gate_hits = []
        if not a.no_gate:
            for col in ("machine_zh", "refined_zh"):
                v = it.get(col)
                if v:
                    for h in check_rule_hits(rules, cur["en_text"], v):
                        if not any(x["rule_id"] == h["rule_id"] for x in gate_hits):
                            gate_hits.append(h)
            for h in gate_hits:
                hit_incr[h["rule_id"]] = hit_incr.get(h["rule_id"], 0) + 1
        blocks = [h for h in gate_hits if h["severity"] == "block"]
        warns = [h for h in gate_hits if h["severity"] == "warn"]
        if blocks and not a.force:
            # 拒写方案：本段本次完全不写库（译文/状态/标记都不动）——库里
            # 永远不会有 block 级坏译文；审计轨迹已在 updates JSON 里。
            blocked += 1
            for h in blocks:
                violations.append({**h, "id": idx,
                                   "action": f"请立即针对 id={idx} 重新翻译并再次 apply"
                                             "（确认规则误伤可 apply --force 强制写入）"})
            continue
        sets, vals = [], []
        wrote_refined = wrote_machine = False
        for col in ("machine_zh", "refined_zh"):
            if col in it and it[col] is not None:
                v = it[col]
                sets.append(f"{col}=?")
                vals.append(v if v != "" else None)  # 空串 = 清空该字段
                if col == "refined_zh" and v:
                    wrote_refined = True
                if col == "machine_zh" and v:
                    wrote_machine = True
        if "notes" in it and it["notes"] is not None:
            # 译注存独立字段：不污染正文、不参与质检、不推进状态
            sets.append("notes=?")
            vals.append(it["notes"] if it["notes"] != "" else None)
        status = it.get("status")
        if status is not None:
            if status not in VALID_STATUS:
                die(f"非法 status：{status}（可选 {VALID_STATUS}）")
        elif wrote_refined:
            status = "refined"
        elif wrote_machine and cur["status"] in ("pending", "wip", "translated"):
            status = "translated"
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        # error_flags：用户显式值优先，rule_* 标记的生杀权归门禁——
        # 写过译文就重算（命中打标/未命中清标，幂等自愈），没写过就原样
        user_flags = it.get("error_flags")
        base_flags = user_flags if user_flags is not None else cur_flags
        if not a.no_gate and (wrote_refined or wrote_machine):
            final_flags = merge_flags(base_flags, RULE_OWNED_CODES,
                                      [h["flag"] for h in blocks + warns])
        else:
            final_flags = base_flags
        if final_flags != cur_flags:
            sets.append("error_flags=?")
            vals.append(final_flags)
        sets.append("updated_at=?")
        vals.extend([ts, book, idx])
        conn.execute(f"UPDATE paragraphs SET {', '.join(sets)}"
                     f" WHERE book_id=? AND para_index=?", vals)
        applied += 1
        # 错题池顺手记（方式 A，v1.5）：条目带 issue_* 字段且本次确实写了译文才入池。
        # bad 存「被覆盖字段的旧值」；被 block 拦截的段在上方 continue，天然不入池。
        if it.get("issue_note") or it.get("issue_cat") or it.get("issue_en"):
            if wrote_refined or wrote_machine:
                bad_old = ((cur["refined_zh"] or cur["machine_zh"] or "")
                           if wrote_refined else (cur["machine_zh"] or ""))
                good_new = it.get("refined_zh") if wrote_refined else it.get("machine_zh")
                if stage_issue(conn, book, idx, it.get("issue_cat") or "trap",
                               it.get("issue_en"), cur["en_text"],
                               bad_old, good_new or "", it.get("issue_note")):
                    staged += 1
            else:
                info(f"提示：id={idx} 带错题字段但本次未写译文，未入错题池")
        if blocks and a.force:
            forced_n += 1
            forced_ids.append(idx)
        for h in warns:
            warnings.append({**h, "id": idx,
                             "action": "已写入并打标；精修时按 suggest_zh 改写后重新 apply 即自动清标"})
    for rid, n in hit_incr.items():
        conn.execute("UPDATE rules SET hit_count=hit_count+? WHERE rule_id=?", (n, rid))
    conn.commit()
    summary = {s: 0 for s in VALID_STATUS}
    for r in conn.execute("SELECT status, COUNT(*) n FROM paragraphs WHERE book_id=?"
                          " GROUP BY status", (book,)):
        summary[r["status"]] = r["n"]
    if blocked:
        info(f"警告：{blocked} 段被规则拦截（block 级）未写库，按返回 JSON 的 violations 返工")
    info(f"已写入 {applied} 段（跳过 {len(bad)} 条非法更新，拦截 {blocked} 段，入错题池 {staged} 条）")
    out_json({"book_id": book, "applied": applied, "skipped": bad,
              "blocked": blocked, "forced": forced_n, "forced_ids": forced_ids,
              "violations": violations, "warnings": warnings, "staged": staged,
              "by_status": summary})
    conn.close()


def cmd_set_status(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    if a.status not in VALID_STATUS:
        die(f"非法 status：{a.status}（可选 {VALID_STATUS}）")
    if a.index is None and (a.from_idx is None or a.to_idx is None):
        die("需要 --index N（单段）或 --from A --to B（闭区间批量）")
    if a.index is not None:
        cond, params = "para_index=?", [a.index]
    else:
        cond, params = "para_index BETWEEN ? AND ?", [a.from_idx, a.to_idx]
    cur = conn.execute(
        f"UPDATE paragraphs SET status=?, claimed_at=NULL, updated_at=?"
        f" WHERE book_id=? AND {cond}", [a.status, now_iso(), a.book] + params)
    conn.commit()
    info(f"已把 {cur.rowcount} 段状态改为 {a.status}")
    out_json({"book_id": a.book, "updated": cur.rowcount, "status": a.status})


def cmd_release(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    cutoff = (datetime.now() - timedelta(minutes=a.older_min)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT para_index, refined_zh, machine_zh FROM paragraphs"
        " WHERE book_id=? AND status='wip' AND (claimed_at IS NULL OR claimed_at<?)",
        (a.book, cutoff)).fetchall()
    # 按已有译稿智能恢复：绝不让已机翻/已精修的段盲目降级回 pending
    to = {"refined": [], "translated": [], "pending": []}
    for r in rows:
        if (r["refined_zh"] or "").strip():
            to["refined"].append(r["para_index"])
        elif (r["machine_zh"] or "").strip():
            to["translated"].append(r["para_index"])
        else:
            to["pending"].append(r["para_index"])
    ts = now_iso()
    for status, idxs in to.items():
        if idxs:
            conn.executemany(
                "UPDATE paragraphs SET status=?, claimed_at=NULL, updated_at=?"
                " WHERE book_id=? AND para_index=?",
                [(status, ts, a.book, i) for i in idxs])
    conn.commit()
    n = len(rows)
    info(f"已释放 {n} 段 wip（占用超过 {a.older_min} 分钟）：{len(to['refined'])} 段回 "
         f"refined，{len(to['translated'])} 段回 translated，{len(to['pending'])} 段回 pending")
    out_json({"book_id": a.book, "released": n, "to_refined": len(to["refined"]),
              "to_translated": len(to["translated"]), "to_pending": len(to["pending"])})
    conn.close()


# ---------------------------------------------------------------- 命令：show / search

FIELD_MAP = {"en": "en_text", "machine": "machine_zh", "refined": "refined_zh"}


def cmd_show(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    if a.index is None and (a.from_idx is None or a.to_idx is None):
        die("需要 --index N 或 --from A --to B")
    if a.index is not None:
        cond, params = "para_index=?", [a.index]
    else:
        cond, params = "para_index BETWEEN ? AND ?", [a.from_idx, a.to_idx]
    rows = conn.execute(
        f"SELECT para_index, is_heading, status, error_flags, en_text, machine_zh,"
        f" refined_zh, notes, updated_at FROM paragraphs WHERE book_id=? AND {cond}"
        f" ORDER BY para_index", [a.book] + params).fetchall()
    if a.fields and a.fields != "all":
        keep = {"en": ["para_index", "en_text"], "machine": ["para_index", "machine_zh"],
                "refined": ["para_index", "refined_zh"],
                "notes": ["para_index", "notes"]}[a.fields]
        rows = [{k: r[k] for k in keep + ["status"]} for r in rows]
    else:
        rows = [dict(r) for r in rows]
    info(f"共 {len(rows)} 段")
    out_json(rows)


def cmd_search(a):
    if not a.zh and not a.en:
        die("至少给一个搜索词：--zh 中文词（查译文）或 --en 英文词（查原文）")
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    conds, params = ["book_id=?"], [a.book]
    if a.zh:
        if a.field in ("refined", "machine"):
            conds.append(f"{FIELD_MAP[a.field]} LIKE ?")
            params.append(f"%{a.zh}%")
        else:
            conds.append("(refined_zh LIKE ? OR machine_zh LIKE ?)")
            params += [f"%{a.zh}%"] * 2
    if a.en:
        conds.append("en_text LIKE ?")
        params.append(f"%{a.en}%")
    rows = conn.execute(
        f"SELECT para_index, status, is_heading, error_flags, en_text, machine_zh,"
        f" refined_zh FROM paragraphs WHERE {' AND '.join(conds)}"
        f" ORDER BY para_index LIMIT ?", params + [a.limit]).fetchall()
    hits = []
    for r in rows:
        full = a.full
        hits.append({
            "para_index": r["para_index"], "status": r["status"],
            "is_heading": bool(r["is_heading"]), "error_flags": r["error_flags"],
            "en": r["en_text"] if full else snippet(r["en_text"]),
            "machine_zh": r["machine_zh"] if full else snippet(r["machine_zh"]),
            "refined_zh": r["refined_zh"] if full else snippet(r["refined_zh"]),
        })
    info(f"命中 {len(hits)} 段（上限 {a.limit}，加 --limit 调整）")
    out_json({"query": {"zh": a.zh, "en": a.en, "field": a.field}, "hits": hits})


# ---------------------------------------------------------------- 命令：replace / qc / glossary

def cmd_replace(a):
    fields = {"refined": ["refined_zh"], "machine": ["machine_zh"],
              "both": ["refined_zh", "machine_zh"]}[a.field]
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    if a.regex:
        try:
            rx = re.compile(a.find)
        except re.error as e:
            die(f"--find 正则非法：{e}")
    cond, params = "book_id=?", [a.book]
    if a.if_en:
        terms = [t.strip() for t in a.if_en.split(",") if t.strip()]
        cond += " AND (" + " OR ".join(["en_text REGEXP ?"] * len(terms)) + ")"
        params += [term_pattern(t) for t in terms]  # 整词匹配：Eve 不吃 evening
    rows = conn.execute(
        f"SELECT para_index, en_text, refined_zh, machine_zh FROM paragraphs"
        f" WHERE {cond}", params).fetchall()
    changed, previews = 0, []
    sample = None  # 首个被改动的 (段, 字段)：--log-issue 采样入错题池用
    for r in rows:
        for col in fields:
            old = r[col]
            if not old:
                continue
            new = rx.sub(a.replacement, old) if a.regex else old.replace(a.find, a.replacement)
            if new == old:
                continue
            if a.dry_run:
                previews.append({"para_index": r["para_index"], "field": col,
                                 "before": snippet(old, 100), "after": snippet(new, 100)})
            else:
                if sample is None:
                    sample = {"para_index": r["para_index"], "en": r["en_text"],
                              "old": old, "new": new}
                conn.execute(
                    f"UPDATE paragraphs SET {col}=?, updated_at=?"
                    " WHERE book_id=? AND para_index=?",
                    (new, now_iso(), a.book, r["para_index"]))
            changed += 1
    staged = 0
    if a.log_issue is not None and not a.dry_run:
        # 方式 B（v1.5）：替换实跑时把首个命中段采样留档；dry-run 不写库也不入池
        if sample:
            note = a.log_issue or f"replace：{a.find} → {a.replacement}"
            trigger = next((t.strip() for t in (a.if_en or "").split(",") if t.strip()), "")
            if stage_issue(conn, a.book, sample["para_index"], "trap", trigger,
                           sample["en"], sample["old"], sample["new"], note):
                staged = 1
        else:
            info("提示：--log-issue 但本次无替换命中，未入错题池")
    if not a.dry_run:
        conn.commit()
    where = f"（限定原文含 {a.if_en}）" if a.if_en else ""
    info(f"扫描 {len(rows)} 段{where}，改动 {changed} 处" + ("（dry-run 未写库）" if a.dry_run else ""))
    out_json({"book_id": a.book, "scanned": len(rows), "changed": changed,
              "dry_run": a.dry_run, "staged": staged,
              **({"previews": previews[:50]} if previews else {})})


def load_badwords(conn, book, words_arg, file_arg, save):
    words = []
    if words_arg:
        words += [w.strip() for w in words_arg.split(",") if w.strip()]
    if file_arg:
        for line in Path(file_arg).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    if not words and not save:  # 读取该书保存过的词表
        row = conn.execute("SELECT qc_badwords FROM books WHERE book_id=?",
                           (book,)).fetchone()
        words = [w for w in (row["qc_badwords"] or "").split(",") if w]
    if save and words:
        conn.execute("UPDATE books SET qc_badwords=?, updated_at=? WHERE book_id=?",
                     (",".join(words), now_iso(), book))
        conn.commit()
        info(f"已保存质检词表（{len(words)} 个词），以后 qc 不带参数也会使用")
    return words


def cmd_qc(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    if a.clear:
        cur = conn.execute("UPDATE paragraphs SET error_flags='', updated_at=?"
                           " WHERE book_id=?", (now_iso(), a.book))
        conn.commit()
        info(f"已清除 {cur.rowcount} 段的全部质检标记")
        out_json({"book_id": a.book, "cleared": cur.rowcount})
        conn.close()
        return
    words = [] if a.rules_only else load_badwords(conn, a.book, a.badwords, a.badwords_file, a.save)
    cliche = []
    if a.cliche and not a.rules_only:
        # 俗套词表属本地种子数据（transdb_seeds.json 的 cliches），工具不内置；
        # 文件缺席时提示并跳过，不阻断 qc
        seeds, spath = load_seeds()
        if seeds is None:
            info(f"--cliche：未找到种子文件（{spath}），俗套词扫描跳过"
                 "——词表经 transdb_seeds.json 的 cliches 字段提供，不随工具内置")
        else:
            cliche = seeds["cliches"]
    rules = load_rules(conn, a.book)
    # rules 标记每次 qc 重算（drop-and-recompute）；--rules-only 时不动 qc 自有标记
    drop_codes = RULE_OWNED_CODES | (set() if a.rules_only else QC_OWNED_CODES)
    flagged = []
    hit_incr = {}
    rows = conn.execute(
        "SELECT para_index, status, is_heading, error_flags, en_text, machine_zh,"
        " refined_zh FROM paragraphs WHERE book_id=? ORDER BY para_index", (a.book,)).fetchall()
    for r in rows:
        zh = r["refined_zh"] or r["machine_zh"] or ""
        en = r["en_text"] or ""
        codes = []
        rule_hits = []
        if not a.rules_only:
            if r["status"] in ("translated", "refined") and not zh.strip():
                codes.append("empty_target")
            if zh:
                if not CJK_RE.search(zh):
                    codes.append("no_chinese")
                if len(en) >= 40 and not r["is_heading"]:
                    ratio = len(zh) / max(len(en), 1)
                    if ratio < a.min_ratio:
                        codes.append("too_short")
                    elif ratio > a.max_ratio:
                        codes.append("too_long")
                    zhs = zh.rstrip()
                    if zhs and zhs[-1] not in ZH_END_PUNCT:
                        codes.append("truncated")  # 句末无法定标点：疑似模型中途掐断
                if not r["is_heading"]:
                    for o, c in QUOTE_PAIRS:
                        if zh.count(o) != zh.count(c):
                            codes.append("unclosed_quote")
                            break
            for w in words:
                if w in zh:
                    codes.append(f"badword:{w}")
            for w in cliche:
                if w in zh:
                    codes.append(f"webnovel_cliche:{w}")
        # rules 表规则扫描（qc 也是 import --dual / sync-md 等旁路写入的全量兜底）
        for h in check_rule_hits(rules, en, zh):
            codes.append(h["flag"])
            rule_hits.append({"flag": h["flag"], "matched_span": h["bad_span"],
                              "suggest_zh": h["suggest_zh"], "note": h["note"]})
            hit_incr[h["rule_id"]] = hit_incr.get(h["rule_id"], 0) + 1
        old_flags = r["error_flags"] or ""
        if codes or old_flags:
            # 有新问题打标；旧标记落在本次重算范围内（drop_codes）而已不再命中的，
            # 重算后自动清除——修好的段重跑 qc 即自愈，无需手工清标
            new_flags = merge_flags(old_flags, drop_codes, codes)
            if new_flags != old_flags and not a.dry_run:
                conn.execute("UPDATE paragraphs SET error_flags=?, updated_at=?"
                             " WHERE book_id=? AND para_index=?",
                             (new_flags, now_iso(), a.book, r["para_index"]))
        if codes:
            entry = {"para_index": r["para_index"], "status": r["status"],
                     "flags": codes, "zh": snippet(zh, 120),
                     "len_en": len(en), "len_zh": len(zh)}
            if rule_hits:
                entry["rule_hits"] = rule_hits
            flagged.append(entry)
    if not a.dry_run:
        for rid, n in hit_incr.items():
            conn.execute("UPDATE rules SET hit_count=hit_count+? WHERE rule_id=?", (n, rid))
        conn.commit()
    scope = "（--rules-only）" if a.rules_only else f"（规则 {len(rules)} 条生效）"
    info(f"质检完成：{len(rows)} 段中 {len(flagged)} 段有问题{scope}"
         + ("（dry-run 未写库）" if a.dry_run else ""))
    out_json({"book_id": a.book, "checked": len(rows), "flagged": len(flagged),
              "dry_run": a.dry_run, "badwords": words, "cliche": a.cliche,
              "rules": len(rules), "rules_only": a.rules_only,
              "rows": flagged[:200]})
    conn.close()


def _flag(conn, book, para_index, code):
    conn.execute("UPDATE paragraphs SET error_flags=?, updated_at=?"
                 " WHERE book_id=? AND para_index=?",
                 (merge_flags(_get_flags(conn, book, para_index), set(), {code}),
                  now_iso(), book, para_index))


def cmd_glossary(a):
    conn = open_db(db_path(a))
    if a.gcmd == "add":
        conn.execute("INSERT OR REPLACE INTO glossary (book_id, en, zh, note, forbidden_zh)"
                     " VALUES (?,?,?,?,?)",
                     (a.book, a.en_term, a.zh_term, a.note or "", a.forbidden or ""))
        conn.commit()
        info(f"术语已登记：{a.en_term} → {a.zh_term}"
             + (f"（禁译：{a.forbidden}）" if a.forbidden else ""))
        out_json({"book_id": a.book, "en": a.en_term, "zh": a.zh_term,
                  "forbidden_zh": a.forbidden or ""})
        conn.close()
    elif a.gcmd == "load":
        if not Path(a.file).is_file():
            die(f"文件不存在：{a.file}")
        n = 0
        for line in Path(a.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t") if "\t" in line else line.split(None, 1)
            if len(parts) < 2:
                die(f"该行无法解析（应为 英文<TAB>中文[<TAB>禁译词表]）：{line}")
            en, zh = parts[0].strip(), parts[1].strip()
            forbidden = parts[2].strip() if len(parts) > 2 else ""
            conn.execute("INSERT OR REPLACE INTO glossary (book_id, en, zh, note, forbidden_zh)"
                         " VALUES (?,?,?,'',?)", (a.book, en, zh, forbidden))
            n += 1
        conn.commit()
        info(f"已导入 {n} 条术语")
        out_json({"book_id": a.book, "loaded": n})
        conn.close()
    elif a.gcmd == "list":
        rows = [dict(r) for r in conn.execute(
            "SELECT en, zh, note, COALESCE(forbidden_zh,'') AS forbidden_zh"
            " FROM glossary WHERE book_id=? ORDER BY en", (a.book,))]
        info(f"共 {len(rows)} 条术语")
        out_json({"book_id": a.book, "terms": rows})
        conn.close()
    elif a.gcmd == "remove":
        cur = conn.execute("DELETE FROM glossary WHERE book_id=? AND en=?",
                           (a.book, a.en_term))
        conn.commit()
        out_json({"book_id": a.book, "removed": cur.rowcount})
        conn.close()
    elif a.gcmd == "check":
        require_book(conn, a.book)
        terms = conn.execute(
            "SELECT en, zh, COALESCE(forbidden_zh,'') AS forbidden_zh FROM glossary"
            " WHERE book_id=?", (a.book,)).fetchall()
        if not terms:
            die("术语表为空，先用 glossary add / load 登记")
        # 先清掉旧的一致性标记，重算
        if not a.dry_run:
            rows_all = conn.execute(
                "SELECT para_index, error_flags FROM paragraphs WHERE book_id=?"
                " AND (error_flags LIKE '%name_mismatch%' OR error_flags LIKE '%forbidden_zh%')",
                (a.book,)).fetchall()
            for r in rows_all:
                conn.execute("UPDATE paragraphs SET error_flags=? WHERE book_id=?"
                             " AND para_index=?",
                             (merge_flags(r["error_flags"],
                                          {"name_mismatch", "forbidden_zh"}, set()),
                              a.book, r["para_index"]))
        mismatches = []
        for t in terms:
            pat = term_pattern(t["en"])  # 整词匹配，大小写按术语本身是否有大写决定
            base = ("SELECT para_index, en_text, machine_zh, refined_zh FROM paragraphs"
                    " WHERE book_id=? AND en_text REGEXP ?"
                    " AND COALESCE(refined_zh, machine_zh, '') <> ''")
            for r in conn.execute(base + " AND COALESCE(refined_zh, machine_zh) NOT LIKE ?",
                                  (a.book, pat, f"%{t['zh']}%")).fetchall():
                mismatches.append({"para_index": r["para_index"], "kind": "missing",
                                   "term_en": t["en"], "term_zh": t["zh"],
                                   "en": snippet(r["en_text"], 100),
                                   "zh": snippet(r["refined_zh"] or r["machine_zh"], 100)})
                if not a.dry_run:
                    _flag(conn, a.book, r["para_index"], "name_mismatch")
            for bad in [w.strip() for w in t["forbidden_zh"].split(",") if w.strip()]:
                for r in conn.execute(base + " AND COALESCE(refined_zh, machine_zh) LIKE ?",
                                      (a.book, pat, f"%{bad}%")).fetchall():
                    mismatches.append({"para_index": r["para_index"], "kind": "forbidden",
                                       "term_en": t["en"], "bad_zh": bad,
                                       "en": snippet(r["en_text"], 100),
                                       "zh": snippet(r["refined_zh"] or r["machine_zh"], 100)})
                    if not a.dry_run:
                        _flag(conn, a.book, r["para_index"], f"forbidden_zh:{t['en']}={bad}")
        if not a.dry_run:
            conn.commit()
        info(f"一致性检查：{len(terms)} 个术语，命中问题 {len(mismatches)} 处"
             + ("（dry-run 未写库）" if a.dry_run else ""))
        out_json({"book_id": a.book, "terms": len(terms),
                  "mismatches": len(mismatches), "rows": mismatches[:200]})
        conn.close()


def _get_flags(conn, book, para_index):
    r = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id=? AND para_index=?",
                     (book, para_index)).fetchone()
    return r["error_flags"] or ""


# ---------------------------------------------------------------- 命令：rule（警惕库）

def _compile_or_die(kind, pattern):
    """规则入库前的正则编译校验：非法立即报错，绝不让坏正则毒死流水线。"""
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE) if kind == "en" else re.compile(pattern)
    except re.error as e:
        die(f"{kind} 正则非法（{pattern!r}）：{e}")


def _rule_fields_ok(cat, en_pat, bad, good, quiet=False):
    """category × 字段组合校验（非法直接 die）。"""
    if cat not in RULE_CATEGORIES:
        die(f"非法 category：{cat}（可选 {RULE_CATEGORIES}）")
    if cat == "trap" and not (en_pat and bad):
        die("trap 规则需要 --en 与 --bad 同时提供（英文门控 + 禁用译法）")
    if cat == "badword":
        if not bad:
            die("badword 规则需要 --bad")
        if en_pat:
            die("badword 规则不设英文门控（要按原文语境放行请改用 trap）")
    if cat == "syntax" and not bad:
        die("syntax 规则需要 --bad（中文句式正则）")
    if cat == "style":
        if not good:
            die("style 规则需要 --good（正面改写示范）")
        if bad:
            die("style 规则不做译后拦截，不能提供 --bad")
        if not en_pat and not quiet:
            info("提示：style 规则没有 --en 将永远不会被挂载")


def _rule_upsert(conn, spec):
    """按去重键 (book_id, category, en_pattern, bad_zh) 插入或更新
    （更新 good/note/severity，保留 hit_count），返回 (rule_id, inserted)。"""
    ex = conn.execute(
        "SELECT rule_id FROM rules WHERE book_id=? AND category=? AND en_pattern=? AND bad_zh=?",
        (spec["book_id"], spec["category"], spec["en_pattern"], spec["bad_zh"])).fetchone()
    if ex:
        conn.execute("UPDATE rules SET good_zh=?, note=?, severity=? WHERE rule_id=?",
                     (spec["good_zh"], spec["note"], spec["severity"], ex[0]))
        return ex[0], False
    cur = conn.execute(
        "INSERT INTO rules (book_id, category, en_pattern, bad_zh, good_zh, note, severity)"
        " VALUES (?,?,?,?,?,?,?)",
        (spec["book_id"], spec["category"], spec["en_pattern"], spec["bad_zh"],
         spec["good_zh"], spec["note"], spec["severity"]))
    return cur.lastrowid, True


def _rule_spec_from(item):
    return {"book_id": item.get("book_id", "*") or "*",
            "category": item.get("category", "trap"),
            "en_pattern": item.get("en_pattern", "") or "",
            "bad_zh": item.get("bad_zh", "") or "",
            "good_zh": item.get("good_zh", "") or "",
            "note": item.get("note", "") or "",
            "severity": item.get("severity", "warn")}


def cmd_rule(a):
    conn = open_db(db_path(a))
    if a.rcmd == "add":
        severity = a.severity
        if a.cat == "style" and severity != "hint":
            info("提示：style 规则只做译前挂载，severity 固定为 hint")
            severity = "hint"
        _compile_or_die("en", a.en)
        _compile_or_die("bad", a.bad)
        _rule_fields_ok(a.cat, a.en, a.bad, a.good)
        spec = {"book_id": a.book, "category": a.cat, "en_pattern": a.en,
                "bad_zh": a.bad, "good_zh": a.good, "note": a.note, "severity": severity}
        rid, inserted = _rule_upsert(conn, spec)
        conn.commit()
        info((f"规则已登记：#{rid}" if inserted else f"规则已更新（去重键命中）：#{rid}")
             + f" [{a.cat}/{severity}] {a.en or a.bad}")
        out_json({"rule_id": rid, "updated": not inserted, **spec})
    elif a.rcmd == "list":
        cond, params = ["1=1"], []
        if a.book:
            cond.append("book_id=?")
            params.append(a.book)
        if a.cat:
            cond.append("category=?")
            params.append(a.cat)
        order = "hit_count DESC, rule_id" if a.top else "rule_id"
        limit = f" LIMIT {int(a.top)}" if a.top else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT rule_id, book_id, category, severity, en_pattern, bad_zh, good_zh,"
            f" note, hit_count, created_at FROM rules WHERE {' AND '.join(cond)}"
            f" ORDER BY {order}{limit}", params)]
        info(f"共 {len(rows)} 条规则")
        out_json({"rules": rows})
    elif a.rcmd == "remove":
        cur = conn.execute("DELETE FROM rules WHERE rule_id=?", (a.id,))
        conn.commit()
        out_json({"removed": cur.rowcount})
    elif a.rcmd == "load":
        p = Path(a.file)
        if not p.is_file():
            die(f"文件不存在：{p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            die(f"JSON 解析失败：{e}")
        items = data if isinstance(data, list) else data.get("rules") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            die('JSON 应为规则对象数组（或 {"rules": [...]}）')
        bad_rows = []
        for i, it in enumerate(items):
            spec = _rule_spec_from(it)
            try:
                _compile_or_die("en", spec["en_pattern"])
                _compile_or_die("bad", spec["bad_zh"])
                _rule_fields_ok(spec["category"], spec["en_pattern"], spec["bad_zh"],
                                spec["good_zh"], quiet=True)
            except SystemExit:
                bad_rows.append(i)
        if bad_rows:
            die(f"第 {bad_rows} 条规则非法（正则或字段组合不对），整批拒绝，未写入任何条目")
        inserted = updated = 0
        for it in items:
            _, ins = _rule_upsert(conn, _rule_spec_from(it))
            inserted += ins
            updated += not ins
        conn.commit()
        info(f"规则导入：新插 {inserted} 条，更新 {updated} 条（按去重键合并）")
        out_json({"loaded": inserted, "updated": updated, "rejected": []})
    elif a.rcmd == "dump":
        cond, params = ["1=1"], []
        if a.book:
            cond.append("book_id=?")
            params.append(a.book)
        rows = [dict(r) for r in conn.execute(
            f"SELECT book_id, category, en_pattern, bad_zh, good_zh, note, severity"
            f" FROM rules WHERE {' AND '.join(cond)} ORDER BY book_id, rule_id", params)]
        if a.out:
            Path(a.out).write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
            info(f"已导出 {len(rows)} 条规则 → {a.out}")
            out_json({"out": a.out, "rules": len(rows)})
        else:
            info(f"共 {len(rows)} 条规则")
            out_json(rows)
    elif a.rcmd == "init-defaults":
        seeds, spath = load_seeds(a.file)
        if seeds is None:
            die(f"未找到种子文件：{spath}。工具不内置规则数据——复制 "
                "transdb_seeds.example.json 为 transdb_seeds.json（或用 file 参数 / "
                "TRANSDB_SEEDS 环境变量指定路径），填入你自己的规则后再试")
        items = seeds["rules"]
        bad_rows = []
        for i, it in enumerate(items):
            spec = _rule_spec_from(it)
            try:
                _compile_or_die("en", spec["en_pattern"])
                _compile_or_die("bad", spec["bad_zh"])
                _rule_fields_ok(spec["category"], spec["en_pattern"], spec["bad_zh"],
                                spec["good_zh"], quiet=True)
            except SystemExit:
                bad_rows.append(i)
        if bad_rows:
            die(f"种子文件第 {bad_rows} 条规则非法（正则或字段组合不对），整批拒绝，未写入")
        inserted = skipped = 0
        for it in items:
            _, ins = _rule_upsert(conn, _rule_spec_from(it))
            inserted += ins
            skipped += not ins
        conn.commit()
        info(f"种子规则导入（{spath}）：新插 {inserted} 条，已存在跳过 {skipped} 条（幂等）")
        out_json({"inserted": inserted, "skipped": skipped, "file": str(spath)})
    conn.close()


# ---------------------------------------------------------------- 命令：issue（错题池）

def cmd_issue(a):
    conn = open_db(db_path(a))
    if a.icmd == "add":
        require_book(conn, a.book)
        row = conn.execute(
            "SELECT en_text, machine_zh, refined_zh FROM paragraphs"
            " WHERE book_id=? AND para_index=?", (a.book, a.index)).fetchone()
        if row is None:
            die(f"段落不存在：{a.book} #{a.index}")
        # bad 默认自动抓当前译文（refined 优先），无需手工复制长文本
        bad = a.bad if a.bad is not None else (row["refined_zh"] or row["machine_zh"] or "")
        iid = stage_issue(conn, a.book, a.index, a.cat, a.en, row["en_text"],
                          bad, a.good or "", a.note or "")
        conn.commit()
        if iid is None:
            die("错题未入库（见上方警告）")
        info(f"错题已登记：#{iid} [{a.cat}] {a.book} #{a.index}"
             + (f"（触发词 {a.en}）" if a.en else ""))
        out_json({"issue_id": iid, "book_id": a.book, "para_index": a.index,
                  "category": a.cat, "issue_en": a.en or "", "note": a.note or ""})
    elif a.icmd == "list":
        cond, params = ["1=1"], []
        if a.status != "all":
            cond.append("status=?")
            params.append(a.status)
        if a.book:
            cond.append("book_id=?")
            params.append(a.book)
        if a.cat:
            cond.append("category=?")
            params.append(a.cat)
        where = " AND ".join(cond)
        total = conn.execute(f"SELECT COUNT(*) FROM issue_staging WHERE {where}",
                             params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM issue_staging WHERE {where}"
            f" ORDER BY category, issue_id LIMIT ?", params + [a.limit]).fetchall()
        issues = []
        for r in rows:
            d = dict(r)
            if not a.full:
                # 存储全量、展示截句：en 按触发词取病灶句，zh 按长度截断（裁定 #7）
                d["en_text"] = anchor_excerpt(d["en_text"], d["issue_en"])
                d["bad_zh"] = snippet(d["bad_zh"], 120)
                d["good_zh"] = snippet(d["good_zh"], 120)
            issues.append(d)
        info(f"共 {total} 条错题（返回 {len(rows)} 条；--limit 调整，--full 看全文快照）")
        out_json({"total": total, "returned": len(rows), "status": a.status,
                  "full": a.full, "issues": issues})
    elif a.icmd == "promote":
        ids = list(dict.fromkeys(parse_id_list(a.id)))
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM issue_staging WHERE issue_id IN ({ph}) ORDER BY issue_id",
            ids).fetchall()
        found = {r["issue_id"] for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            die(f"错题不存在：{missing}")
        if any(r["status"] != "open" for r in rows):
            die("仅 open 状态的错题可转正（promoted/ignored 的不再处理）")
        cats = {r["category"] for r in rows}
        if a.cat:
            cat = a.cat
        elif len(cats) == 1:
            cat = cats.pop()  # 默认继承错题的 category（裁定 #2）
        else:
            die(f"所选错题 category 不一致（{sorted(cats)}），需显式 --cat 指定")
        note = a.note if a.note is not None else next(
            (r["note"] for r in rows if r["note"]), "")
        # 作用域默认单书（错题来源书目），显式 --book '*' 才全局（裁定 #5）
        scope = a.book if a.book is not None else rows[0]["book_id"]
        severity = a.severity
        if cat == "style" and severity != "hint":
            info("提示：style 规则只做译前挂载，severity 固定为 hint")
            severity = "hint"
        # None 归一为空串：UNIQUE 去重键里的 NULL 互不相等，会绕过去重
        en_pat, bad_pat = a.en or "", a.bad or ""
        # 校验链与入库完全复用 rule add：非法即 die（整单拒绝，错题保持 open）
        _compile_or_die("en", en_pat)
        _compile_or_die("bad", bad_pat)
        _rule_fields_ok(cat, en_pat, bad_pat, a.good)
        spec = {"book_id": scope, "category": cat, "en_pattern": en_pat,
                "bad_zh": bad_pat, "good_zh": a.good, "note": note, "severity": severity}
        rid, inserted = _rule_upsert(conn, spec)
        conn.execute(
            f"UPDATE issue_staging SET status='promoted', rule_id=? WHERE issue_id IN ({ph})",
            [rid] + ids)
        # 转正即返回命中面预览：复盘当场校准正则的反馈回路（裁定 #4）
        preview = preview_rule_hits(conn, rows[0]["book_id"], en_pat, bad_pat)
        conn.commit()
        info(f"错题 {ids} 已转正为规则 #{rid}"
             + ("（去重键命中，更新既有规则）" if not inserted else "")
             + f" [{cat}/{severity}] 作用域 {scope}；看 preview 校准正则")
        out_json({"rule_id": rid, "updated": not inserted, "promoted": ids,
                  "book_id": scope, "category": cat, "severity": severity,
                  "note": note, "preview": preview})
    elif a.icmd == "ignore":
        ids = list(dict.fromkeys(parse_id_list(a.id)))
        ph = ",".join("?" * len(ids))
        found = {r[0] for r in conn.execute(
            f"SELECT issue_id FROM issue_staging WHERE issue_id IN ({ph})", ids)}
        missing = [i for i in ids if i not in found]
        if missing:
            die(f"错题不存在：{missing}")
        cur = conn.execute(
            f"UPDATE issue_staging SET status='ignored' WHERE issue_id IN ({ph})", ids)
        conn.commit()
        info(f"已忽略 {cur.rowcount} 条错题")
        out_json({"ignored": cur.rowcount})
    conn.close()


# ---------------------------------------------------------------- 命令：diff / sync-md

def norm_text(s):
    """比对用的规整键：压掉所有空白差异（硬换行/多空格不影响对齐判断）。"""
    return re.sub(r"\s+", " ", s or "").strip()


def parse_file_blocks(path):
    """读外部成品文件 → [(起始行号, 段文本)]：空行分段、段内换行保留，
    纯数字页码块丢弃——与 export --sep blank 的输出格式互逆。"""
    text, _ = read_text_auto(path)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, cur, start = [], [], None
    for no, ln in enumerate(lines, 1):
        if ln.strip():
            if start is None:
                start = no
            cur.append(ln.strip())
        elif cur:
            blocks.append((start, "\n".join(cur)))
            cur, start = [], None
    if cur:
        blocks.append((start, "\n".join(cur)))
    return [(no, t) for no, t in blocks if not re.fullmatch(r"\d{1,4}", t)]


def load_db_texts(conn, book, field, fallback):
    main_col = FIELD_MAP[field]
    fb_col = FIELD_MAP[fallback] if fallback != "none" and fallback != field else None
    out = []
    for r in conn.execute("SELECT para_index, is_heading, en_text, machine_zh, refined_zh"
                          " FROM paragraphs WHERE book_id=? ORDER BY para_index",
                          (book,)).fetchall():
        text = r[main_col] or (r[fb_col] if fb_col else None) or ""
        out.append({"para_index": r["para_index"], "is_heading": bool(r["is_heading"]),
                    "text": text})
    return out


def align_db_file(db_rows, file_blocks):
    """库段落 × 文件块按规整文本对齐（SequenceMatcher 内容匹配，非死板按位）。
    db_rows: [{"para_index", "text"}]；file_blocks: [(起始行号, 文本)]。
    返回 (results, same)：result ∈ same / differ / missing_in_file / extra_in_file，
    其中 differ 行带 file_idx（文件块下标，sync-md 回写用）。"""
    a = [norm_text(r["text"]) for r in db_rows]
    b = [norm_text(t) for _, t in file_blocks]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    results, same = [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                results.append({"para_index": db_rows[i1 + k]["para_index"],
                                "file_line": file_blocks[j1 + k][0], "result": "same"})
            same += i2 - i1
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                results.append({"para_index": db_rows[i1 + k]["para_index"],
                                "file_line": file_blocks[j1 + k][0], "result": "differ",
                                "db_snip": snippet(a[i1 + k], 60),
                                "file_snip": snippet(b[j1 + k], 60),
                                "file_idx": j1 + k})
            for k in range(i1 + n, i2):
                results.append({"para_index": db_rows[k]["para_index"], "file_line": None,
                                "result": "missing_in_file", "db_snip": snippet(a[k], 60)})
            for k in range(j1 + n, j2):
                results.append({"para_index": None, "file_line": file_blocks[k][0],
                                "result": "extra_in_file", "file_snip": snippet(b[k], 60)})
        elif tag == "delete":
            for k in range(i1, i2):
                results.append({"para_index": db_rows[k]["para_index"], "file_line": None,
                                "result": "missing_in_file", "db_snip": snippet(a[k], 60)})
        elif tag == "insert":
            for k in range(j1, j2):
                results.append({"para_index": None, "file_line": file_blocks[k][0],
                                "result": "extra_in_file", "file_snip": snippet(b[k], 60)})
    return results, same


def cmd_diff(a):
    if not Path(a.file).is_file():
        die(f"文件不存在：{a.file}")
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    db_rows = load_db_texts(conn, a.book, a.field, a.fallback)
    file_blocks = parse_file_blocks(a.file)
    results, same = align_db_file(db_rows, file_blocks)
    bad = [r for r in results if r["result"] != "same"]
    info(f"对齐完成：库 {len(db_rows)} 段 / 文件 {len(file_blocks)} 块；一致 {same}，"
         f"错位或差异 {len(bad)} 处"
         f"（differ=内容不同 missing_in_file=文件缺段 extra_in_file=文件多块）")
    out_json({"book_id": a.book, "file": a.file, "field": a.field,
              "db_paragraphs": len(db_rows), "file_blocks": len(file_blocks),
              "identical": same, "rows": results if a.all else bad[:200]})
    conn.close()


def cmd_sync_md(a):
    """外部成品回写库：对齐后把内容不同的段用文件版覆盖 refined_zh（状态置 refined）。
    只更新既有段落，绝不增删段落；文件多出/缺失的块只报告请人工核对。
    默认 dry-run 预览，加 --write 才写库。"""
    if not Path(a.file).is_file():
        die(f"文件不存在：{a.file}")
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    db_rows = load_db_texts(conn, a.book, "refined", "machine")
    file_blocks = parse_file_blocks(a.file)
    results, same = align_db_file(db_rows, file_blocks)
    differs = [r for r in results if r["result"] == "differ"]
    missing = [r for r in results if r["result"] == "missing_in_file"]
    extra = [r for r in results if r["result"] == "extra_in_file"]
    if a.write:
        ts = now_iso()
        for r in differs:
            block_text = file_blocks[r["file_idx"]][1]
            if block_text.startswith("[[缺译 #"):
                continue  # 导出缺译标记不是译文，绝不写库
            new = join_lines(block_text.split("\n"))
            conn.execute(
                "UPDATE paragraphs SET refined_zh=?, status='refined', claimed_at=NULL,"
                " updated_at=? WHERE book_id=? AND para_index=?",
                (new, ts, a.book, r["para_index"]))
        conn.commit()
    info(f"回写：{len(differs)} 段内容不同"
         + (f"，已写回 {len(differs)} 段" if a.write else "（dry-run 预览，加 --write 实写）")
         + f"；文件缺 {len(missing)} 段、多 {len(extra)} 块（无法自动回写，请人工核对）")
    out_json({"book_id": a.book, "file": a.file, "dry_run": not a.write,
              "to_update": len(differs), "missing_in_file": len(missing),
              "extra_in_file": len(extra),
              "rows": [{"para_index": r["para_index"], "file_line": r["file_line"],
                        "db_snip": r["db_snip"], "file_snip": r["file_snip"]}
                       for r in differs][:200]})
    conn.close()


# ---------------------------------------------------------------- 命令：export / sql / selftest

def cmd_export(a):
    conn = open_db(db_path(a))
    require_book(conn, a.book)
    if a.bilingual:
        main_col, fb_col = "en_text", None
    else:
        main_col = FIELD_MAP[a.field]
        fb_col = FIELD_MAP[a.fallback] if a.fallback != "none" and a.fallback != a.field else None
    rows = conn.execute(
        "SELECT para_index, is_heading, en_text, machine_zh, refined_zh, notes"
        " FROM paragraphs WHERE book_id=? ORDER BY para_index", (a.book,)).fetchall()
    # 译注：全书连续编号，正文尾加 [N] 角标，文末集中为脚注区
    note_rows = [(r["para_index"], " ".join((r["notes"] or "").split()))
                 for r in rows if (r["notes"] or "").strip()]
    note_no = {pi: i + 1 for i, (pi, _) in enumerate(note_rows)}
    lines, missing, fallback_used = [], [], 0
    for r in rows:
        if a.bilingual:
            zh = r["refined_zh"] or r["machine_zh"] or f"[[缺译 #{r['para_index']}]]"
            block = f"{r['en_text']}\n{zh}"
        else:
            val = r[main_col]
            if not val and fb_col:
                val = r[fb_col]
                if val:
                    fallback_used += 1
            if not val:
                missing.append(r["para_index"])
                val = f"[[缺译 #{r['para_index']}]]"
            block = ("## " + val) if (a.md_headings and r["is_heading"]) else val
        if note_rows and a.notes != "none" and r["para_index"] in note_no:
            block += f"[{note_no[r['para_index']]}]"
        lines.append(block)
    if note_rows and a.notes != "none":
        lines.extend(["\n".join(["## 译注"] if a.md_headings else ["———", "译注"])]
                     + [f"[{i}] {note}" for i, (_, note) in enumerate(note_rows, 1)])
    sep = "\n\n" if a.sep == "blank" else "\n"
    out_p = Path(a.out)
    if str(out_p.parent) not in ("", "."):
        out_p.parent.mkdir(parents=True, exist_ok=True)  # 剧本用 成品/书名.md，目录不存在自动建
    out_p.write_text(sep.join(lines) + "\n", encoding="utf-8", newline="\n")
    if missing:
        info(f"警告：{len(missing)} 段缺译，输出中已插入 [[缺译 #N]] 标记，"
             f"前 20 个：{missing[:20]}")
    info(f"已导出 {len(lines)} 段 → {a.out}（回退 {fallback_used} 段，缺译 {len(missing)} 段，"
         f"译注 {0 if a.notes == 'none' else len(note_rows)} 条）")
    out_json({"book_id": a.book, "out": a.out, "paragraphs": len(lines),
              "fallback_used": fallback_used, "missing": len(missing),
              "notes": 0 if a.notes == "none" else len(note_rows),
              "missing_preview": missing[:20]})
    conn.close()


def cmd_sql(a):
    q = a.query.strip().rstrip(";")
    if not q:
        die("SQL 为空")
    first = q.split(None, 1)[0].lower()
    readonly = first in ("select", "with", "pragma", "explain")
    if not readonly and not a.write:
        die("检测到写操作。确认执行请加 --write（请先备份数据库文件）")
    conn = open_db(db_path(a))
    if readonly:
        rows = [dict(r) for r in conn.execute(q)]
        info(f"返回 {len(rows)} 行")
        out_json(rows)
    else:
        cur = conn.execute(q)
        conn.commit()
        info(f"执行完成，影响 {cur.rowcount} 行")
        out_json({"changed": cur.rowcount})


def cmd_selftest(a):
    """端到端自测：临时目录里建库，跑完整流程并断言，不碰任何现有数据。"""
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        mark = "ok  " if cond else "FAIL"
        print(f"  [{mark}] {name}" + (f" —— {detail}" if (detail and not cond) else ""),
              file=sys.stderr)

    sample = (
        "Chapter 1\n\nTHE RAIN\n\nIt was a cold morning on the island.\n"
        "Eve wrapped her scarf tighter, watching the boats roll in\nwith the tide.\n\n"
        "12\n\nShe carried a small handbag and a bottle of brandy.\n\n"
        "\"Where is Eve?\" said the captain.\n\n\"I don't know,\" Eve replied, "
        "and stared at the sea.\n\nEpilogue\n\nThe end."
    )
    saved_seeds_env = os.environ.get("TRANSDB_SEEDS")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "t.db")
        conn = open_db(db)
        # v1.5.1 起工具零内置数据：selftest 自造合成种子文件跑全链路，
        # 词条全部为合成词（zword/禁词甲/测试套路词），不含任何真实词库
        SEED_RULES = [
            {"book_id": "*", "category": "trap", "severity": "block",
             "en_pattern": r"\bzword\b", "bad_zh": r"禁词甲|禁词乙",
             "good_zh": "正确词", "note": "测试种子：zword 消歧（合成词条）"},
            {"book_id": "*", "category": "syntax", "severity": "warn", "en_pattern": "",
             "bad_zh": r"被[^，。！？；：“”]{1,10}所[一-鿿]{1,4}",
             "good_zh": "删去“被…所…”，改为主动动词链或主谓顺承句",
             "note": "测试种子：欧化被动句式"},
            {"book_id": "*", "category": "syntax", "severity": "warn", "en_pattern": "",
             "bad_zh": r"(?:他的|她的|它的)[^，。！？]{1,12}(?:他的|她的|它的)[^，。！？]{1,12}(?:他的|她的|它的)",
             "good_zh": "单句内连续 3 个物主代词必须删减", "note": "测试种子：物主代词泛滥"},
        ]
        seeds_path = Path(td) / "transdb_seeds.json"
        seeds_path.write_text(json.dumps(
            {"rules": SEED_RULES, "cliches": ["测试套路词"]}, ensure_ascii=False),
            encoding="utf-8")
        os.environ["TRANSDB_SEEDS"] = str(seeds_path)
        src = Path(td) / "sample.txt"
        src.write_text(sample, encoding="utf-8")
        paras, dropped = parse_paragraphs(sample)
        check("解析：空行分段 8 段", len(paras) == 8, f"got {len(paras)}: {paras}")
        check("解析：丢弃页码行 12", dropped == 1)
        check("解析：硬换行合并为一段",
              any("watching the boats roll in with the tide" in p for p in paras))
        # 伪 import（不走 CLI，直接复用内部逻辑）
        rows = [("bk", i, looks_heading(p), p, None, None, "pending", "", None, now_iso())
                for i, p in enumerate(paras)]
        conn.executemany(
            "INSERT INTO paragraphs (book_id, para_index, is_heading, en_text, machine_zh,"
            " refined_zh, status, error_flags, claimed_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO books (book_id, para_count, split_mode, encoding,"
                     " created_at, updated_at) VALUES ('bk',?,?,?,?,?)",
                     (len(paras), "blank", "utf-8", now_iso(), now_iso()))
        conn.commit()
        check("标题识别：Chapter/Epilogue/全大写行", sum(r[2] for r in rows) == 3,
              f"got {sum(r[2] for r in rows)}")
        # 双语对照稿（dual 模式）
        check("dual：语言判定（含英文名的中文段仍判为中文）",
              is_zh_para("“Eve 拎着手袋。”") and not is_zh_para("Chapter 1"))
        dual = ("Chapter 1\n\n第一章\n\nEnglish one.\n\n英文一。\n\n"
                "English two, no translation.\n\nEnglish three.\n\n英文三。\n\n"
                "English four.\n\n中文四上。\n\n中文四下。")
        dpairs, dmerged, dorphan = pair_dual_stream(parse_dual_stream(dual)[0])
        check("dual：分块格式配对 5 段、4 段带译文、漏译段 zh 为 None",
              len(dpairs) == 5 and sum(1 for _, z in dpairs if z) == 4
              and dpairs[2] == ("English two, no translation.", None), str(dpairs))
        check("dual：连续中文段并回同一对（merged 计数）",
              dmerged == 1 and "中文四上" in dpairs[4][1] and "中文四下" in dpairs[4][1])
        check("dual：入库状态——带译文 translated / 漏译 pending",
              ("translated" if dpairs[1][1] else "pending") == "translated"
              and ("translated" if dpairs[2][1] else "pending") == "pending")
        pairfmt = ("Chapter 1\n第一章\n\n"
                   "The rain fell hard that evening, and seven boats stayed in the harbor.\n"
                   "那天晚上雨下得很大，七艘船留在了港口。\n\n"
                   "She believed every word.\n她相信每一个字。")
        ppairs, _, _ = pair_dual_stream(parse_dual_stream(pairfmt)[0])
        check("dual：成对格式（英中同块）正确拆分，英文不再粘上中文",
              [z for _, z in ppairs] == ["第一章", "那天晚上雨下得很大，七艘船留在了港口。",
                                         "她相信每一个字。"]
              and all(not CJK_RE.search(e) for e, _ in ppairs), str(ppairs))
        rt = "\n\n".join(f"{e}\n{z}" for e, z in ppairs if z)
        check("dual：export --bilingual 格式回灌配对不报废",
              pair_dual_stream(parse_dual_stream(rt)[0])[0] == [(e, z) for e, z in ppairs if z])
        linewise = "English one.\n英文一。\nEnglish two.\n英文二。"
        lpairs, _, _ = pair_dual_stream(parse_dual_stream(linewise)[0])
        check("dual：无空行逐行交替也能配对",
              lpairs == [("English one.", "英文一。"), ("English two.", "英文二。")],
              str(lpairs))
        upd = {"book_id": "bk", "updates": [
            {"para_index": 0, "machine_zh": "第一章"},
            {"para_index": 1, "machine_zh": "雨"},
            {"para_index": 2, "machine_zh": "岛上的清晨很冷。伊芙把围巾裹得更紧，望着船只随潮入港。"},
            {"para_index": 3, "machine_zh": "夏娃拎着一只手袋和一瓶白兰地。"},
            {"para_index": 4, "machine_zh": "“夏娃在哪儿？”船长说。"},
            {"para_index": 5, "machine_zh": "“我不知道，”夏娃答道，望着大海。"},
            {"para_index": 6, "machine_zh": "尾声"},
            {"para_index": 7, "machine_zh": "全文完。"}]}
        import types
        import io

        def run_apply(payload, force=False, no_gate=False):
            fake = types.SimpleNamespace(file="-", book=None, allow_partial=False,
                                         force=force, no_gate=no_gate, db=db)
            buf = io.StringIO()
            real_in, real_out = sys.stdin, sys.stdout
            sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), buf
            try:
                cmd_apply(fake)
            finally:
                sys.stdin, sys.stdout = real_in, real_out
            return json.loads(buf.getvalue())

        def run_cmd(fn, fake):
            buf = io.StringIO()
            real_out = sys.stdout
            sys.stdout = buf
            try:
                fn(fake)
            finally:
                sys.stdout = real_out
            return json.loads(buf.getvalue())

        applied_json = run_apply(upd)
        check("apply：写入 8 段", applied_json["applied"] == 8, str(applied_json))
        st = conn.execute("SELECT status FROM paragraphs WHERE book_id='bk' AND para_index=3"
                          ).fetchone()["status"]
        check("apply：状态自动推进为 translated", st == "translated", st)
        # replace --if-en
        n = conn.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE book_id='bk' AND en_text LIKE '%Eve%'"
            " AND machine_zh LIKE '%夏娃%'").fetchone()[0]
        conn.execute("UPDATE paragraphs SET machine_zh=replace(machine_zh,'夏娃','伊芙')"
                     " WHERE book_id='bk' AND en_text LIKE '%Eve%'")
        n2 = conn.execute("SELECT COUNT(*) FROM paragraphs WHERE book_id='bk'"
                          " AND machine_zh LIKE '%夏娃%'").fetchone()[0]
        check("replace 条件替换：只动原文含 Eve 的 2 段，纯中文段落的旧译不受波及",
              n == 2 and n2 == 1, f"matched={n}, remain={n2}")
        # qc badword
        conn.execute("UPDATE paragraphs SET error_flags='name_mismatch' WHERE book_id='bk'"
                     " AND para_index=3")
        for r in conn.execute("SELECT para_index, status, is_heading, error_flags, en_text,"
                              " machine_zh, refined_zh FROM paragraphs WHERE book_id='bk'"):
            zh = r["refined_zh"] or r["machine_zh"] or ""
            codes = [f"badword:手袋"] if "手袋" in zh else []
            if codes:
                conn.execute("UPDATE paragraphs SET error_flags=? WHERE book_id='bk'"
                             " AND para_index=?",
                             (merge_flags(r["error_flags"], QC_OWNED_CODES, codes),
                              r["para_index"]))
        flags = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=3").fetchone()["error_flags"]
        check("qc：badword 标记且保留 name_mismatch",
              "badword:手袋" in flags and "name_mismatch" in flags, flags)
        # export 对齐
        rows = conn.execute("SELECT para_index, refined_zh, machine_zh FROM paragraphs"
                            " WHERE book_id='bk' ORDER BY para_index").fetchall()
        lines = [(r["refined_zh"] or r["machine_zh"] or f"[[缺译 #{r['para_index']}]]")
                 for r in rows]
        check("export：段落 1:1 对齐（8 段）", len(lines) == 8)
        check("export：无缺译标记", not any(l.startswith("[[缺译") for l in lines))
        # 滑动窗口批次
        conn.execute("UPDATE paragraphs SET status='pending' WHERE book_id='bk'"
                     " AND para_index IN (6,7)")
        conn.commit()
        batches = make_batches(conn, "bk", "pending", n=1, max_chars=None, chunks=2,
                               before=2, after=1)
        b1, b2 = batches[0], batches[1]
        check("窗口：--chunks 2 --n 1 → 两个批次各 1 段",
              len(batches) == 2 and b1["ids"] == [6] and b2["ids"] == [7],
              str([b["ids"] for b in batches]))
        check("窗口：context_before/after 结构与 id 对齐",
              b1["context_after"][0]["id"] == 7 and b2["context_before"][-1]["id"] == 6
              and all("en" in t and "id" in t for t in b1["target_chunk"]))
        check("窗口：context_before 带已有译文 zh",
              b1["context_before"][-1]["zh"] is not None)
        b3 = make_batches(conn, "bk", "translated", n=20, max_chars=40, chunks=9,
                          before=0, after=0)
        total_ids = sum(len(b["ids"]) for b in b3)
        ok_pack = all(b["chars"] <= 40 or len(b["ids"]) == 1 for b in b3)
        check("字数打包：--max-chars 40 → 多批次、不超限、单段超长独占一批",
              len(b3) >= 2 and total_ids == 6 and ok_pack,
              str([(b["ids"], b["chars"]) for b in b3]))
        r = run_apply({"book_id": "bk", "updates": [{"id": 6, "machine_zh": "尾声（重译）"}]})
        v = conn.execute("SELECT machine_zh, status FROM paragraphs WHERE book_id='bk'"
                         " AND para_index=6").fetchone()
        check("apply：支持 id 别名（target_chunk 的 id 直接写回）",
              r["applied"] == 1 and v["machine_zh"] == "尾声（重译）"
              and v["status"] == "translated", str(r) + str(dict(v)))
        # stdin 管道（apply -）编码加固：子进程真实走一遍字节解码路径
        import subprocess
        script = str(Path(__file__).resolve())
        payload = json.dumps({"book_id": "bk", "updates": [
            {"id": 7, "machine_zh": "全文完。（管道 UTF-8 测试）"}]}, ensure_ascii=False)
        rp = subprocess.run([sys.executable, script, "--db", db, "apply", "-"],
                            input=payload.encode("utf-8"), capture_output=True)
        row7 = conn.execute("SELECT machine_zh FROM paragraphs WHERE book_id='bk'"
                            " AND para_index=7").fetchone()
        check("管道：apply - 接受 UTF-8 字节流（PS7/cmd 场景）",
              rp.returncode == 0 and row7["machine_zh"] == "全文完。（管道 UTF-8 测试）",
              f"rc={rp.returncode} err={rp.stderr[:200]!r}")
        rg = subprocess.run([sys.executable, script, "--db", db, "apply", "-"],
                            input=payload.encode("gbk"), capture_output=True)
        check("管道：GBK 字节流被拒绝并给出修复指引（不写脏数据）",
              rg.returncode == 1 and "UTF-8" in rg.stderr.decode("utf-8", "ignore")
              and row7["machine_zh"] == "全文完。（管道 UTF-8 测试）",
              f"rc={rg.returncode} err={rg.stderr[:200]!r}")
        # 术语：词界正则 + 禁译表（gcmd 子命令走真实 CLI 路径）
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',8,0,'The evening came with seven bells, and never rang"
                     " again.','夜幕降临，钟声七响，再也没有响起。',NULL,'translated','',NULL,?)",
                     (now_iso(),))
        conn.commit()
        conn.execute("INSERT INTO glossary (book_id, en, zh, forbidden_zh)"
                     " VALUES ('bk','Eve','伊芙','夏娃')")
        conn.commit()
        hit = conn.execute("SELECT COUNT(*) FROM paragraphs WHERE book_id='bk'"
                           " AND en_text REGEXP ?", (term_pattern("Eve"),)).fetchone()[0]
        like_n = conn.execute("SELECT COUNT(*) FROM paragraphs WHERE book_id='bk'"
                              " AND en_text LIKE '%Eve%'").fetchone()[0]
        check("术语：词界匹配 Eve 不再误伤 evening/seven/never（旧 LIKE 会多咬 1 段）",
              hit == 3 and like_n == 4, f"regexp={hit} like={like_n}")
        low = conn.execute("SELECT COUNT(*) FROM paragraphs WHERE book_id='bk'"
                           " AND en_text REGEXP ?", (term_pattern("eve"),)).fetchone()[0]
        check("术语：全小写术语不区分大小写（专有名词带大写则区分）", low == 3, str(low))
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',12,0,'“Eve?” asked the captain again, leaning closer.',"
                     " '“夏娃呢？”船长又问了一遍。',NULL,'translated','',NULL,?)", (now_iso(),))
        conn.commit()
        gjson = run_cmd(cmd_glossary, types.SimpleNamespace(
            book="bk", gcmd="check", dry_run=True, db=db))
        kinds = sorted(m["kind"] for m in gjson["rows"])
        check("glossary check：漏译伊芙报 missing，译文含禁译词夏娃报 forbidden",
              kinds == ["forbidden", "missing"], str(gjson["rows"]))
        # release：按已有译稿智能恢复，不盲目降级
        conn.execute("UPDATE paragraphs SET status='wip', claimed_at='2000-01-01T00:00:00'"
                     " WHERE book_id='bk' AND para_index IN (2,6)")
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',9,0,'Never finished.',NULL,NULL,'wip','',"
                     " '2000-01-01T00:00:00',?)", (now_iso(),))
        conn.commit()
        rjson = run_cmd(cmd_release, types.SimpleNamespace(book="bk", older_min=30, db=db))
        st2 = conn.execute("SELECT status FROM paragraphs WHERE book_id='bk'"
                           " AND para_index=2").fetchone()["status"]
        st9 = conn.execute("SELECT status FROM paragraphs WHERE book_id='bk'"
                           " AND para_index=9").fetchone()["status"]
        check("release：有译稿回 translated、无译稿才回 pending（不降级）",
              rjson["to_translated"] == 2 and rjson["to_pending"] == 1
              and st2 == "translated" and st9 == "pending", str(rjson))
        # qc：截断 / 未闭合引号 / 网文俗套词
        long_en = ("The captain talked for a long time about the tide, the boats, "
                   "the weather and the long winter that was coming to the island.")
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',10,0,?,?,NULL,'translated','',NULL,?)",
                     (long_en, "船长讲了很久，她的测试套路词倚在栏杆上，谈到潮汐、船只、天气，"
                               "还有即将来到岛上的漫长冬天", now_iso()))
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',11,0,?,?,NULL,'translated','',NULL,?)",
                     (long_en, "“船长讲了很久，谈到潮汐、船只、天气。", now_iso()))
        conn.commit()
        qjson = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords=None, badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=True, clear=False, cliche=True,
            rules_only=False, db=db))
        fl = {r["para_index"]: r["flags"] for r in qjson["rows"]}
        check("qc：截断译文（句末无法定标点）与俗套词同时被拦截",
              "truncated" in fl.get(10, []) and "webnovel_cliche:测试套路词" in fl.get(10, []),
              str(fl.get(10)))
        check("qc：未闭合引号被拦截",
              "unclosed_quote" in fl.get(11, []), str(fl.get(11)))
        # diff / sync-md：外部成品与库对齐、差异回写
        conn.execute("UPDATE paragraphs SET machine_zh='（此段后来补译。）'"
                     " WHERE book_id='bk' AND para_index=9")
        conn.commit()
        texts = [r["text"] for r in load_db_texts(conn, "bk", "refined", "machine")]
        edited = list(texts)
        edited[3] = (edited[3] or "") + "（手改）"
        del edited[7]
        ext = Path(td) / "ext.md"
        ext.write_text("\n\n".join(edited) + "\n", encoding="utf-8")
        djson = run_cmd(cmd_diff, types.SimpleNamespace(
            book="bk", file=str(ext), field="refined", fallback="machine",
            all=False, db=db))
        check("diff：标出 1 处内容差异 + 1 处文件缺段",
              djson["identical"] == len(texts) - 2
              and sum(1 for r in djson["rows"] if r["result"] == "differ") == 1
              and sum(1 for r in djson["rows"] if r["result"] == "missing_in_file") == 1,
              str(djson["rows"]))
        sjson = run_cmd(cmd_sync_md, types.SimpleNamespace(
            book="bk", file=str(ext), write=True, db=db))
        after = conn.execute("SELECT refined_zh, status FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=3").fetchone()
        check("sync-md：差异段回写 refined_zh 并置 refined",
              sjson["to_update"] == 1 and after["refined_zh"] == edited[3]
              and after["status"] == "refined", str(dict(after)))
        # notes 译注：独立字段、qc 免扫、导出为文末脚注
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',13,0,'The quarterback shouted hike and the tight end ran"
                     " his route.','四分卫喊了声开球口令，近端锋跑了自己的路线。',NULL,"
                     " 'translated','',NULL,?)", (now_iso(),))
        conn.commit()
        rn = run_apply({"book_id": "bk", "updates": [
            {"id": 13, "notes": "hike 为美式橄榄球开球口令；tight end 即近端锋。"}]})
        nv = conn.execute("SELECT notes, status FROM paragraphs WHERE book_id='bk'"
                          " AND para_index=13").fetchone()
        check("notes：apply 写入独立字段且不推进状态",
              rn["applied"] == 1 and nv["notes"].startswith("hike")
              and nv["status"] == "translated", str(dict(nv)))
        q2 = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords=None, badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=True, clear=False, cliche=True,
            rules_only=False, db=db))
        check("notes：qc 不扫译注，正文干净即不报",
              13 not in {x["para_index"] for x in q2["rows"]}, str(q2["rows"]))
        exp1 = str(Path(td) / "exp_notes.md")
        run_cmd(cmd_export, types.SimpleNamespace(
            book="bk", field="machine", fallback="none", bilingual=False,
            md_headings=False, sep="blank", notes="footnote", out=exp1, db=db))
        etxt = Path(exp1).read_text(encoding="utf-8")
        check("export：译注渲染为段尾角标 + 文末脚注区",
              "路线。[1]" in etxt and "译注" in etxt
              and "[1] hike 为美式橄榄球开球口令" in etxt, etxt[-160:])
        exp2 = str(Path(td) / "exp_pure.md")
        run_cmd(cmd_export, types.SimpleNamespace(
            book="bk", field="machine", fallback="none", bilingual=False,
            md_headings=False, sep="blank", notes="none", out=exp2, db=db))
        check("export --notes none：纯净稿不带注",
              "[1]" not in Path(exp2).read_text(encoding="utf-8")
              and "译注" not in Path(exp2).read_text(encoding="utf-8"))
        # ---- v1.4：rules 警惕库闭环 ----
        def expect_die(fn, fake, name):
            try:
                run_cmd(fn, fake)
            except SystemExit:
                check(name, True)
                return
            check(name, False, "未按预期退出")

        def ns_next(**kw):
            base = dict(book="bk", n=20, max_chars=None, chunks=1, before=0, after=0,
                        ids=None, from_idx=None, to_idx=None, status=None,
                        claim=False, flagged=False, db=db)
            base.update(kw)
            return types.SimpleNamespace(**base)

        expect_die(cmd_rule, types.SimpleNamespace(
            rcmd="add", book="*", cat="trap", en="[", bad="x", good="",
            note="", severity="warn", db=db), "rules：add en 正则编译失败被拒绝")
        expect_die(cmd_rule, types.SimpleNamespace(
            rcmd="add", book="*", cat="trap", en="ok", bad="(", good="",
            note="", severity="warn", db=db), "rules：add bad 正则编译失败被拒绝")
        radd = run_cmd(cmd_rule, types.SimpleNamespace(
            rcmd="add", book="bk", cat="trap", en=r"\bztest\b", bad="左测试|右测试",
            good="正解", note="", severity="warn", db=db))
        radd2 = run_cmd(cmd_rule, types.SimpleNamespace(
            rcmd="add", book="bk", cat="trap", en=r"\bztest\b", bad="左测试|右测试",
            good="正解二", note="细化", severity="block", db=db))
        check("rules：add 去重键命中时更新而非重复插入",
              radd["rule_id"] == radd2["rule_id"] and radd2["updated"]
              and conn.execute("SELECT COUNT(*) FROM rules WHERE book_id='bk'"
                               ).fetchone()[0] == 1 and radd2["severity"] == "block",
              f"{radd} {radd2}")
        d1 = run_cmd(cmd_rule, types.SimpleNamespace(rcmd="init-defaults",
                                                      file=str(seeds_path), db=db))
        d2 = run_cmd(cmd_rule, types.SimpleNamespace(rcmd="init-defaults",
                                                      file=str(seeds_path), db=db))
        check("rules：init-defaults 从种子文件导入且幂等",
              d1["inserted"] == len(SEED_RULES) and d2["inserted"] == 0,
              f"{d1} {d2}")
        for pi, en in ((19, "His zword stayed dry on the dock."),
                       (20, "He pulled down his zword by the pool."),
                       (21, "The captain laughed and waved.")):
            conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                         " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                         f" VALUES ('bk',{pi},0,?,NULL,NULL,'pending','',NULL,?)",
                         (en, now_iso()))
        conn.commit()
        nj = run_cmd(cmd_next, ns_next(ids="20"))
        hints = nj["batches"][0].get("hints", [])
        zword_hint = next((h for h in hints if "zword" in h["en_pattern"]), None)
        check("rules：next 挂载 hints（matched_ids 与字段齐全）",
              zword_hint is not None and zword_hint["matched_ids"] == [20]
              and zword_hint["forbid_zh"] and zword_hint["suggest_zh"] and zword_hint["note"],
              str(hints))
        check("rules：badword/syntax（en 为空）不挂载 hints",
              all(h["en_pattern"] for h in hints), str([h.get("category") for h in hints]))
        hc0 = conn.execute("SELECT hit_count FROM rules WHERE en_pattern=?",
                           (r"\bzword\b",)).fetchone()[0]
        check("rules：hit_count 译前挂载不计数", hc0 == 0, str(hc0))
        nj2 = run_cmd(cmd_next, ns_next(ids="21", before=2))
        check("rules：hints 只扫 target_chunk，上下文命中不挂载",
              nj2["batches"][0].get("hints", []) == [], str(nj2["batches"][0].get("hints")))
        for i in range(12):
            conn.execute("INSERT INTO rules (book_id, category, en_pattern, bad_zh,"
                         " good_zh, note, severity) VALUES ('bk','trap','.',?,'g','','warn')",
                         (f"毫毫毫毫{i}",))
        conn.commit()
        nj3 = run_cmd(cmd_next, ns_next(ids="21"))
        check("rules：hints 超 10 条截断并报 hints_truncated",
              len(nj3["batches"][0].get("hints", [])) == 10
              and nj3["batches"][0].get("hints_truncated") == 2,
              str(len(nj3["batches"][0].get("hints", []))))
        conn.execute("DELETE FROM rules WHERE book_id='bk' AND en_pattern='.'")
        conn.commit()
        rb = run_apply({"book_id": "bk", "updates": [{"id": 20, "machine_zh": "他把禁词甲拉了下来。"}]})
        row20 = conn.execute("SELECT machine_zh, status FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=20").fetchone()
        check("rules：apply block 拦截且不写库（译文/状态都不动，退出码 0）",
              rb["blocked"] == 1 and rb["applied"] == 0
              and rb["violations"][0]["flag"].startswith("rule_trap:")
              and rb["violations"][0]["suggest_zh"]
              and row20["machine_zh"] is None and row20["status"] == "pending",
              str(rb))
        rok = run_apply({"book_id": "bk", "updates": [{"id": 20, "machine_zh": "他用上了正确词。"}]})
        row20b = conn.execute("SELECT machine_zh, status FROM paragraphs WHERE book_id='bk'"
                              " AND para_index=20").fetchone()
        check("rules：apply 放行（正确译法 blocked==0 且状态推进）",
              rok["blocked"] == 0 and rok["applied"] == 1
              and row20b["machine_zh"] == "他用上了正确词。" and row20b["status"] == "translated",
              str(rok))
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',22,0,'He was deeply affected by her words.',NULL,NULL,"
                     "'pending','',NULL,?)", (now_iso(),))
        conn.commit()
        rw = run_apply({"book_id": "bk", "updates": [
            {"id": 22, "machine_zh": "他被她的话所打动了。"}]})
        row22 = conn.execute("SELECT status, error_flags FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=22").fetchone()
        check("rules：warn 规则写入并打标（不拦截）",
              rw["applied"] == 1 and rw["blocked"] == 0 and len(rw["warnings"]) == 1
              and "rule_syntax:" in (row22["error_flags"] or "")
              and row22["status"] == "translated", str(rw) + str(dict(row22)))
        conn.execute("UPDATE paragraphs SET error_flags = error_flags || ',name_mismatch'"
                     " WHERE book_id='bk' AND para_index=22")
        conn.commit()
        rf = run_apply({"book_id": "bk", "updates": [
            {"id": 22, "refined_zh": "听到这话，他心头一震。"}]})
        row22f = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                              " AND para_index=22").fetchone()["error_flags"]
        check("rules：修好重 apply 后 rule_* 自动清除、其他标记保留",
              rf["blocked"] == 0 and "rule_syntax" not in (row22f or "")
              and "name_mismatch" in row22f, row22f)
        rfo = run_apply({"book_id": "bk", "updates": [
            {"id": 20, "machine_zh": "他把禁词甲拉了下来。"}]}, force=True)
        row20fo = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                               " AND para_index=20").fetchone()["error_flags"]
        rfix = run_apply({"book_id": "bk", "updates": [
            {"id": 20, "machine_zh": "他把正确词用好了。"}]})
        row20fx = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                               " AND para_index=20").fetchone()["error_flags"]
        check("rules：apply --force 强制写入并打标留痕（修正后自动清标）",
              rfo["blocked"] == 0 and rfo["applied"] == 1 and rfo["forced"] == 1
              and 20 in rfo["forced_ids"] and "rule_trap:" in (row20fo or "")
              and rfix["blocked"] == 0 and not (row20fx or ""), str(rfo) + row20fx)
        rng = run_apply({"book_id": "bk", "updates": [
            {"id": 19, "machine_zh": "他的禁词甲停在码头。"}]}, no_gate=True)
        row19 = conn.execute("SELECT status, error_flags FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=19").fetchone()
        check("rules：apply --no-gate 跳过校验（不拦不打标）",
              rng["applied"] == 1 and rng["blocked"] == 0
              and row19["status"] == "translated" and row19["error_flags"] == "",
              str(rng) + str(dict(row19)))
        # 给 19 段换上正确译文，别让后续全书 qc 重复命中 zword
        conn.execute("UPDATE paragraphs SET machine_zh='他的正确词在码头上还是干的。'"
                     " WHERE book_id='bk' AND para_index=19")
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',23,0,'She was moved by what she said.',"
                     " '他被她说的话所感动。',NULL,'translated','',NULL,?)", (now_iso(),))
        conn.commit()
        q3 = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords=None, badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=False, clear=False, cliche=False,
            rules_only=True, db=db))
        row23 = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=23").fetchone()["error_flags"]
        q3hit = next((x for x in q3["rows"] if x["para_index"] == 23), None)
        conn.execute("UPDATE paragraphs SET machine_zh='听了她的话，他感动不已。'"
                     " WHERE book_id='bk' AND para_index=23")
        conn.commit()
        q4 = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords=None, badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=False, clear=False, cliche=False,
            rules_only=True, db=db))
        row23f = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                              " AND para_index=23").fetchone()["error_flags"]
        check("rules：qc 打标且修复后重跑自动清标",
              "rule_syntax:" in (row23 or "") and q3hit is not None
              and q3hit.get("rule_hits") and not (row23f or ""), f"{row23} -> {row23f}")
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',24,0,?,? ,NULL,'translated','',NULL,?)",
                     ("She picked up the handbag from the table and looked inside for her keys.",
                      "她把手袋从桌上拿起来，找她的钥匙。", now_iso()))
        conn.commit()
        qw1 = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords="手袋", badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=False, clear=False, cliche=False,
            rules_only=False, db=db))
        row24 = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                             " AND para_index=24").fetchone()["error_flags"]
        conn.execute("UPDATE paragraphs SET machine_zh='她把手包从桌上拿起来，找她的钥匙。'"
                     " WHERE book_id='bk' AND para_index=24")
        conn.commit()
        qw2 = run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords="手袋", badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=False, clear=False, cliche=False,
            rules_only=False, db=db))
        row24f = conn.execute("SELECT error_flags FROM paragraphs WHERE book_id='bk'"
                              " AND para_index=24").fetchone()["error_flags"]
        check("qc：badword 标记随重算自愈（v1.3.0 残留 bug 修复回归）",
              "badword:手袋" in (row24 or "") and not (row24f or ""),
              f"{row24} -> {row24f}")
        conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                     " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                     " VALUES ('bk',25,0,'She was persuaded by his words.',"
                     " '她被他的话所说服。',NULL,'translated','',NULL,?)", (now_iso(),))
        conn.commit()
        run_cmd(cmd_qc, types.SimpleNamespace(
            book="bk", badwords=None, badwords_file=None, save=False,
            min_ratio=0.15, max_ratio=3.0, dry_run=False, clear=False, cliche=False,
            rules_only=True, db=db))
        fl = run_cmd(cmd_next, ns_next(flagged=True, n=10))
        tgt = [t for b in fl["batches"] for t in b["target_chunk"]]
        check("rules：next --flagged 取病灶段（error_flags + flag_details 规则卡）",
              fl["status"] == "flagged" and tgt
              and all(t.get("error_flags") for t in tgt)
              and any(b.get("flag_details") for b in fl["batches"])
              and fl["remaining"] == len(tgt), str(fl.get("remaining")) + str(tgt))
        expect_die(cmd_next, ns_next(flagged=True, status="pending"),
                   "rules：--flagged 与 --status 互斥报错")
        hc = conn.execute("SELECT hit_count FROM rules WHERE en_pattern=?",
                          (r"\bzword\b",)).fetchone()[0]
        check("rules：hit_count 只在译后命中时递增（block+force 各 1）", hc == 2, str(hc))
        conn.execute("INSERT INTO rules (book_id, category, en_pattern, bad_zh,"
                     " good_zh, note, severity) VALUES ('bk','trap','[','卡卡卡','g','','block')")
        conn.commit()
        nbad = run_cmd(cmd_next, ns_next(ids="21"))
        abad = run_apply({"book_id": "bk", "updates": [{"id": 21, "machine_zh": "他大笑起来。"}]})
        check("rules：库内坏正则被跳过，next/apply 不崩",
              isinstance(nbad.get("batches"), list) and abad["applied"] == 1
              and abad["blocked"] == 0, str(abad))
        # ---- v1.5：issue_staging 错题池 ----
        def ns_issue(**kw):
            base = dict(icmd="list", book=None, index=None, cat=None, en="", bad=None,
                        good="", note=None, status="open", limit=30, full=False,
                        id=None, severity="warn", db=db)
            base.update(kw)
            return types.SimpleNamespace(**base)

        for pi, en, mzh in (
                (26, "The river bent west. He sat down on the bank and waited.",
                 "他坐在银行上等着。"),
                (27, "She joined him on the bank a minute later.", "她也在银行上坐下。"),
                (28, "She was startled by the sudden noise.", "她被突然的声响所惊吓。")):
            conn.execute("INSERT INTO paragraphs (book_id, para_index, is_heading, en_text,"
                         " machine_zh, refined_zh, status, error_flags, claimed_at, updated_at)"
                         f" VALUES ('bk',{pi},0,?,?,NULL,'translated','',NULL,?)",
                         (en, mzh, now_iso()))
        conn.commit()
        ia = run_cmd(cmd_issue, ns_issue(icmd="add", book="bk", index=25, cat="syntax",
                                         en="persuaded", good="听了她的话，他同意了。",
                                         note="被…所说服 欧化被动，应改主动句"))
        irow = conn.execute("SELECT * FROM issue_staging WHERE issue_id=?",
                            (ia["issue_id"],)).fetchone()
        check("issues：add 自动抓取该段 en_text 与当前译文作 bad",
              irow["en_text"] == "She was persuaded by his words."
              and irow["bad_zh"] == "她被他的话所说服。"
              and irow["good_zh"] == "听了她的话，他同意了。"
              and irow["status"] == "open", str(dict(irow)))
        ra = run_apply({"book_id": "bk", "updates": [
            {"id": 23, "refined_zh": "听了她的话，他心头一暖。",
             "issue_cat": "trap", "issue_en": "moved", "issue_note": "moved 此处指感动"}]})
        row_a = conn.execute("SELECT * FROM issue_staging WHERE para_index=23"
                             " AND category='trap'").fetchone()
        check("issues：apply 带 issue 字段入池（首次精修 bad=旧 machine_zh）",
              ra["staged"] == 1 and row_a["bad_zh"] == "听了她的话，他感动不已。"
              and row_a["good_zh"] == "听了她的话，他心头一暖。"
              and row_a["issue_en"] == "moved", str(ra) + str(dict(row_a)))
        rb = run_apply({"book_id": "bk", "updates": [
            {"id": 23, "refined_zh": "她的话让他心头一暖。",
             "issue_cat": "style", "issue_note": "更紧凑的主谓句"}]})
        row_b = conn.execute("SELECT * FROM issue_staging WHERE para_index=23"
                             " AND category='style'").fetchone()
        check("issues：apply 重修场景 bad=旧 refined_zh",
              rb["staged"] == 1 and row_b["bad_zh"] == "听了她的话，他心头一暖。"
              and row_b["good_zh"] == "她的话让他心头一暖。", str(dict(row_b)))
        n_before = conn.execute("SELECT COUNT(*) FROM issue_staging").fetchone()[0]
        rblk = run_apply({"book_id": "bk", "updates": [
            {"id": 19, "refined_zh": "他的禁词甲在码头上还是干的。",
             "issue_cat": "trap", "issue_note": "zword 误译"}]})
        check("issues：block 拦截段不入池（拒写无对照）",
              rblk["blocked"] == 1 and rblk["staged"] == 0
              and conn.execute("SELECT COUNT(*) FROM issue_staging").fetchone()[0] == n_before,
              str(rblk))
        rno = run_apply({"book_id": "bk", "updates": [
            {"id": 22, "notes": "备注", "issue_cat": "trap", "issue_note": "x"}]})
        check("issues：未写译文的条目不入池（译文照写）",
              rno["applied"] == 1 and rno["staged"] == 0
              and conn.execute("SELECT COUNT(*) FROM issue_staging").fetchone()[0] == n_before,
              str(rno))
        rin = run_apply({"book_id": "bk", "updates": [
            {"id": 22, "refined_zh": "听到这话，他心头猛地一震。",
             "issue_cat": "xx", "issue_note": "y"}]})
        check("issues：非法 issue_cat 跳过入池但译文照写",
              rin["applied"] == 1 and rin["staged"] == 0
              and conn.execute("SELECT COUNT(*) FROM issue_staging").fetchone()[0] == n_before,
              str(rin))
        rr_dry = run_cmd(cmd_replace, types.SimpleNamespace(
            book="bk", find="手袋", replacement="手包", field="machine", if_en="handbag",
            regex=False, dry_run=True, log_issue="手袋→手包，handbag 规范译名", db=db))
        n_after_dry = conn.execute("SELECT COUNT(*) FROM issue_staging").fetchone()[0]
        rr = run_cmd(cmd_replace, types.SimpleNamespace(
            book="bk", find="手袋", replacement="手包", field="machine", if_en="handbag",
            regex=False, dry_run=False, log_issue="手袋→手包，handbag 规范译名", db=db))
        row_r = conn.execute("SELECT * FROM issue_staging WHERE note LIKE '手袋→手包%'"
                             ).fetchone()
        check("issues：replace --log-issue 采样首个命中段（dry-run 不入池）",
              rr_dry["staged"] == 0 and rr_dry["dry_run"] and n_after_dry == n_before
              and rr["staged"] == 1 and row_r["para_index"] == 3
              and row_r["bad_zh"] == "夏娃拎着一只手袋和一瓶白兰地。"
              and row_r["good_zh"] == "夏娃拎着一只手包和一瓶白兰地。"
              and row_r["issue_en"] == "handbag" and row_r["category"] == "trap",
              str(rr) + str(dict(row_r or {})))
        r26 = run_apply({"book_id": "bk", "updates": [
            {"id": 26, "refined_zh": "他在河岸上坐下，等着。",
             "issue_cat": "trap", "issue_en": "bank",
             "issue_note": "bank 此处指河岸，不是银行"}]})
        r28 = run_apply({"book_id": "bk", "updates": [
            {"id": 28, "refined_zh": "突然的声响吓了她一跳。",
             "issue_cat": "syntax", "issue_en": "startled",
             "issue_note": "被…所惊吓 欧化被动，应改主动句"}]})
        il = run_cmd(cmd_issue, ns_issue(icmd="list"))
        item26 = next((x for x in il["issues"] if x["para_index"] == 26), None)
        ilf = run_cmd(cmd_issue, ns_issue(icmd="list", full=True))
        item26f = next((x for x in ilf["issues"] if x["para_index"] == 26), None)
        check("issues：list 截句展示（en 取含触发词的句子）与 --full 全文",
              r26["staged"] == 1 and item26 is not None
              and item26["en_text"] == "He sat down on the bank and waited."
              and item26f["en_text"].startswith("The river bent west."),
              str(item26))
        ilit = run_cmd(cmd_issue, ns_issue(icmd="list", cat="syntax"))
        check("issues：list 按 cat 过滤",
              ilit["total"] >= 2
              and all(x["category"] == "syntax" for x in ilit["issues"]), str(ilit["total"]))
        iid26 = conn.execute("SELECT issue_id FROM issue_staging WHERE para_index=26"
                             ).fetchone()[0]
        pr = run_cmd(cmd_issue, ns_issue(icmd="promote", id=str(iid26),
                                         en=r"\bthe\s+bank\b", bad="银行",
                                         good="河岸/堤岸"))
        prow = conn.execute("SELECT status, rule_id FROM issue_staging WHERE issue_id=?",
                            (iid26,)).fetchone()
        rrule = conn.execute("SELECT book_id, category, severity, note FROM rules"
                             " WHERE rule_id=?", (pr["rule_id"],)).fetchone()
        check("issues：promote 默认 warn + 单书作用域 + 继承 cat/note + 回填 rule_id",
              pr["severity"] == "warn" and pr["book_id"] == "bk"
              and rrule["severity"] == "warn" and rrule["book_id"] == "bk"
              and rrule["category"] == "trap"
              and rrule["note"] == "bank 此处指河岸，不是银行"
              and prow["status"] == "promoted" and prow["rule_id"] == pr["rule_id"],
              str(pr) + str(dict(rrule)))
        check("issues：promote 返回 preview 命中统计",
              pr["preview"]["book"] == "bk" and pr["preview"]["en_matched"] == 2
              and pr["preview"]["flagged"] == 1
              and pr["preview"]["sample_ids"] == [27], str(pr.get("preview")))
        iid25 = ia["issue_id"]
        iid28 = conn.execute("SELECT issue_id FROM issue_staging WHERE para_index=28"
                             ).fetchone()[0]
        pr2 = run_cmd(cmd_issue, ns_issue(icmd="promote", id=f"{iid25},{iid28}",
                                          bad="被[^，。]{1,8}所", good="改为主动白描句"))
        st25 = conn.execute("SELECT status, rule_id FROM issue_staging WHERE issue_id=?",
                            (iid25,)).fetchone()
        st28 = conn.execute("SELECT status, rule_id FROM issue_staging WHERE issue_id=?",
                            (iid28,)).fetchone()
        check("issues：promote 多 id 转正（cat/note 继承，回填同一 rule_id）",
              pr2["category"] == "syntax" and pr2["severity"] == "warn"
              and st25["status"] == "promoted" and st28["status"] == "promoted"
              and st25["rule_id"] == st28["rule_id"] == pr2["rule_id"], str(pr2))
        iid3 = conn.execute("SELECT issue_id FROM issue_staging WHERE para_index=3"
                            ).fetchone()[0]
        iid_style = conn.execute("SELECT issue_id FROM issue_staging WHERE category='style'"
                                 ).fetchone()[0]
        expect_die(cmd_issue, ns_issue(icmd="promote", id=str(iid3), en="[", bad="x"),
                   "issues：promote 坏正则整单拒绝且错题仍 open")
        st3 = conn.execute("SELECT status FROM issue_staging WHERE issue_id=?",
                           (iid3,)).fetchone()["status"]
        check("issues：promote 拒绝后错题状态未变", st3 == "open", st3)
        expect_die(cmd_issue, ns_issue(icmd="promote", id=f"{iid3},{iid_style}"),
                   "issues：promote category 不一致未指定 --cat 报错")
        ig = run_cmd(cmd_issue, ns_issue(icmd="ignore", id=f"{iid3},{iid_style}"))
        n_ig = conn.execute("SELECT COUNT(*) FROM issue_staging WHERE status='ignored'"
                            ).fetchone()[0]
        check("issues：ignore 批量忽略", ig["ignored"] == 2 and n_ig == 2, str(ig))
        conn.close()
        import gc
        gc.collect()  # 回收测试中未显式 close 的连接，Windows 下才能删掉临时库
    if saved_seeds_env is None:
        os.environ.pop("TRANSDB_SEEDS", None)
    else:
        os.environ["TRANSDB_SEEDS"] = saved_seeds_env
    failed = [c for c in checks if not c[1]]
    out_json({"passed": len(checks) - len(failed), "failed": len(failed),
              "cases": [c[0] for c in checks if not c[1]]})
    if failed:
        sys.exit(1)
    info(f"自测全部通过（{len(checks)} 项）")


# ---------------------------------------------------------------- 参数解析

def add_db(sp):
    sp.add_argument("--db", default=argparse.SUPPRESS,
                    help="数据库路径（默认 translations.db 或环境变量 TRANSDB_DB）")


def build_parser():
    p = argparse.ArgumentParser(
        prog="transdb", description="transdb —— 小说翻译流水线 SQLite 工具（零依赖）",
        epilog="详细说明见 README.md。数据输出为 JSON（stdout），日志走 stderr。")
    p.add_argument("--version", action="version", version=f"transdb {VERSION}")
    p.add_argument("--db", default=None, help="数据库路径（默认 translations.db）")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    sp = sub.add_parser("init", help="初始化/检查数据库")
    add_db(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入原文 txt，按段落入库")
    sp.add_argument("source", help="原文文件路径（utf-8 / gbk 自动识别）")
    sp.add_argument("--book", required=True, help="书目 ID（如 novel_p1）")
    sp.add_argument("--split", choices=["blank", "line"], default="blank",
                    help="blank=空行分段(默认,英文硬换行自动合并) line=每行一段")
    sp.add_argument("--no-join", action="store_true", help="blank 模式下不合并段内换行")
    sp.add_argument("--keep-numbers", action="store_true", help="保留纯数字行（默认视为页码丢弃）")
    sp.add_argument("--dual", action="store_true",
                    help="双语对照稿导入：块级解析（空行分块 + 块内按语言切分），"
                         "兼容英中同块成对/各占一段/逐行交替三种格式。中文直接入 "
                         "machine_zh 并置 translated（跳过机翻阶段），原稿漏译段置 "
                         "pending 待补译")
    sp.add_argument("--force", action="store_true", help="书目已存在时推翻重导（删旧段落与译文）")
    add_db(sp)
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("books", help="列出所有书目及进度")
    sp.add_argument("--json", action="store_true")
    add_db(sp)
    sp.set_defaults(func=cmd_books)

    sp = sub.add_parser("stats", help="查看某书目的分段状态统计")
    sp.add_argument("book")
    sp.add_argument("--json", action="store_true")
    add_db(sp)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("next", help="取下一批待处理段落（滑动窗口+上下文，JSON）")
    sp.add_argument("book")
    sp.add_argument("--n", type=int, default=20, help="每批段数上限（默认 20）")
    sp.add_argument("--max-chars", type=int, default=None,
                    help="每批字符预算：累计超过即截断（与 --n 同时生效，先到为准；"
                         "单个超长段落独占一批）")
    sp.add_argument("--chunks", type=int, default=1, help="一次取几个批次（默认 1）")
    sp.add_argument("--before", type=int, default=2,
                    help="滑动窗口：每批附带前 N 段作上下文（默认 2，含已有译文；0 关闭）")
    sp.add_argument("--after", type=int, default=1,
                    help="滑动窗口：每批附带后 N 段开头（默认 1，截前 300 字符；0 关闭）")
    sp.add_argument("--ids", help="显式指定本批段落号（逗号分隔，如 101,102,105），忽略状态")
    sp.add_argument("--from", dest="from_idx", type=int, help="显式区间起点（与 --to 成对）")
    sp.add_argument("--to", dest="to_idx", type=int, help="显式区间终点（与 --from 成对）")
    sp.add_argument("--status", default=None, choices=list(VALID_STATUS),
                    help="取哪种状态（机翻阶段 pending，精修阶段 translated；默认 pending）")
    sp.add_argument("--flagged", action="store_true",
                    help="取带质检标记的病灶段（不限状态；与 --status/--ids/--from/--to "
                         "互斥；target_chunk 带 error_flags，批次带 flag_details 规则卡）")
    sp.add_argument("--claim", action="store_true",
                    help="同时把批次标为 wip（独占事务原子抢占，并行会话绝不重复取段）")
    add_db(sp)
    sp.set_defaults(func=cmd_next)

    sp = sub.add_parser("show", help="查看段落全文（单段或区间）")
    sp.add_argument("book")
    sp.add_argument("--index", type=int)
    sp.add_argument("--from", dest="from_idx", type=int)
    sp.add_argument("--to", dest="to_idx", type=int)
    sp.add_argument("--fields", choices=["all", "en", "machine", "refined", "notes"],
                    default="all")
    add_db(sp)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("apply", help="把译文批量写回数据库（JSON 文件，原子事务，状态自动推进，"
                                      "规则门禁实时拦截）")
    sp.add_argument("file", help="更新 JSON 路径（- = stdin，须 UTF-8 字节流；"
                                 "Windows PS 5.1 管道会损坏中文，建议一律用文件）")
    sp.add_argument("--book", help="覆盖 JSON 里的 book_id")
    sp.add_argument("--allow-partial", action="store_true", help="跳过非法条目而非整体报错")
    sp.add_argument("--force", action="store_true",
                    help="block 级规则拦截时强制写入（仍打 rule_* 标记留痕，交付前复核）")
    sp.add_argument("--no-gate", dest="no_gate", action="store_true",
                    help="跳过规则门禁（不拦、不打标、不清标）")
    add_db(sp)
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("set-status", help="手工改状态（整段重做时用）")
    sp.add_argument("book")
    sp.add_argument("--index", type=int)
    sp.add_argument("--from", dest="from_idx", type=int)
    sp.add_argument("--to", dest="to_idx", type=int)
    sp.add_argument("--status", required=True, choices=list(VALID_STATUS))
    add_db(sp)
    sp.set_defaults(func=cmd_set_status)

    sp = sub.add_parser("release", help="释放卡死的 wip 段落（按已有译稿恢复，不降级）")
    sp.add_argument("book")
    sp.add_argument("--older-min", type=int, default=30, help="占用超过多少分钟才释放（默认 30）")
    add_db(sp)
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("search", help="按中文或英文关键词检索段落")
    sp.add_argument("book")
    sp.add_argument("--zh", help="中文关键词（默认同时搜机翻与精修）")
    sp.add_argument("--en", help="英文关键词（搜原文，不区分大小写）")
    sp.add_argument("--field", choices=["any", "refined", "machine"], default="any",
                    help="限定中文搜索的字段")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--full", action="store_true", help="不截断，返回全文")
    add_db(sp)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("replace", help="全局安全替换（可限定原文含某英文词才替换）")
    sp.add_argument("book")
    sp.add_argument("--find", required=True, help="要找的字符串（或 --regex 时的正则）")
    sp.add_argument("--replace", dest="replacement", required=True, help="替换为")
    sp.add_argument("--field", choices=["refined", "machine", "both"], default="refined")
    sp.add_argument("--if-en", help="仅当原文包含这些英文词（逗号分隔，整词匹配，"
                                    "Eve 不吃 evening）才替换")
    sp.add_argument("--regex", action="store_true", help="把 --find 当正则")
    sp.add_argument("--dry-run", action="store_true", help="只预览不写库")
    sp.add_argument("--log-issue", dest="log_issue", nargs="?", const="", default=None,
                    help="替换执行时把首个命中段采样入错题池（可跟一句备注；"
                         "dry-run 不写库也不入池）")
    add_db(sp)
    sp.set_defaults(func=cmd_replace)

    sp = sub.add_parser("qc", help="质检：长度比/无中文/截断/引号闭合/词表 + rules 警惕库扫描，"
                                   "结果写入 error_flags")
    sp.add_argument("book")
    sp.add_argument("--badwords", help="机翻腔/危险词列表（逗号分隔）")
    sp.add_argument("--badwords-file", help="词表文件（每行一词，# 注释）")
    sp.add_argument("--save", action="store_true", help="把词表存到书目配置，以后免传")
    sp.add_argument("--cliche", action="store_true",
                    help="启用俗套词扫描（词表来自 transdb_seeds.json 的 cliches 字段，"
                         "标记为 webnovel_cliche:*；工具不内置词表）")
    sp.add_argument("--rules-only", dest="rules_only", action="store_true",
                    help="仅跑 rules 表规则扫描（跳过长度/标点/词表等内置检查）")
    sp.add_argument("--min-ratio", type=float, default=0.15, help="译文/原文 长度比下限")
    sp.add_argument("--max-ratio", type=float, default=3.0, help="译文/原文 长度比上限")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--clear", action="store_true", help="清除该书全部质检标记")
    add_db(sp)
    sp.set_defaults(func=cmd_qc)

    sp = sub.add_parser("glossary", help="人名/术语表：登记与全文一致性检查")
    gsub = sp.add_subparsers(dest="gcmd", metavar="子命令")
    g = gsub.add_parser("add", help="登记术语：glossary add 书目 英文 中文")
    g.add_argument("book"); g.add_argument("en_term"); g.add_argument("zh_term")
    g.add_argument("--note", default="")
    g.add_argument("--forbidden", default="",
                   help="禁译词表：绑定时译文里不允许出现的中文（逗号分隔，如 夏娃,前夕）")
    add_db(g); g.set_defaults(func=cmd_glossary, gcmd="add")
    g = gsub.add_parser("load", help="从 TSV 批量导入（英文<TAB>中文[<TAB>禁译词表]）")
    g.add_argument("book"); g.add_argument("file")
    add_db(g); g.set_defaults(func=cmd_glossary, gcmd="load")
    g = gsub.add_parser("list", help="列出术语")
    g.add_argument("book")
    add_db(g); g.set_defaults(func=cmd_glossary, gcmd="list")
    g = gsub.add_parser("remove", help="删除术语")
    g.add_argument("book"); g.add_argument("en_term")
    add_db(g); g.set_defaults(func=cmd_glossary, gcmd="remove")
    g = gsub.add_parser("check", help="全文一致性检查（整词匹配 + 禁译词表双向核验）")
    g.add_argument("book")
    g.add_argument("--dry-run", action="store_true")
    add_db(g); g.set_defaults(func=cmd_glossary, gcmd="check")

    sp = sub.add_parser("rule", help="警惕库：译前提示 / 译后门禁规则的管理与沉淀")
    rsub = sp.add_subparsers(dest="rcmd", metavar="子命令")
    r = rsub.add_parser("add", help="添加/更新规则（去重键命中则只更新不改 hit_count）")
    r.add_argument("--cat", required=True, choices=list(RULE_CATEGORIES),
                   help="trap=双语消歧 badword=纯中文禁词 syntax=句式反模式 style=黄金范例")
    r.add_argument("--book", default="*", help="适用书目（默认 '*' 全局通用）")
    r.add_argument("--en", default="", help="英文触发正则（IGNORECASE；trap 必填；"
                                            "PS 里用单引号包裹防转义）")
    r.add_argument("--bad", default="", help="禁用/劣质中文正则（style 不允许）")
    r.add_argument("--good", default="", help="推荐译法或正面改写示范")
    r.add_argument("--note", default="", help="给 Agent 看的简短原因")
    r.add_argument("--severity", default="warn", choices=["block", "warn", "hint"],
                   help="block=写回拦截 warn=写入打标 hint=仅译前挂载"
                        "（默认 warn，确认无误再升 block）")
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="add")
    r = rsub.add_parser("list", help="列出规则（含 hit_count）")
    r.add_argument("--book", default=None, help="按 book_id 过滤（'*' 或具体书目）")
    r.add_argument("--cat", choices=list(RULE_CATEGORIES), default=None)
    r.add_argument("--top", type=int, default=None, help="按 hit_count 降序取前 N（找最活跃规则）")
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="list")
    r = rsub.add_parser("remove", help="按 rule_id 删除规则")
    r.add_argument("--id", type=int, required=True)
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="remove")
    r = rsub.add_parser("load", help="从 JSON 批量导入（按去重键合并；任一条非法整批拒绝）")
    r.add_argument("file")
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="load")
    r = rsub.add_parser("dump", help="导出规则库为 JSON（--out 落盘，默认 stdout）")
    r.add_argument("--book", default=None, help="按 book_id 过滤")
    r.add_argument("--out", default=None)
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="dump")
    r = rsub.add_parser("init-defaults", help="从种子文件导入规则（幂等；默认 transdb_seeds.json，"
                                              "工具不内置任何规则数据）")
    r.add_argument("file", nargs="?", default=None,
                   help="种子 JSON 路径（默认 transdb_seeds.json 或 TRANSDB_SEEDS 环境变量）")
    add_db(r); r.set_defaults(func=cmd_rule, rcmd="init-defaults")

    sp = sub.add_parser("issue", help="错题池：翻译/精修途中顺手记原始错例，事后批量提炼为 rules 规则")
    isub = sp.add_subparsers(dest="icmd", metavar="子命令")
    i = isub.add_parser("add", help="登记错题：自动抓取该段原文与当前译文作对照")
    i.add_argument("book")
    i.add_argument("--index", type=int, required=True, help="段落号（para_index）")
    i.add_argument("--cat", choices=list(RULE_CATEGORIES), default="trap",
                   help="trap=词义/俚语 badword=禁词 syntax=句式/被动 style=好句积累（默认 trap）")
    i.add_argument("--en", default="", help="英文触发词（展示截句锚点，如 bank）")
    i.add_argument("--bad", default=None, help="错误译文片段/全文（默认自动抓当前译文）")
    i.add_argument("--good", default="", help="修正后的译文（可选）")
    i.add_argument("--note", default="", help="一句话病因分析")
    add_db(i); i.set_defaults(func=cmd_issue, icmd="add")
    i = isub.add_parser("list", help="查看错题池（默认截句展示 + 分页，--full 看全文快照）")
    i.add_argument("--book", default=None)
    i.add_argument("--status", choices=list(ISSUE_STATUSES) + ["all"], default="open")
    i.add_argument("--cat", choices=list(RULE_CATEGORIES), default=None)
    i.add_argument("--limit", type=int, default=30, help="每页条数（默认 30）")
    i.add_argument("--full", action="store_true", help="输出全文快照（默认截句）")
    add_db(i); i.set_defaults(func=cmd_issue, icmd="list")
    i = isub.add_parser("promote", help="错题批量转正为正式规则（复用 rule add 校验；"
                                        "默认 warn + 单书作用域，返回命中预览）")
    i.add_argument("--id", required=True, help="错题号，逗号分隔（如 15,16,18）")
    i.add_argument("--cat", choices=list(RULE_CATEGORIES), default=None,
                   help="默认继承所选错题的 category")
    i.add_argument("--en", default="", help="英文触发正则（PS 里用单引号包裹防转义）")
    i.add_argument("--bad", default="", help="禁用/劣质中文正则")
    i.add_argument("--good", default="", help="推荐译法（不继承错题的整段 good）")
    i.add_argument("--note", default=None, help="默认继承所选错题的 note")
    i.add_argument("--severity", default="warn", choices=["block", "warn", "hint"],
                   help="默认 warn——先 warn 后 block，确认无误再升（铁律）")
    i.add_argument("--book", default=None,
                   help="作用域：默认单书（错题来源书目），'*' 才全局通用")
    add_db(i); i.set_defaults(func=cmd_issue, icmd="promote")
    i = isub.add_parser("ignore", help="忽略偶发/无需入库的错题")
    i.add_argument("--id", required=True, help="错题号，逗号分隔")
    add_db(i); i.set_defaults(func=cmd_issue, icmd="ignore")

    sp = sub.add_parser("export", help="按段落顺序导出成品文件")
    sp.add_argument("book")
    sp.add_argument("--field", choices=["refined", "machine", "en"], default="refined")
    sp.add_argument("--fallback", choices=["machine", "refined", "en", "none"],
                    default="machine", help="目标字段为空时的回退（默认 machine）")
    sp.add_argument("--bilingual", action="store_true", help="原文+译文对照导出")
    sp.add_argument("--md-headings", action="store_true", help="标题段落加 ## 前缀")
    sp.add_argument("--sep", choices=["blank", "single"], default="blank",
                    help="段落间隔（blank=空行，single=单换行）")
    sp.add_argument("--notes", choices=["footnote", "none"], default="footnote",
                    help="译注导出形态（默认 footnote=正文尾角标+文末脚注区；"
                         "none=纯净稿不带注）")
    sp.add_argument("--out", required=True, help="输出文件路径")
    add_db(sp)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("diff", help="外部成品文件与库内段落对齐比对（标出错位/差异行号）")
    sp.add_argument("book")
    sp.add_argument("--file", required=True, help="外部 Markdown/txt 成品路径")
    sp.add_argument("--field", choices=["refined", "machine", "en"], default="refined")
    sp.add_argument("--fallback", choices=["machine", "refined", "en", "none"],
                    default="machine", help="目标字段为空时的回退（默认 machine）")
    sp.add_argument("--all", action="store_true", help="连完全一致的段也一并列出")
    add_db(sp)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("sync-md", help="把外部编辑后的成品回写库内 refined_zh"
                                        "（默认 dry-run，--write 实写）")
    sp.add_argument("book")
    sp.add_argument("--file", required=True, help="外部 Markdown/txt 成品路径")
    sp.add_argument("--write", action="store_true", help="实写（默认只预览）")
    add_db(sp)
    sp.set_defaults(func=cmd_sync_md)

    sp = sub.add_parser("sql", help="直接执行 SQL（默认只读；写库需 --write）")
    sp.add_argument("query")
    sp.add_argument("--write", action="store_true")
    add_db(sp)
    sp.set_defaults(func=cmd_sql)

    sp = sub.add_parser("selftest", help="端到端自测（临时目录，不碰真实数据）")
    add_db(sp)
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv=None):
    force_utf8_stdio()
    a = build_parser().parse_args(argv)
    if getattr(a, "func", None) is None:
        build_parser().print_help()
        return 0
    a.func(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
