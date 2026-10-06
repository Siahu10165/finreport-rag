"""硅基流动（SiliconFlow）在线 Embedding 客户端。

用在线 API 跑Qwen/Qwen3-Embedding-8B，替代本机跑模型：
  * 8B 模型 8 张 6GB 显卡也装不下，本机 CPU 跑要 4 小时
  * 在线按token 计费，16392 块约 ¥4，一次性开销可接受

接口规范（https://api-docs.siliconflow.cn/docs/api/embeddings-post）：
  POST https://api.siliconflow.cn/v1/embeddings
  Authorization: Bearer <key>
  {
    "model": "Qwen/Qwen3-Embedding-8B",
    "input": ["文本1", "文本2", ...],      # 支持数组批量
    "dimensions": 1024,                    # Qwen3 系列支持自定义维度
    "encoding_format": "float"
  }
  → {"object":"list","data":[{"embedding":[...],"index":0}], "usage":{...}}

要处理的三个真实坑：
  1. **TPM 限流**：返回 429 + code 20012「TPM limit reached」，
     必须指数退避重试，不能直接失败
  2. **input 数组长度上限**：一次塞太多条会 400，实测按 16~32 条一批最稳
  3. **长文本**：模型支持 32K，正常不会超；但空串会 400，
     过滤掉空文本并保持索引对齐（见 embed_texts 的返回值约定）

断点续传：向量化 16392 块要几十分钟，中间断网/限流失败必须能续，
所以每批结果立刻落盘到data/index/_parts/，重跑时自动跳过已完成的批次。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"

# 退避上限：限流时最长等 60 秒，避免无限重试卡死
MAX_BACKOFF = 60.0


class EmbeddingError(RuntimeError):
    """向量化失败（重试耗尽仍不成功）。"""


class SiliconFlowEmbedder:
    """硅基流动 Embedding 客户端。

    参数
    ----
    api_key      硅基流动 API key
    model        模型名，默认 Qwen/Qwen3-Embedding-8B
    dimensions   输出维度，Qwen3-Embedding-8B 支持 64~4096
    batch_size   单次请求的文本条数
    max_retries  单次请求的最大重试次数
    min_interval 两次请求之间的最小间隔（秒），用于自我限速
    base_url     API 根地址
    """

    def __init__(
        self,
        api_key: str,
        model: str = "Qwen/Qwen3-Embedding-8B",
        dimensions: int = 1024,
        batch_size: int = 16,
        max_retries: int = 6,
        min_interval: float = 0.15,
        base_url: str = SILICONFLOW_BASE,
        timeout: float = 60.0,
        truncate: str = "",
    ):
        if not api_key:
            raise EmbeddingError(
                "缺少 SILICONFLOW_API_KEY。请复制 .env.example 为 .env 并填入 key。"
            )
        if requests is None:
            raise EmbeddingError("缺少 requests，请先 pip install requests")
        self.api_key = api_key
        self.model = model
        self.dimensions = int(dimensions)
        self.batch_size = max(1, int(batch_size))
        self.max_retries = int(max_retries)
        self.min_interval = float(min_interval)
        self.timeout = float(timeout)
        self.base_url = base_url.rstrip("/")
        # truncate："" 表示不传（模型 32K 足够，一般用不上）。
        # 若某批文本超长报 400，设成 "right" 让服务端从右侧截断。
        self.truncate = truncate or ""

        self._last_call = 0.0
        # 统计信息，方便最后汇报真实消耗
        self.stats = {
            "requests": 0,
            "prompt_tokens": 0,
            "retries": 0,
            "rate_limited": 0,
            "elapsed": 0.0,
        }

    # ------------------------------------------------------------ 底层
    def _throttle(self) -> None:
        """自我限速：保证两次请求之间至少间隔 min_interval 秒。"""
        gap = time.time() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.time()

    def _post(self, texts: list[str]) -> list[list[float]]:
        """发一次请求并返回向量列表。内部处理重试与退避。"""
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        if self.truncate:
            payload["truncate"] = self.truncate
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_err = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            t0 = time.time()
            try:
                resp = requests.post(
                    f"{self.base_url}/embeddings",
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
                self.stats["requests"] += 1
            except requests.RequestException as e:  # noqa: PERF203
                # 网络层失败（断网/DNS/超时）也走同一套退避
                last_err = f"网络异常 {type(e).__name__}: {e}"
                self._sleep_backoff(attempt)
                self.stats["retries"] += 1
                continue

            elapsed = time.time() - t0
            self.stats["elapsed"] += elapsed

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    last_err = f"响应不是 JSON：{resp.text[:120]}"
                    self._sleep_backoff(attempt)
                    self.stats["retries"] += 1
                    continue

                items = data.get("data") or []
                if len(items) != len(texts):
                    last_err = (
                        f"返回条数不匹配：请求 {len(texts)} 条，"
                        f"返回 {len(items)} 条"
                    )
                    self._sleep_backoff(attempt)
                    self.stats["retries"] += 1
                    continue

                usage = data.get("usage") or {}
                self.stats["prompt_tokens"] += int(
                    usage.get("prompt_tokens") or 0
                )

                # API 用 index 字段标明每条向量对应哪个输入，
                # 顺序不保证，必须按 index 归位（踩过：直接 append 会串行）
                out: list[list[float] | None] = [None] * len(texts)
                for pos, item in enumerate(items):
                    idx = int(item.get("index", pos))
                    if 0 <= idx < len(texts):
                        out[idx] = item["embedding"]
                if any(v is None for v in out):
                    last_err = "部分输入没有对应向量返回"
                    self._sleep_backoff(attempt)
                    self.stats["retries"] += 1
                    continue
                return [v for v in out if v is not None]

            # ---- 错误分支 ----
            body = resp.text[:200]
            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                last_err = f"429 限流：{body}"
            elif resp.status_code in (500, 502, 503, 504):
                last_err = f"{resp.status_code} 服务端错误：{body}"
            elif resp.status_code == 400:
                # 400 多半是「这批文本太长」或「空字符串」——属于不可重试的参数错误，
                # 再重试多少次都一样，直接抛出让上层定位到具体批次
                raise EmbeddingError(
                    f"400 参数错误（第 {attempt + 1} 次）：{body}\n"
                    f"本批第 1 条长度 {len(texts[0])} 字符。"
                    f"若报 token 超限，调小 EMBED_API_BATCH 或设置 EMBED_API_TRUNCATE=right。"
                )
            elif resp.status_code == 401:
                raise EmbeddingError(f"401 鉴权失败：{body}。检查 SILICONFLOW_API_KEY。")
            elif resp.status_code == 403:
                raise EmbeddingError(f"403 无权限：{body}。检查账户是否可访问该模型。")
            else:
                last_err = f"{resp.status_code}：{body}"

            self._sleep_backoff(attempt)
            self.stats["retries"] += 1

        raise EmbeddingError(f"重试 {self.max_retries} 次仍失败：{last_err}")

    def _sleep_backoff(self, attempt: int) -> None:
        """指数退避 + 抖动。抖动是必要的：多个请求同时被限流时，
        没有抖动会形成惊群效应，永远撞在同一波限流上。"""
        import random

        wait = min(MAX_BACKOFF, (2**attempt) * 1.5)
        wait *= 0.7 + random.random() * 0.6  # ±30% 抖动
        time.sleep(wait)

    # ------------------------------------------------------------ 批量
    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """单批（<= batch_size 条）向量化，返回 (len(texts), dim) float32。"""
        vecs = self._post(list(texts))
        arr = np.asarray(vecs, dtype=np.float32)
        if arr.ndim != 2:
            raise EmbeddingError(f"向量形状异常：{arr.shape}")
        if arr.shape[1] != self.dimensions:
            raise EmbeddingError(
                f"维度不符：请求 {self.dimensions}，返回 {arr.shape[1]}。"
                f"确认模型 {self.model} 是否支持该维度。"
            )
        return arr

    def embed_query(self, query: str) -> np.ndarray:
        """编码单条查询，返回已归一化的 float32 向量。"""
        arr = self.embed_batch([query])
        return _l2_normalize(arr)[0]


def _l2_normalize(arr: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化。归一化后内积 = 余弦相似度。"""
    arr = np.asarray(arr, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    # 防止除零：全零向量原样返回
    norms[norms == 0] = 1.0
    return arr / norms


# ---------------------------------------------------------------- 断点续传


class PartWriter:
    """把大批量向量分片落盘，支持断点续传。

    向量化 16392 块在线跑要几十分钟，中途限流/断网就得能续：
    每批结果立刻存成一个 .npy 小文件，重跑时扫目录跳过已完成的批次。
    """

    def __init__(self, parts_dir: Path, dim: int):
        self.dir = Path(parts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.meta_path = self.dir / "_progress.json"
        self.meta = {"batch_size": 0, "done_batches": []}
        if self.meta_path.exists():
            try:
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def reset(self, batch_size: int) -> None:
        """清空旧分片（切块参数变了必须重来，否则维度/内容都对不上）。"""
        for p in self.dir.glob("*.npy"):
            p.unlink()
        self.meta = {"batch_size": batch_size, "done_batches": []}
        self.save()

    def done_batches(self) -> set[int]:
        """已完成的批号集合。只认「文件真实存在」的批次，
        避免元数据写了但文件被删导致错位。"""
        done = set(self.meta.get("done_batches") or [])
        valid = set()
        for b in done:
            if (self.dir / f"part_{b:05d}.npy").exists():
                valid.add(b)
        return valid

    def save(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False), encoding="utf-8"
        )

    def write(self, batch_no: int, arr: np.ndarray) -> None:
        np.save(self.dir / f"part_{batch_no:05d}.npy", arr.astype(np.float16))
        done = set(self.meta.get("done_batches") or [])
        done.add(int(batch_no))
        self.meta["done_batches"] = sorted(done)
        self.save()

    def load_all(self, expected_batches: int) -> np.ndarray | None:
        """所有批次齐了才返回 (N, dim)，否则 None（表示还没跑完）。"""
        done = self.done_batches()
        if len(done) < expected_batches:
            return None
        arrs = []
        for b in range(expected_batches):
            p = self.dir / f"part_{b:05d}.npy"
            if not p.exists():
                return None
            arrs.append(np.load(p))
        return np.concatenate(arrs, axis=0)


def build_embedder_from_env() -> SiliconFlowEmbedder:
    """从环境变量构造客户端，供 03_build_index / retriever 共用。"""
    # 延迟import 避免 common.py 在没装 requests 的环境里直接崩
    from common import (
        EMBED_API_BATCH,
        EMBED_API_INTERVAL,
        EMBED_API_MAX_RETRIES,
        EMBED_API_MODEL,
        EMBED_API_TRUNCATE,
        EMBED_BASE_URL,
        EMBED_DIM,
        SILICONFLOW_API_KEY,
    )

    return SiliconFlowEmbedder(
        api_key=SILICONFLOW_API_KEY,
        model=EMBED_API_MODEL,
        dimensions=EMBED_DIM,
        batch_size=EMBED_API_BATCH,
        max_retries=EMBED_API_MAX_RETRIES,
        min_interval=EMBED_API_INTERVAL,
        base_url=EMBED_BASE_URL,
        truncate=EMBED_API_TRUNCATE,
    )