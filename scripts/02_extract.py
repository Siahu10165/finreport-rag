"""从年报 PDF 提取正文文字与表格，表格按行列结构还原。

输出 data/text/<code>.jsonl，每行一个「页」：
    {"page": 6, "section": "管理层讨论与分析", "text": "...", "tables": [...]}

难点与对策（真实踩过的坑）：
  * 这些年报大多是设计精美的艺术排版，章节不是「第X节」纯文本行，
    PyMuPDF 逐行扫「第一节」一个都抓不到（实测中国平安 370 页 0 命中）。
    对策：解析目录页拿到「章节名 + 起始页码」，建立页码区间→章节的映射。
  * 目录页的版式五花八门：有的「章节名」和「页码」在同一行、有的分两列、
    有的章节名换行。这里用「短行 + 相邻数字行」配对，再人工兜底。
  * pdfplumber 抽表对无框线表格效果差，会出现整页被当成一张表。
    对策：按表格行数/列数/单元格密度过滤，并保留原始行列。

要点（对照课件「提取样本」页）：
  * 每页保留页码标记，检索结果才能标出「第几页」
  * 表格保留行列原样：科目 | 本期 | 上期 这样能被人读、也能被检索
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PDF_DIR, TEXT_DIR, now_str  # noqa: E402

# 页眉页脚噪声
NOISE_RE = re.compile(
    r"^(证券代码|证券简称|股票代码|股票简称|年度报告|半年度报告|"
    r"第\s*\d+\s*页|©|www\.|http|本公司不保证|请务必阅读)"
)
# 目录页里常见的非章节行
TOC_NOISE = re.compile(
    r"^(目录|备查文件目录|释义$|contents|CONTENTS|前导信息|"
    r"载有|在公司|报告期|年度报告摘要|图片索引|"
    r"\d{4}\s*年年度报告$|年年度报告$|"
    r"第\s*\d+\s*页|第[一二三四五六七八九十]+节$)"  # 光秃秃的「第X节」不是章节名
)
PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
# 常见章节名（用于目录无页码时按出现顺序回填）
KNOWN_SECTIONS = (
    "释义", "公司简介", "公司信息", "公司概况", "公司基本信息", "公司业务概要",
    "核心竞争力分析", "经营情况讨论与分析", "管理层讨论与分析", "经营分析与战略",
    "重要事项", "股份变动及股东情况", "普通股股份变动及股东情况", "债券相关情况",
    "财务报告", "财务报告及备查文件", "公司治理", "环境与社会责任", "环境和社会",
    "审计报告", "年度财务报告", "补充资料", "备查文件目录", "信息披露索引",
    "董事长致辞", "总裁致辞", "首席执行官致辞", "经营亮点", "财务摘要", "风险因素",
    "业务概要", "业务综述", "经营概览", "经营业绩", "利润表", "资产负债表",
    "现金流量表", "财务报表", "股东情况", "关联交易", "募集资金", "重大关联交易",
    "董事、高级管理人员和员工情况", "企业管治报告", "董事会报告", "合并财务报表",
    "综合金融", "医疗养老", "业绩综述", "内含价值", "内含价值分析", "利源分析",
    "风险管理", "流动性及资本资源", "主要财务指标", "致股东函", "致股东报告",
    "备查文件目录及信息披露索引", "其他信息", "关于公司", "经营情况",
)


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


def clean_line(s: str) -> str:
    s = s.replace("\xa0", " ").strip()
    return "" if NOISE_RE.match(s) else s


# ---------------------------------------------------------------- 目录解析


# 标准节名行：「第一节 重要提示、目录和释义 .... 2」
STD_SEC_RE = re.compile(r"^第[一二三四五六七八九十]+\s*节\s*(?P<name>\S.*)$")
STD_SEC_ALONE_RE = re.compile(r"^第[一二三四五六七八九十]+\s*节\s*$")
SEQ_NAME_RE = re.compile(r"^\d{1,2}\s+\S")


def _looks_like_toc_line(ln: str) -> bool:
    """粗判一行是否像目录项（用于挑选目录页，不必精确）。"""
    if not ln or len(ln) > 60:
        return False
    if STD_SEC_RE.match(ln) or SEQ_NAME_RE.match(ln):
        return True
    return _valid_toc_name(ln, loose=True)


def parse_toc(doc) -> list[tuple[str, int]]:
    """从目录页解析出 [(章节名, 起始页码)]。

    目录页的版式实测有八种，全部要兼容（这是真实踩过的坑）：
      A. 「管理层讨论与分析 23」同行，页码带前导点或空格
      B. 章节名单独一行，下一行是纯页码            （中信证券）
      C. 「03 管理层讨论与分析」带序号前缀        （中国人寿）
      D. 章节名只有文字、页码在目录里缺失          （中国人保，需按出现顺序推页）
      E. 「第一节 重要提示、目录和释义」标准节名   （广发证券）
      F. 「002\\t 释义」页码在前、章节名在后      （中金公司）
      G. 「第一节  释义/12」页码用斜杠紧贴        （招商证券）
      H. 页码整列 + 章节名整列，行序完全错乱      （国信证券 / 浙商证券 / 华泰证券）
      I. 「15 会计数据和业务数据摘要」页码在前同行（太保 / 新华保险）

    H/I/F 这类靠「读行序」是读不出来的——PyMuPDF 的 get_text 按文本块输出，
    页码块和名称块谁先谁后完全看排版（实测国信 9 个页码全在前、9 个章节名全在后）。
    所以主路径改成「按词坐标 y 邻近配对」，行序解析只当兜底。
    """
    toc = []
    pages = _toc_pages(doc)
    for pno in pages:
        toc.extend(parse_toc_by_coords(doc, pno))
    if pages:
        log(f"    目录页 {[p + 1 for p in pages]} 坐标法共配出 {len(toc)} 项")
    if not pages:
        return []

    # 行序法一并跑，**与坐标法合并**而不是二选一。
    # 原因：招商/东吴的目录页上半是「关于我们/经营分析/公司治理/财务报告」
    # 四个大板块的速览（页码 1/2/3/4），下半才是真目录（页码两位数）。
    # 只用坐标法会只抓到板块速览，只用行序法会漏掉拆行的节名。
    merged = list(toc)
    seen_pairs = {(n, p) for n, p in toc}
    for n, p in parse_toc_by_lines(doc, pages[0]):
        if (n, p) not in seen_pairs:
            merged.append((n, p))
            seen_pairs.add((n, p))
    return merged


def _toc_pages(doc) -> list[int]:
    """找出所有目录页（可能连续多页，如中国太保的目录占了 15、16 两页）。

    判据：含「目录」二字，且页面上的「页码词 + 章节名词」数量够多。
    """
    scored: list[tuple[int, int]] = []
    for pno in range(min(24, doc.page_count)):
        text = doc[pno].get_text("text") or ""
        if "目录" not in text and "目 录" not in text:
            continue
        hits = _count_toc_items(doc[pno])
        if hits >= 4:
            scored.append((pno, hits))
    if not scored:
        return []
    best = max(h for _p, h in scored)
    # 保留分数 >= 最优页一半的页，且必须是连续的（目录不会隔页）
    keep = [p for p, h in scored if h >= best * 0.4]
    keep.sort()
    start = keep[0]
    out = [start]
    for p in keep[1:]:
        if p == out[-1] + 1:
            out.append(p)
    return out


def _count_toc_items(page) -> int:
    """一页里有几个像目录项的东西（页码词 + 章节名词）。"""
    words = _page_words(page)
    if not words:
        return 0
    n = 0
    for _x, _y, w, h in words:
        if _is_page_token(w, h) or _looks_like_section_word(w):
            n += 1
    return n


def _page_words(page) -> list[tuple[float, float, str, float]]:
    """页面的词 → [(x中心, y中心, 文本, 高度)]。

    带出「词高」是因为中国太保的目录页上有几个超大号的装饰性数字
    （'127'/'135'/'57' 高度横跨 160+pt，是版式设计的背景数字），
    它们会被 _is_page_token 误判成页码、污染栏聚类，必须靠高度挡掉。
    """
    try:
        raw = page.get_text("words")
    except Exception:  # noqa: BLE001
        return []
    return [
        ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, w[4], abs(w[3] - w[1]))
        for w in raw
        if w[4].strip()
    ]


def _is_page_token(w: str, height: float = 0.0) -> bool:
    """页码词：1~3 位纯数字（允许 '002' 这种补零）。

    height 超过 20pt 的一律不算——那是版式装饰的大字，不是页码。
    """
    s = w.strip()
    if height > 20:
        return False
    return bool(re.fullmatch(r"\d{1,3}", s)) and 0 < int(s) < 1000


def _looks_like_section_word(w: str) -> bool:
    """章节名词：短、无标点数字，或标准节名（含「第X节」前缀）。

    注意排除「第一节」这种**光秃秃的节号**——浙商/太保的目录里
    节号单独一行、节名在下一行，节号不是章节名的一部分，
    让它进入名称块会把配对搅乱。
    """
    s = w.strip()
    if not (2 <= len(s) <= 24):
        return False
    if STD_SEC_ALONE_RE.match(s):
        return False  # 「第一节」只是个序号
    if STD_SEC_RE.match(s):
        return True
    if re.search(r"[\d，,。、；;：:（）()【】\[\]｜|/\\%#@]", s):
        return False
    return re.search(r"[一-鿿]", s) is not None


def parse_toc_by_coords(doc, pno: int) -> list[tuple[str, int]]:
    """坐标法：把「页码词」与「章节名词」按 y 邻近配对。

    关键点（实测踩过的坑）：
      * 页码块和名称块的输出顺序不可靠，必须靠 y 坐标找「同一行/紧邻行」。
      * 章节名常被拆成多行（浙商「第二节 / 公司简介和 / 主要财务指标」），
        所以要从页码往上回溯，把连续的名称行拼成一个完整章节名。
      * 目录页上还有「经营业绩」「财务报告」这类分组标题（不带页码），
        会被误当成章节名。用「已知标准章节词」二次过滤。
    """
    words = _page_words(doc[pno])
    if not words:
        return []

    # 按 y 聚成行（行内保留 (x, 文本, 词高)）
    rows: list[tuple[float, list[tuple[float, str, float]]]] = []
    for x, y, w, h in sorted(words, key=lambda t: (round(t[1], 1), t[0])):
        placed = False
        for r in rows:
            if abs(r[0] - y) <= 4.0:
                r[1].append((x, w, h))
                placed = True
                break
        if not placed:
            rows.append((y, [(x, w, h)]))
    rows.sort(key=lambda r: r[0])

    # 目录常常是多栏排版（实测中金 2 栏 x≈81/315、浙商 3 栏 x≈102/256/426、
    # 国信/华泰 页码列与名称列分开）。必须先按 x 分栏，再在栏内按 y 配对——
    # 直接在全页范围内按 y 配对会把相邻栏的章节名粘在一起
    # （中金曾出现「释义经营概览」这种跨栏结果）。
    entries = _toc_entries_multicol(rows)
    toc: list[tuple[str, int]] = []
    for page_no, parts in entries:
        if not (0 < page_no < doc.page_count):
            continue
        name = _join_section_parts(parts)
        if name:
            toc.append((name, page_no))
    toc.sort(key=lambda t: t[1])
    return toc


def _toc_entries_multicol(
    rows: list[tuple[float, list[tuple[float, str, float]]]],
) -> list[tuple[int, list[str]]]:
    """把目录页的多栏结构拆成 [(页码, 名称碎片)]。

    做法：
      1. 用「页码词的 x」聚类出栏（实测页码 x 极干净：中金 81/315、
         浙商 102/256/426、太保 206/370，同栏内差 <5pt）
      2. 每个文本块归到 x 最近的栏 —— 名称块离本页码可以差 40pt+
         （浙商页码 x=102、名称 x=143），所以归栏必须按「最近栏」而不是固定容差
      3. 栏内按 y 排序，页码与最近的名称块配对
    """
    # 1) 页码 x 聚类成栏（只看正常字号的数字，装饰大字已被 _is_page_token 挡掉）
    page_xs = sorted(
        x
        for _y, items in rows
        for x, w, h in items
        if _is_page_token(w, h)
    )
    if not page_xs:
        return []
    col_xs: list[float] = [page_xs[0]]
    for x in page_xs[1:]:
        if x - col_xs[-1] > 20:
            col_xs.append(x)
        else:
            col_xs[-1] = (col_xs[-1] + x) / 2

    def col_of(x: float) -> int:
        return min(range(len(col_xs)), key=lambda k: abs(x - col_xs[k]))

    # 2) 每一行按 x 切成「块」，每块归到最近的栏
    cols: list[list[tuple[float, int | None, list[str]]]] = [[] for _ in col_xs]
    for y, items in rows:
        items.sort(key=lambda t: t[0])
        blocks: list[list] = []  # [x, page_no, names]
        for x, w, h in items:
            s = w.strip()
            is_pg = _is_page_token(s, h)
            is_nm = _looks_like_section_word(s)
            if not is_pg and not is_nm:
                continue
            # 与上一个块间距 <25pt 视为同一块
            if blocks and 0 <= x - blocks[-1][0] <= 25:
                if is_pg and blocks[-1][1] is None:
                    blocks[-1][1] = int(s)
                elif not is_pg:
                    blocks[-1][2].append(s)
            else:
                blocks.append([x, int(s) if is_pg else None, [] if is_pg else [s]])
        for bx, bp, bn in blocks:
            if bp is None and not bn:
                continue
            cols[col_of(bx)].append((y, bp, bn))

    # 3) 栏内按 y 配对
    out: list[tuple[int, list[str]]] = []
    for col in cols:
        col.sort(key=lambda r: r[0])
        for i, (y, page_no, names) in enumerate(col):
            if page_no is None:
                continue
            if names:
                out.append((page_no, list(names)))
                continue
            best_j, best_d = None, 1e9
            for j in range(len(col)):
                if j == i:
                    continue
                ny, np_, nn = col[j]
                if np_ is not None or not nn:
                    continue
                dy = abs(ny - y)
                if dy < best_d:
                    best_j, best_d = j, dy
            if best_j is None or best_d > 40:
                continue
            parts = list(col[best_j][2])
            for j in range(best_j + 1, min(best_j + 3, len(col))):
                if col[j][1] is not None or not col[j][2]:
                    break
                parts.extend(col[j][2])
                if len("".join(parts)) >= 10:
                    break
            out.append((page_no, parts))
    return out


# 组标题不该当章节名（它们是目录的分组标签或首页速览，不对应正文独立章节）
# 注意：不要与 KNOWN_SECTIONS 重复，否则 _split_glued 会把
# 「公司治理、环境和社会」这种合法章节名从「公司治理」处截断。
GROUP_TITLES = (
    "公司治理与债券相关情况", "财务报告及备查文件", "经营业绩",
    "其他信息", "关于公司", "经营情况", "释义项", "释义内容",
    "关于我们", "经营分析", "财务报告及其他", "战略与经营分析",
)

# 机构简称/监管机构名：它们常出现在目录页或页眉，但绝不是章节名
# （实测国信证券曾解析出「中国证监会@32」这种）
ORG_NAMES = (
    "中国证监会", "上交所", "深交所", "中证协", "证券时报", "上海证券报",
    "公司登记机关", "会计师事务所", "律师事务所", "保荐机构",
    "国家外汇管理局", "深圳证监局", "海南证监局", "北京证监局",
)


def _join_section_parts(parts: list[str]) -> str:
    """把拆散的章节名碎片拼回去，并做基本清洗。

    还要处理「粘连」：多栏目录里相邻两栏的块可能被并进同一个名字，
    实测出现过「总裁致辞公司治理与债券相关情况」——两个独立章节粘成一句。
    判据是名字里出现了 ≥2 个互不重叠的已知标准章节词，此时只取第一个。
    """
    # 「第X节」和它后面的名字要粘在一起，其余直接拼
    name = ""
    for p in parts:
        if STD_SEC_RE.match(p):
            name = p
        elif STD_SEC_ALONE_RE.match(p):
            name = p
        else:
            name = (name + p) if name else p
    name = re.sub(r"\s+", "", name)
    name = re.sub(r"^第[一二三四五六七八九十]+\s*节\s*", "", name)
    name = re.sub(r"^\d{1,2}\s*", "", name)
    name = _split_glued(name)
    if not (2 <= len(name) <= 24):
        return ""
    if name in GROUP_TITLES:
        return ""
    return name


def _split_glued(name: str) -> str:
    """名字里粘了两个章节时，截断到第一个章节结束处。

    难点：「公司简介和主要财务指标」本身含「公司简介」「主要财务指标」两个词，
    但它是一个合法章节名，不能截。而「总裁致辞公司治理与债券相关情况」
    是「总裁致辞」+ 组标题「公司治理与债券相关情况」粘的，必须截。
    判据：只有在**前缀刚好等于一个已知词**、且剩余部分也是一个已知词/组标题
    （也就是两段都是完整的章节名）时才截断——「公司简介」+「和主要财务指标」
    的后半段不是完整章节名，所以不切。
    """
    # 所有已知词的出现（取最长的优先，避免「公司信息」抢占「公司简介和…」）
    hits: list[tuple[int, str]] = []
    for k in KNOWN_SECTIONS + GROUP_TITLES:
        start = 0
        while True:
            i = name.find(k, start)
            if i < 0:
                break
            hits.append((i, k))
            start = i + 1
    if len(hits) < 2:
        return name
    # 同一位置只保留最长的词
    best_at: dict[int, str] = {}
    for i, k in sorted(hits, key=lambda t: (t[0], -len(t[1]))):
        if i not in best_at or len(k) > len(best_at[i]):
            best_at[i] = k
    items = sorted((i, k) for i, k in best_at.items())
    if len(items) < 2:
        return name
    first_end = items[0][0] + len(items[0][1])
    rest = name[first_end:]
    # 前缀本身就是一个完整章节名，且剩下的开头也是一个完整章节名/组标题
    for k in KNOWN_SECTIONS + GROUP_TITLES:
        if rest.startswith(k) and len(k) >= 3:
            return name[:first_end]
    return name


def parse_toc_by_lines(doc, start: int) -> list[tuple[str, int]]:
    """按行序解析目录（坐标法的兜底，覆盖正文式目录）。"""
    toc: list[tuple[str, int]] = []
    pending: str | None = None
    for pno in range(start, min(start + 6, doc.page_count)):
        lines = [x.strip() for x in (doc[pno].get_text("text") or "").splitlines()]
        lines = [x for x in lines if x]
        for ln in lines:
            # 形式 A：「xxx …… 23」或「xxx 23」；形式 F/I：页码在前的「002 释义」
            m = re.match(r"^(.*?)[\s.·…]{2,}(\d{1,3})$", ln)
            if m:
                name, num = m.group(1).strip(), int(m.group(2))
                if 0 < num < doc.page_count and _valid_toc_name(name):
                    toc.append((_strip_seq(name), num))
                continue
            # 形式 G：「第一节  释义/12」页码用斜杠紧贴
            m = re.match(r"^(.*?)[/／]\s*(\d{1,3})$", ln)
            if m:
                name, num = m.group(1).strip(), int(m.group(2))
                if 0 < num < doc.page_count and _valid_toc_name(name):
                    toc.append((_strip_seq(name), num))
                continue
            parts = ln.split()
            if len(parts) >= 2 and PAGE_NUM_RE.match(parts[-1]):
                name = "".join(parts[:-1]).strip()
                num = int(parts[-1])
                if 0 < num < doc.page_count and _valid_toc_name(name):
                    toc.append((_strip_seq(name), num))
                continue
            # 形式 B：纯页码行，配上行 pending
            if PAGE_NUM_RE.match(ln) and pending:
                toc.append((pending, int(ln)))
                pending = None
                continue
            # 形式 C/E：章节名独立一行
            if _valid_toc_name(ln, loose=True):
                m2 = re.match(r"^(\d{1,2})\s+(\S.*)$", ln)
                pending = m2.group(2).strip() if m2 else _strip_seq(ln)
            else:
                if len(ln) > 30:
                    pending = None
    return toc


def _valid_toc_name(name: str, loose: bool = False) -> bool:
    """判断一行文字像不像目录里的章节名。"""
    if not name or TOC_NOISE.match(name) or NOISE_RE.match(name):
        return False
    if not loose and len(name) > 30:
        return False
    # 目录项里不该有长句
    if len(name) > 40 or name.endswith("。") or name.endswith("；"):
        return False
    # 标准节名带顿号是常态：「第一节 重要提示、目录和释义」
    # 「第五节 公司治理、环境和社会」，不能一刀切按标点判死。
    if STD_SEC_RE.match(name):
        return True
    # 其余目录项通常不含标点符号（顿号/逗号/句号混在一起的多半是表格行被误读）
    if re.search(r"[，,、；;：:（）()【】]", name):
        return False
    return True


def _strip_seq(name: str) -> str:
    """去掉章节名里的序号前缀与残留页码：01 / 第X节 / 释义/12。"""
    name = re.sub(r"^\d{1,2}\s+", "", name).strip()
    name = re.sub(r"^第[一二三四五六七八九十]+\s*节\s*", "", name).strip()
    # 有些目录把页码用斜杠粘在名字尾巴上，行序解析时没剥干净
    name = re.sub(r"[/／]\s*\d{1,4}\s*$", "", name).strip()
    return name


def scan_body_sections(doc) -> dict[str, int]:
    """扫正文里的标准章节标题，返回 {章节名: 首次出现的页码}。

    这是最可靠的一路：页码是 PyMuPDF 的物理页号，绝无偏移。
    只认两种版式（实测 A 股年报就这两种）：
      「第一节  管理层讨论与分析」   （节名同行）
      「第一节」 / 下一行「管理层讨论与分析」（节名单独一行）

    「（续）」页眉不算新章节——申万宏源有 110 页都打着章节页眉，
    不去重会得到 100 多个「XX（续）」。
    """
    found: dict[str, int] = {}
    for pno in range(doc.page_count):
        lines = [x.strip() for x in (doc[pno].get_text("text") or "").splitlines()]
        lines = [x for x in lines if x]
        for idx, ln in enumerate(lines[:8]):
            s = ln.strip()
            name = None
            m = STD_SEC_RE.match(s)
            if m:
                cand = m.group(1).strip()
                if 2 <= len(cand) <= 24:
                    name = cand
            elif STD_SEC_ALONE_RE.match(s) and idx + 1 < len(lines):
                cand = lines[idx + 1].strip()
                # 节名单独一行时，下一行常是页眉/日期，要挡掉
                if 2 <= len(cand) <= 24 and not re.search(r"[\d，,。、；;：:]", cand):
                    name = cand
            if name:
                key = re.sub(r"[（(]续[）)]\s*$", "", name).strip()
                if key and key not in found:
                    found[key] = pno + 1
                break
    return found


def build_section_map(doc) -> list[tuple[int, str]]:
    """建立 [(起始页, 章节名)] 映射。

    三路合并（每一路单独用都会在某些公司上翻车，必须合起来）：
      1. 正文扫描：页码绝对可靠，但只覆盖用「第X节」标题的公司（约一半）
      2. 目录解析：章节名齐全，但页码是「印刷页码」，与物理页有偏移
                   （实测国信偏 5 页、银河偏 3 页）
      3. 偏移校准：用两路共有的章节算出偏移众数，再把目录里独有的章节
         按校正后的页码并入

    实测踩过的坑：只看目录会整体错位；只看正文会漏掉一半公司的章节。
    """
    body = scan_body_sections(doc)
    toc = parse_toc(doc)

    # --- 目录先过清洗，剔除「释义/12」「深交所」这类垃圾项 ---
    toc_clean = clean_sections(
        [(p, n) for n, p in toc if n], doc.page_count, quiet=True
    ) if toc else []
    toc_map: dict[str, int] = {}
    for p, n in toc_clean:
        toc_map.setdefault(n, p)

    # --- 算偏移：两路共有的章节，正文页 - 目录页 ---
    diffs: list[int] = []
    for bn, bp in body.items():
        for tn, tp in toc_map.items():
            if bn == tn or bn in tn or tn in bn:
                diffs.append(bp - tp)
                break
    offset = 0
    if len(diffs) >= 2:
        offset = max(set(diffs), key=diffs.count)  # 众数

    merged: dict[str, int] = {}
    # 目录独有 → 用校准后的页码
    for tn, tp in toc_map.items():
        merged[tn] = min(doc.page_count, max(1, tp + offset))
    # 正文扫描覆盖它（页码更准，直接覆盖）
    for bn, bp in body.items():
        merged[bn] = bp

    items = sorted(((p, n) for n, p in merged.items()), key=lambda kv: kv[0])
    log(
        f"    正文扫描 {len(body)} 章 / 目录解析 {len(toc_map)} 章 / "
        f"页码偏移 {offset}，合并后 {len(items)} 章"
    )
    cleaned = clean_sections(items, doc.page_count)
    if len(cleaned) >= 2:
        return cleaned

    # --- 最后的兜底：按标准章节词在正文里定位（中国人保这类无标题的公司）---
    log("    合并结果仍不足，按标准章节词在正文中定位 ...")
    located: list[tuple[int, str]] = []
    for pno in range(doc.page_count):
        text = doc[pno].get_text("text") or ""
        head = "\n".join(text.splitlines()[:6])
        for nm in list(body.keys()) + list(toc_map.keys()) + list(KNOWN_SECTIONS):
            if not nm or len(nm) < 2:
                continue
            if nm in head and not any(x[1] == nm for x in located):
                located.append((pno + 1, nm))
    if len(located) >= 2:
        return clean_sections(sorted(located), doc.page_count)
    return cleaned


def clean_sections(
    items: list[tuple[int, str]], total_pages: int, quiet: bool = False
) -> list[tuple[int, str]]:
    """清洗章节列表：只留下可信的章节名。

    A 股年报的章节名高度标准化（第一节 释义 / 第三节 管理层讨论与分析 /
    财务报告 …），所以采用「白名单优先」策略：
      1. 名字命中标准章节词 → 保留（这才是有用的元数据）
      2. 名字干净（无标点数字、长度 3-20、无日期样式）且覆盖 >=5 页 → 保留
      3. 其余一律丢弃，宁可让 section 为空
    实测不这样严格就会混进「–」「们」「指」「Page/」「2007 年4 月」
    这类噪声（它们会被当成章节名写进检索元数据，比空值更糟）。
    """
    if not items:
        return []
    items = sorted(items, key=lambda x: x[0])
    end = total_pages + 1
    spans: list[tuple[int, str, int]] = []
    for i, (num, name) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else end
        spans.append((num, name, nxt - num))

    def is_known(n: str) -> bool:
        # 先剥掉「第七节 」「第X节 」前缀再匹配标准章节词
        bare = re.sub(r"^第[一二三四五六七八九十]+\s*节\s*", "", n).strip()
        bare = re.sub(r"^\d{1,2}\s+", "", bare)
        return any(k in n or k in bare for k in KNOWN_SECTIONS)

    def normalize(n: str) -> str:
        """章节名统一去掉「第X节」「01」前缀和残留页码，并切掉粘连的多余章节。"""
        return _split_glued(_strip_seq(n))

    def is_clean(n: str) -> bool:
        # 无标点、无数字日期、无英文乱码、长度适中
        if not (3 <= len(n) <= 20):
            return False
        if re.search(r"[，,。、；;：:（）()【】\[\]｜|/\\%#@]", n):
            return False
        if re.search(r"\d", n):  # 含数字的多半是日期或编号
            return False
        if re.search(r"[A-Za-z]{2,}", n):  # 连续英文字母
            return False
        if name_is_single_char(n):
            return False
        return True

    def is_trustworthy(n: str) -> bool:
        """最后的质量闸：章节名至少要有 2 个汉字，且不含控制字符/制表符。

        实测漏进来的脏名：「中国证监会」（机构简称）、「035\\t 管理层讨论与分析」
        （带制表符的残留）、「监事会报告」这种还行但要靠白名单。
        判据：汉字数 >= 2，且不含\\t \\n 等控制符。
        """
        if re.search(r"[\x00-\x1f\x7f]", n):
            return False
        if any(o in n for o in ORG_NAMES):
            return False
        return len(re.findall(r"[一-鿿]", n)) >= 2

    keep = [
        (num, normalize(name), ln)
        for num, name, ln in spans
        if name not in GROUP_TITLES
        and is_trustworthy(name)
        and (is_known(name) or (is_clean(name) and ln >= 5))
    ]
    if len(keep) > 30:
        keep.sort(key=lambda x: -x[2])
        keep = sorted(keep[:15])
        log("    章节数过多，只保留覆盖最广的 15 个")
    # 去掉 normalize 后可能出现的重名（同一章节被目录和正文各记一次）
    dedup: list[tuple[int, str]] = []
    seen_names: set[str] = set()
    for num, name, _ in sorted(keep, key=lambda x: x[0]):
        if name in seen_names:
            continue
        seen_names.add(name)
        dedup.append((num, name))
    if not quiet:
        log(f"    章节过滤：{len(items)} -> {len(dedup)}")
    return dedup


def name_is_single_char(n: str) -> bool:
    """「们」「指」「–」这种：去掉标点后只剩 1 个字。"""
    stripped = re.sub(r"[^\u4e00-\u9fff]", "", n)
    return len(stripped) <= 1


def section_for_page(sec_map: list[tuple[int, str]], page: int) -> str:
    """页码 → 所属章节。"""
    if not sec_map:
        return ""
    cur = ""
    for start, name in sec_map:
        if page >= start:
            cur = name
        else:
            break
    return cur


# ---------------------------------------------------------------- 正文


def extract_pages(pdf_path: Path) -> list[dict]:
    pages: list[dict] = []
    with pymupdf.open(pdf_path) as doc:
        sec_map = build_section_map(doc)
        log(f"    目录解析出 {len(sec_map)} 个章节")
        for pno in range(doc.page_count):
            raw = doc[pno].get_text("text") or ""
            lines = [clean_line(x) for x in raw.splitlines()]
            lines = [x for x in lines if x]
            pages.append(
                {
                    "page": pno + 1,
                    "section": section_for_page(sec_map, pno + 1),
                    "text": "\n".join(lines),
                }
            )
    return pages


# ---------------------------------------------------------------- 表格
#
# 实测教训：这些年报的表格大多是「无框线 + 艺术排版」，
# pdfplumber.extract_tables() 基本抓不到（实测中信证券 389 页只出 16 张表，
# 而合并资产负债表这种核心表完全没抽到）。
# 但 PyMuPDF 能拿到每个词的坐标，用 x 坐标分列就能把行列还原出来。
# 所以主路径改成「按 x 聚类分列」，pdfplumber 只作为补充。

# 常见的财务报表科目关键词，用于识别表格页
FIN_TABLE_HINTS = (
    "合并资产负债表", "合并利润表", "合并现金流量表", "母公司资产负债表",
    "母公司利润表", "母公司现金流量表", "合并所有者权益变动表",
    "利润表", "资产负债表", "现金流量表", "财务报表", "主要会计数据",
)
# 单元格值的样子：数字、百分比、破折号
CELL_RE = re.compile(r"^[\d,.()\-—/%．\s]+$")


def _cluster_columns(items: list[tuple[float, str]], gap: float) -> list[list[str]]:
    """按 x 坐标把词聚成列。gap 为列间距阈值（单位：pt）。"""
    if not items:
        return []
    items = sorted(items, key=lambda t: t[0])
    cols: list[list[tuple[float, str]]] = [[items[0]]]
    for x, w in items[1:]:
        if x - cols[-1][-1][0] > gap:
            cols.append([(x, w)])
        else:
            cols[-1].append((x, w))
    return [[t for _, t in col] for col in cols]


def extract_tables_by_position(doc, page) -> list[dict]:
    """用词坐标把一页还原成若干张表。

    做法：
      1. 取页面所有词，按 y 坐标聚成「行」
      2. 每行内按 x 聚成「列」
      3. 连续若干行都有 >=3 列时认定为表格
    """
    try:
        words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wno
    except Exception:  # noqa: BLE001
        return []

    if not words:
        return []

    # 1) 按 y 聚行（同一行内 y 差小于 3pt 视为同一行）
    rows_raw: list[list[tuple[float, float, str]]] = []
    for w in sorted(words, key=lambda w: (round(w[1], 1), w[0])):
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if not txt.strip():
            continue
        placed = False
        for row in rows_raw:
            if abs(row[0][1] - y0) <= 3.0:
                row.append((x0, y0, txt))
                placed = True
                break
        if not placed:
            rows_raw.append([(x0, y0, txt)])
    rows_raw.sort(key=lambda r: r[0][1])

    # 2) 每行分列
    table_rows: list[list[str]] = []
    for row in rows_raw:
        # 估算本页的列间距：取本页较宽行的平均列数
        xs = sorted(x for x, _, _ in row)
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        big = [g for g in gaps if g > 20]
        gap = (sum(big) / len(big)) if big else 0
        if gap <= 0:
            cells = [t for _, _, t in row]
            table_rows.append(cells)
            continue
        cells = []
        cur = [row[0]]
        for item in row[1:]:
            if item[0] - cur[-1][0] > max(18, gap * 0.6):
                cells.append(cur)
                cur = [item]
            else:
                cur.append(item)
        cells.append(cur)
        table_rows.append([" ".join(t for _, _, t in c) for c in cells])

    # 3) 切成连续的表格段：连续 3 行以上且至少 2 列，且多数行是「值型」
    tables: list[dict] = []
    cur: list[list[str]] = []
    for cells in table_rows:
        nonempty = [c for c in cells if c.strip()]
        numeric = sum(1 for c in nonempty if CELL_RE.match(c))
        is_tabular = len(nonempty) >= 2 and numeric >= max(1, len(nonempty) // 2)
        if is_tabular:
            cur.append(cells)
        else:
            if len(cur) >= 3:
                tables.append({"title": "", "rows": cur})
            cur = []
    if len(cur) >= 3:
        tables.append({"title": "", "rows": cur})

    # 4) 过滤太宽（整页正文被误判）与太窄
    out = []
    for t in tables:
        rows = [[c for c in r if c.strip()] for r in t["rows"]]
        rows = [r for r in rows if r]
        if len(rows) < 3:
            continue
        maxcol = max(len(r) for r in rows)
        if maxcol < 2 or maxcol > 12:
            continue
        # 单元格平均长度：太长说明混进正文
        flat = [c for r in rows for c in r]
        if sum(len(c) for c in flat) / len(flat) > 24:
            continue
        out.append({"title": "", "rows": rows})
    return out


def detect_table_title(text: str) -> str:
    """从页首几行里找财务报表名，作为表名。"""
    for ln in text.splitlines()[:8]:
        s = ln.strip()
        for h in FIN_TABLE_HINTS:
            if h in s and len(s) <= 30:
                return s
    return ""


def extract_tables(pdf_path: Path) -> dict[int, list[dict]]:
    """逐页用坐标法抽表。返回 {页码: [{"title","rows"}]}。"""
    out: dict[int, list[dict]] = {}
    with pymupdf.open(pdf_path) as doc:
        for pno in range(doc.page_count):
            page = doc[pno]
            try:
                tables = extract_tables_by_position(doc, page)
            except Exception:  # noqa: BLE001
                continue
            if not tables:
                continue
            # 给表配名字：优先本页第一个表用页首的报表名
            page_text = page.get_text("text") or ""
            title = detect_table_title(page_text)
            for i, t in enumerate(tables):
                if i == 0:
                    t["title"] = title
                out.setdefault(pno + 1, []).append(t)
    return out


# 财务报表里常见的科目行名（用于无数字时的兜底识别）
ITEM_RE = re.compile(
    r"^(货币资金|结算备付金|拆出资金|交易性金融资产|衍生金融资产|"
    r"买入返售金融资产|应收账款|应收款项|预付款项|其他应收款|存货|"
    r"发放贷款和垫款|债权投资|其他债权投资|长期股权投资|投资性房地产|"
    r"固定资产|在建工程|无形资产|商誉|递延所得税资产|其他资产|"
    r"资产总计|负债合计|所有者权益|股本|资本公积|未分配利润|"
    r"营业收入|营业成本|营业支出|营业总成本|利息收入|手续费及佣金收入|"
    r"投资收益|公允价值变动收益|业务及管理费|营业利润|利润总额|"
    r"所得税费用|净利润|归属于母公司|基本每股收益|稀释每股收益|"
    r"经营活动产生的现金流量净额|投资活动产生的现金流量净额|"
    r"筹资活动产生的现金流量净额|现金及现金等价物净增加额|期末现金余额)"
)


def extract_tables_by_items(doc) -> dict[int, list[dict]]:
    """兜底：坐标法失效时（自定义字体导致数字提取不到），改用科目名识别。

    实测中国人保的合并资产负债表整页只有 34 个词——年份和金额用的是
    嵌入字体，PyMuPDF 取不到，数字全丢，坐标法判据必然失效。
    这时页面仍然保留了完整的科目名列表，可以还原成「单列科目表」，
    至少保证科目可被检索到（数值缺失会在评测里如实记录）。
    """
    out: dict[int, list[dict]] = {}
    for pno in range(doc.page_count):
        try:
            text = doc[pno].get_text("text") or ""
        except Exception:  # noqa: BLE001
            continue
        if not detect_table_title(text):
            continue
        items: list[str] = []
        for ln in text.splitlines():
            s = ln.strip().rstrip("：:").strip()
            if ITEM_RE.match(s) and 2 <= len(s) <= 20:
                items.append(s)
        # 去重保序
        seen: set[str] = set()
        uniq = [x for x in items if not (x in seen or seen.add(x))]
        if len(uniq) >= 5:
            out[pno + 1] = [
                {
                    "title": detect_table_title(text),
                    "rows": [[x] for x in uniq],
                    "degraded": True,  # 标记：数值因字体问题未能提取
                }
            ]
    return out


def extract_tables_full(pdf_path: Path) -> dict[int, list[dict]]:
    """坐标法为主，科目名法兜底。"""
    primary = extract_tables(pdf_path)
    n_primary = sum(len(v) for v in primary.values())
    log(f"    坐标法抽出 {n_primary} 张表")
    if n_primary > 20:
        return primary
    log("    坐标法效果不佳，启用科目名兜底 ...")
    fallback = extract_tables_by_items(pymupdf.open(pdf_path))
    merged = dict(primary)
    for pno, tbs in fallback.items():
        if pno not in merged:
            merged[pno] = tbs
    n_fb = sum(len(v) for v in fallback.values() if v and v[0].get("degraded"))
    log(f"    科目名兜底补上 {n_fb} 张（这些表的数值因 PDF 字体问题未能提取）")
    return merged


# ---------------------------------------------------------------- 主流程


def process(pdf_path: Path, code: str, name: str) -> dict:
    pages = extract_pages(pdf_path)
    n_text = sum(len(p["text"]) for p in pages)
    secs = {p["section"] for p in pages if p["section"]}
    log(f"  {code} {name}: {len(pages)} 页 / {n_text} 字 / 章节 {len(secs)} 个，抽表格...")

    try:
        tables_by_page = extract_tables_full(pdf_path)
    except Exception as e:  # noqa: BLE001
        log(f"  {code} {name}: 表格抽取异常，跳过：{e}")
        tables_by_page = {}

    n_tables = sum(len(v) for v in tables_by_page.values())
    n_rows = sum(len(t["rows"]) for v in tables_by_page.values() for t in v)
    for p in pages:
        p["tables"] = tables_by_page.get(p["page"], [])

    out_path = TEXT_DIR / f"{code}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(
                json.dumps(
                    {
                        "code": code,
                        "name": name,
                        "page": p["page"],
                        "section": p["section"],
                        "text": p["text"],
                        "tables": p["tables"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    stat = {
        "code": code,
        "name": name,
        "pages": len(pages),
        "chars": n_text,
        "sections": sorted(secs),
        "n_sections": len(secs),
        "tables": n_tables,
        "table_rows": n_rows,
    }
    log(
        f"  {code} {name}: 完成 — {stat['pages']} 页 / {stat['chars']} 字 / "
        f"{stat['n_sections']} 章 / {stat['tables']} 表 / {stat['table_rows']} 行"
    )
    return stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*")
    args = ap.parse_args()

    manifest_path = PDF_DIR / "manifest.json"
    if not manifest_path.exists():
        log("找不到 data/pdf/manifest.json，请先跑 01_download.py")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    stats = []
    for code, rec in manifest.items():
        if rec.get("status") in ("failed", "no_report"):
            continue
        if args.codes and code not in args.codes:
            continue
        pdfs = list(PDF_DIR.glob(f"{code}_*/*年度报告.pdf"))
        if not pdfs:
            log(f"  {code}: 找不到 PDF，跳过")
            continue
        try:
            stats.append(process(pdfs[0], code, rec.get("name", code)))
        except Exception as e:  # noqa: BLE001
            log(f"  {code}: 处理失败 {e}")

    if stats:
        total = {
            "companies": len(stats),
            "pages": sum(s["pages"] for s in stats),
            "chars": sum(s["chars"] for s in stats),
            "tables": sum(s["tables"] for s in stats),
            "table_rows": sum(s["table_rows"] for s in stats),
        }
        (TEXT_DIR / "stats.json").write_text(
            json.dumps({"total": total, "per_company": stats}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(
            f"===== 全库：{total['companies']} 家 / {total['pages']} 页 / "
            f"{total['chars']} 字 / {total['tables']} 张表 ====="
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
