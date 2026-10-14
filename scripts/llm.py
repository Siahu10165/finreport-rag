"""调用 DeepSeek 生成答案，并强制每句话都带出处角标。

对照课件「带引用的生成」那一页的做法：
  * 把召回的块编号成 [1][2][3]…，正文只允许引用这些编号
  * 提示词里明确「材料里没有的信息不许编」
  * 生成后再做一次「出口校验」：答案里出现的每个 [n] 都必须在召回列表里
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL  # noqa: E402

SYSTEM_PROMPT = """你是一个严谨的财报问答助手。你只能依据下面提供的「参考资料」回答问题。

硬性要求：
1. 每一个结论后面必须标注来源编号，格式为 [1]、[2]，可多个并列写成 [1][3]。
2. 参考资料里没有的数字、日期、名称，一律不许编造。不确定就明确说「资料中未提及」。
3. 涉及多家公司或需要横向比较时，必须分别列出每家公司的数据并各自标注来源。
4. 金额单位照抄原文，不要自己换算或四舍五入。
5. 用中文回答，简洁直接，不要复述问题，不要写「根据参考资料」这类废话开场。

回答格式：
先给直接结论（1-3 句），再按需分点补充细节。"""


def build_user_prompt(question: str, chunks: list[dict]) -> str:
    parts = [f"问题：{question}", "", "参考资料："]
    for i, c in enumerate(chunks, start=1):
        head = f"[{i}] 公司：{c['company']}（{c['code']}） 章节：{c['section'] or '未标注'} 页码：第 {c['page']} 页 类型：{'表格' if c['kind'] == 'table' else '正文'}"
        body = c["text"].strip()
        parts.append(head)
        parts.append(body)
        parts.append("")
    parts.append("请依据以上资料回答问题，并在每句话后标注来源编号。")
    return "\n".join(parts)


def call_deepseek(question: str, chunks: list[dict], timeout: int = 120) -> dict:
    """返回 {"answer": str, "used": [1,2], "raw": str, "error": str|None}"""
    if not DEEPSEEK_API_KEY:
        return {
            "answer": "",
            "used": [],
            "raw": "",
            "error": "未配置 DEEPSEEK_API_KEY（请在 .env 里填写）",
        }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(question, chunks)},
            # 预填空 assistant 段：deepseek-flash 是**推理模型**，
            # 响应里 content 与 reasoning_content 共享 max_tokens 预算。
            # 不预填时长题目会思考很久把预算吃光 → finish_reason='length'
            # 且 content 返回空字符串——**HTTP 200、不报错、答案却是空的**
            # （实测第 8 题跨公司对比题就栽在这里，评测一度把「调用失败」
            # 误判成「模型答错」）。预填后它直接续写答案，稳且快。
            {"role": "assistant", "content": ""},
        ],
        "temperature": 0.1,  # 财报问答要稳，别发挥
        # 给足预算：推理内容与正文共享这个数。实测 1500 会不够。
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {"answer": "", "used": [], "raw": "", "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "used": [], "raw": "", "error": f"请求失败：{e}"}

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    answer = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    if not answer:
        # 静默返回空是最难查的失败模式，必须显式报出来，
        # 否则评测会把「调用失败」误判成「模型答错」。
        return {
            "answer": "",
            "used": [],
            "raw": "",
            "error": (
                f"模型返回空内容（finish_reason={choice.get('finish_reason')}，"
                f"reasoning {len(reasoning)} 字）——推理模型把 max_tokens 吃光了"
            ),
        }
    # 出口校验：只保留真实存在的引用编号
    cited = {int(x) for x in re.findall(r"\[(\d{1,2})\]", answer)}
    valid = {c for c in cited if 1 <= c <= len(chunks)}
    return {
        "answer": answer,
        "used": sorted(valid),
        "hallucinated_cites": sorted(cited - valid),
        "raw": answer,
        "error": None,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    from retriever import HybridRetriever

    r = HybridRetriever()
    res = r.search(args.question, final_topk=args.k)
    print(f"召回 {len(res['chunks'])} 块：", flush=True)
    out = call_deepseek(args.question, res["chunks"])
    if out["error"]:
        print("错误：", out["error"])
    else:
        print(f"引用编号：{out['used']}")
        if out.get("hallucinated_cites"):
            print(f"！出现无效引用：{out['hallucinated_cites']}")
        print("-" * 60)
        print(out["answer"])