"""MCP 도구 정의·응답 크기 측정 (이슈 #30): 평가셋 질의로 search·get_details 응답 글자 수를 잰다.

    uv run python eval/response_size.py --out eval/runs/context/after.json
    <서버 버전의 python> eval/response_size.py --out eval/runs/context/before.json

지금 import되는 sonbae를 잰다. 다른 버전을 재려면 그 버전이 설치된 가상 환경의 python으로 실행한다
(eval/agent_call_run.py가 eval/runs/agent/<name>/servers/에 설치해 둔 것). 서버 코드만 있는
가상 환경에서도 돌도록 표준 라이브러리와 sonbae만 쓴다.

Voyage를 부르지 않도록 키를 지우고 단어 일치 검색으로 잰다. 의미 검색과 결과 건수가 다를 수 있어
버전끼리 비교하는 용도로 쓴다. search 상위 5건을 get_details로 연다.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "eval" / "search" / "queries.toml"
DETAIL_IDS = 5


def _summary(values: list[int]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median": statistics.median(values),
        "mean": round(statistics.mean(values), 1),
        "max": max(values),
        "total": sum(values),
    }


def measure(queries_path: Path = QUERIES) -> dict:
    os.environ.pop("VOYAGE_API_KEY", None)
    logging.disable(logging.CRITICAL)
    from sonbae import server  # 키를 지운 뒤 불러온다

    tools = asyncio.run(server.server.list_tools())
    definitions = {
        "instructions": len(server.INSTRUCTIONS),
        "tools": {
            t.name: {
                "description": len(t.description or ""),
                "schema": len(json.dumps(t.input_schema, ensure_ascii=False)),
            }
            for t in tools
        },
    }
    queries = tomllib.loads(queries_path.read_text(encoding="utf-8"))["queries"]
    search_chars, details_chars, results = [], [], []
    per_query = {}
    for q in queries:
        text = server.search(
            q["query"], q.get("problem_type"), q.get("domain"), q.get("technologies")
        )
        ids = list(dict.fromkeys(re.findall(r"^\[(case_\d+)\]", text, re.M)))
        search_chars.append(len(text))
        results.append(len(ids))
        entry = {"search": len(text), "results": len(ids)}
        if ids:
            detail = server.get_details(ids[:DETAIL_IDS])
            details_chars.append(len(detail))
            entry["details"] = len(detail)
        per_query[q["id"]] = entry
    return {
        "definitions": definitions,
        "search_chars": _summary(search_chars),
        "search_results": _summary(results),
        "details_chars": _summary(details_chars),
        "per_query": per_query,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    data = measure()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s, d = data["search_chars"], data["details_chars"]
    print(f"search 중앙값 {s['median']}자, get_details 중앙값 {d['median']}자 → {args.out}")


if __name__ == "__main__":
    main()
