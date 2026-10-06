"""财报问答 Web 服务（Flask）。

页面形态：问答页 + 出处可点回原文。
  * 左边提问，右边看答案
  * 每条答案里的 [n] 角标都能点，点开显示该块的原文片段 + 公司/章节/页码
  * 「点回原文」按钮会跳到 data/text/<code>.jsonl 里那一页的原文

启动：python scripts/04_serve.py --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, TEXT_DIR  # noqa: E402

app = Flask(
    __name__,
    template_folder=str(ROOT / "web"),
    static_folder=str(ROOT / "web"),
)

_retr = None
_llm_ok = True


def get_retriever():
    global _retr
    if _retr is None:
        from retriever import HybridRetriever

        _retr = HybridRetriever()
    return _retr


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    meta_path = ROOT / "data" / "index" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    extract_stats = {}
    st = TEXT_DIR / "stats.json"
    if st.exists():
        extract_stats = json.loads(st.read_text(encoding="utf-8")).get("total", {})
    return jsonify({"index": meta, "extract": extract_stats})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"error": "问题不能为空"}), 400
    topk = int(data.get("topk") or 8)

    try:
        r = get_retriever()
        res = r.search(q, final_topk=topk)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"检索失败：{e}"}), 500

    chunks = []
    for i, c in enumerate(res["chunks"], start=1):
        chunks.append(
            {
                "idx": i,
                "chunk_id": c["chunk_id"],
                "company": c["company"],
                "code": c["code"],
                "section": c["section"],
                "page": c["page"],
                "kind": c["kind"],
                "text": c["text"],
                "rrf_score": round(c["rrf_score"], 5),
                "retrieval": {
                    "vector_rank": c["retrieval"].get("vector_rank"),
                    "bm25_rank": c["retrieval"].get("bm25_rank"),
                    "vector_score": round(c["retrieval"].get("vector_score", 0), 4),
                    "bm25_score": round(c["retrieval"].get("bm25_score", 0), 4),
                },
            }
        )

    payload = {
        "question": q,
        "n_candidate_vector": res["n_candidate_vector"],
        "n_candidate_bm25": res["n_candidate_bm25"],
        "chunks": chunks,
        "answer": None,
        "used": [],
        "error": None,
    }

    if data.get("with_llm", True):
        from llm import call_deepseek

        out = call_deepseek(q, res["chunks"])
        payload["answer"] = out["answer"]
        payload["used"] = out["used"]
        payload["error"] = out["error"]
        payload["hallucinated_cites"] = out.get("hallucinated_cites") or []

    return jsonify(payload)


@app.route("/api/page")
def api_page():
    """点回原文：返回某公司某页的原文。"""
    code = request.args.get("code", "")
    page = request.args.get("page", "0")
    fp = TEXT_DIR / f"{code}.jsonl"
    if not code or not fp.exists():
        return jsonify({"error": "找不到该公司"}), 404
    try:
        pno = int(page)
    except ValueError:
        return jsonify({"error": "页码无效"}), 400
    with fp.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("page") == pno:
                return jsonify(rec)
    return jsonify({"error": f"没有第 {pno} 页"}), 404


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    # 预热，把索引加载与首次 API 调用的等待前置到服务启动阶段
    try:
        r = get_retriever()
        print(f"索引就绪：{len(r.chunks)} 块 / {r.vectors.shape[1]} 维", flush=True)
        print(
            f"查询编码：{r.embed_model}（硅基流动在线 API，{r.embed_dim} 维）",
            flush=True,
        )
        r.search("测试", final_topk=2)
        print("向量 API 已预热", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"索引加载失败：{e}", flush=True)

    print(f"打开 http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())