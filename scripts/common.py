"""共用的路径、配置与工具函数。"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
PDF_DIR = DATA / "pdf"
TEXT_DIR = DATA / "text"
INDEX_DIR = DATA / "index"
LOG_DIR = DATA / "logs"
WEB_DIR = ROOT / "web"
DOCS_DIR = ROOT / "docs"

for _d in (PDF_DIR, TEXT_DIR, INDEX_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 配置

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))

# ---------------------------------------------------------------- 在线向量化
#
# 用硅基流动（SiliconFlow）的 Embeddings API 跑 Qwen/Qwen3-Embedding-8B。
# 为什么不跑本地：
#   * 8B 模型即使量化也要十几 GB 显存/内存，1660 Ti 6GB 与 16GB 内存都放不下
#   * 退到 0.6B 本地跑：1660 Ti 约 20-40 分钟，纯 CPU 要1.6-4 小时
#   * 在线按 token 计费，16392 块约 ¥4（8B ¥0.28/M tokens），一次性可接受
#
# 接口：POST https://api.siliconflow.cn/v1/embeddings
# 文档：https://api-docs.siliconflow.cn/docs/api/embeddings-post
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
EMBED_API_MODEL = os.getenv("EMBED_API_MODEL", "Qwen/Qwen3-Embedding-8B")
# 每批文本条数。实测 16~32 条最稳：一次塞太多容易 400 或撞 TPM 限流。
EMBED_API_BATCH = int(os.getenv("EMBED_API_BATCH", "16"))
# 两次请求最小间隔（秒），自我限速，避免无谓触发 429
EMBED_API_INTERVAL = float(os.getenv("EMBED_API_INTERVAL", "0.15"))
# 单批最大重试次数（429/5xx 走指数退避）
EMBED_API_MAX_RETRIES = int(os.getenv("EMBED_API_MAX_RETRIES", "6"))
# 超长文本截断方向：""=不截断 / "right" / "left"。
# Qwen3-Embedding 支持 32K，财报块 800 字远不到，正常留空即可。
EMBED_API_TRUNCATE = os.getenv("EMBED_API_TRUNCATE", "")


def check_api_config() -> dict:
    """校验向量化配置，返回可直接写进 meta.json 的信息。

    在真正开始16392 块的批量调用之前就把配置问题暴露出来，
    比跑到一半才失败要好。
    """
    info = {
        "embed_provider": "siliconflow",
        "embed_model": EMBED_API_MODEL,
        "embed_dim": EMBED_DIM,
        "embed_base_url": EMBED_BASE_URL,
        "embed_api_batch": EMBED_API_BATCH,
        "embed_api_truncate": EMBED_API_TRUNCATE or None,
    }
    if not SILICONFLOW_API_KEY:
        raise RuntimeError(
            "缺少 SILICONFLOW_API_KEY。请先 cp .env.example .env，"
            "然后在 .env 里填入硅基流动的 API key。"
        )
    # 硅基流动 API key 固定是 sk- 开头的 48 位十六进制；提前校验能挡住
    #「复制时带了空格」「填成了 DeepSeek 的 key」这类低级错误
    key = SILICONFLOW_API_KEY.strip()
    if not key.startswith("sk-"):
        raise RuntimeError(
            "SILICONFLOW_API_KEY 格式不对（应以 sk- 开头）。"
            "注意不要填成 DeepSeek 的 key，两者不通用。"
        )
    info["embed_api_key_tail"] = key[-4:]  # 只留末4 位便于核对，不落全key
    return info


# ---------------------------------------------------------------- 切块/检索
# 对照课件「半年报库的切块」：800 字一块、重合 120 字
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))

VECTOR_TOPK = int(os.getenv("VECTOR_TOPK", "20"))
BM25_TOPK = int(os.getenv("BM25_TOPK", "20"))
RRF_K = int(os.getenv("RRF_K", "60"))
FINAL_TOPK = int(os.getenv("FINAL_TOPK", "8"))

# 报告期：2025 年年报（2026 年 3-4 月披露）
REPORT_YEAR = 2025
SE_DATE = f"{REPORT_YEAR}-01-01~{REPORT_YEAR + 1}-06-30"

CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_TOPSEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": (
        "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
    ),
}

# ---------------------------------------------------------------- 语料清单
# 保险 5 家 + 券商 15 家 = 20 家（作业要求 10 家以上）
COMPANIES = [
    # ---- 保险 ----
    ("601318", "中国平安", "sse"),
    ("601601", "中国太保", "sse"),
    ("601628", "中国人寿", "sse"),
    ("601336", "新华保险", "sse"),
    ("601319", "中国人保", "sse"),
    # ---- 券商 ----
    ("600030", "中信证券", "sse"),
    ("601688", "华泰证券", "sse"),
    ("600999", "招商证券", "sse"),
    ("601211", "国泰君安", "sse"),
    ("601066", "中信建投", "sse"),
    ("600958", "东方证券", "sse"),
    ("601881", "中国银河", "sse"),
    ("600109", "国金证券", "sse"),
    ("601555", "东吴证券", "sse"),
    ("000776", "广发证券", "szse"),
    ("000166", "申万宏源", "szse"),
    ("000728", "国元证券", "szse"),
    ("002736", "国信证券", "szse"),
    ("601995", "中金公司", "sse"),
    # 替补：海通证券、东方证券的 2025 年报正文在巨潮查不到记录
    ("000783", "长江证券", "szse"),
    ("601878", "浙商证券", "sse"),
]


# ---------------------------------------------------------------- 工具


def clean_title(raw: str) -> str:
    """去掉 cninfo 返回标题里的 <em> 高亮标签。"""
    return re.sub(r"</?em>", "", raw or "").strip()


def is_complete_pdf(path: Path, min_bytes: int = 20_000) -> bool:
    """判断本地 PDF 是否已完整下载（断点续传的依据）。

    条件：文件够大 + 头部是 %PDF + 尾部有 %%EOF。
    """
    if not path.exists() or path.stat().st_size < min_bytes:
        return False
    try:
        with path.open("rb") as f:
            head = f.read(1024)
            if b"%PDF" not in head:
                return False
            f.seek(max(0, path.stat().st_size - 2048))
            tail = f.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def polite_sleep(seconds: float = 0.4) -> None:
    """请求间隔，避免给交易所站点造成压力。"""
    time.sleep(seconds)


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_jsonl(path: Path) -> list[dict]:
    import json

    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    import json

    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
