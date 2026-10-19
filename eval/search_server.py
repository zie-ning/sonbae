"""M10 5단계 R5: 서버 search가 4단계 실험(R4)의 판정 결과를 그대로 재현하는지 확인한다.

    uv run python eval/search_server.py          # 캐시 클라이언트 + 키 없음(대체 경로)
    uv run python eval/search_server.py --live   # 실제 Voyage API까지 (VOYAGE_API_KEY 필요)

리포트: eval/reports/m10-r5-server.md. 세 가지로 서버 query.search를 부른다.

1. 캐시 클라이언트: 실험 때 저장한 검색어 임베딩·리랭커 점수를 돌려주는 가짜 클라이언트.
   서버 로직(후보 텍스트, RRF, 정렬, 기준선)이 실험(eval/search_gate.py의 "점수 ≥ 0.50")과
   같은지 본다. 질문별 결과 목록이 실험과 다르면 리포트에 적는다
2. 실제 API: 서버가 쓰는 VoyageClient 그대로. 응답 시간도 잰다
3. 키 없음: 단어 일치 검색으로 대체하는 경로 (M5 방식과 같아야 한다)
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.search_embed import MODELS, EmbeddingIndex  # noqa: E402
from eval.search_eval import (  # noqa: E402
    MAX_RESULTS,
    REPORTS,
    EvalQuery,
    Ranked,
    System,
    load_queries,
    summarize,
)
from eval.search_experiments import Context, run_rows  # noqa: E402
from eval.search_rerank import RERANKERS, Reranker, _fmt, _load_env, score_all  # noqa: E402
from sonbae import db  # noqa: E402
from sonbae.search import query as q  # noqa: E402
from sonbae.search import semantic  # noqa: E402

EMBEDDING = "voyage-4-large"
RERANKER = "voyage-3"


class CachedClient:
    """실험 캐시에서 검색어 임베딩과 리랭커 점수를 돌려준다 (캐시에 없으면 API를 부른다)."""

    def __init__(self, ctx: Context):
        self.index = EmbeddingIndex(MODELS[EMBEDDING])
        self.index.build(ctx.conn)
        self.reranker = Reranker(next(s for s in RERANKERS if s.key == RERANKER))

    def embed_query(self, text: str, timeout: float | None = None) -> np.ndarray:
        return self.index.query_vector(text).astype(np.float32)

    def rerank(self, query: str, documents: list[str], timeout: float | None = None) -> list[float]:
        return self.reranker.scores(query, documents)


def server_system(
    ctx: Context,
    name: str,
    client,
    times: list[float] | None = None,
    fallbacks: list[str] | None = None,
) -> System:
    """fallbacks가 있으면 대체 경로를 탄 질문을 기록하고, 없으면(캐시 클라이언트) 실패로 본다."""

    def rank(x: EvalQuery) -> list[Ranked]:
        start = time.perf_counter()
        result = q.search(ctx.conn, x.query, x.filters(), client)
        if times is not None:
            times.append(time.perf_counter() - start)
        if client is not None and not result.semantic:
            if fallbacks is None:
                raise RuntimeError(f"{x.id}: 의미 검색 대신 대체 경로를 탔다 ({result.fallback})")
            fallbacks.append(f"{x.id}: {result.fallback}")
        return [Ranked(e["id"], -n) for n, e in enumerate(result.entries)]

    return System(name, rank, cutoff=True)


def experiment_system(ctx: Context) -> System:
    """R4 실험의 '점수 ≥ 0.50' 설정 (eval/search_gate.py와 같은 계산)."""
    reranker = Reranker(next(s for s in RERANKERS if s.key == RERANKER))
    scored = {s.query.id: s for s in score_all(ctx, [reranker], EMBEDDING)}

    def rank(x: EvalQuery) -> list[Ranked]:
        s = scored[x.id]
        if s.top1(RERANKER) < semantic.GATE:
            return []
        kept = [r for r in s.ranked(RERANKER) if r.score >= semantic.ITEM_MIN]
        return kept[:MAX_RESULTS]

    return System("실험 (R4 점수 ≥ 0.50)", rank, cutoff=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="M10 R5 서버 search 재현 확인")
    parser.add_argument("--live", action="store_true", help="실제 Voyage API로도 돌린다")
    args = parser.parse_args()

    _load_env()
    ctx = Context(db.connect(ROOT / "data" / "techblog.sqlite"), load_queries())
    if semantic.load_vectors(ctx.conn) is None:
        sys.exit(
            "검색 DB에 항목 임베딩이 없습니다. pipeline.embed와 pipeline.build_index를 실행하세요."
        )

    systems = [
        experiment_system(ctx),
        server_system(ctx, "서버 (캐시 클라이언트)", CachedClient(ctx)),
    ]
    live_times: list[float] = []
    live_fallbacks: list[str] = []
    if args.live:
        client = semantic.client_from_env()
        if client is None:
            sys.exit(f"{semantic.API_KEY_ENV}가 필요합니다.")
        systems.append(server_system(ctx, "서버 (실제 API)", client, live_times, live_fallbacks))
    systems.append(server_system(ctx, "서버 (키 없음 → 단어 일치 검색)", None))
    rows = run_rows(ctx, systems)

    lines = [
        "# M10 R5: 서버 search 재현 확인",
        "",
        "서버 query.search를 평가셋 62개로 돌려 4단계 실험(R4)의 판정 결과와 비교한다.",
        f"판정 기준: 1위 리랭커 점수 ≥ {semantic.GATE}, 항목 점수 ≥ {semantic.ITEM_MIN}.",
        "",
        "| 설정 | Recall@5 | MRR | Recall@10 | 없는 주제 결과 없음 | 평균 결과 수"
        " | 무관 비율 | 미판정 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        m = summarize(row.results)
        lines.append(
            f"| {row.system.name} | {_fmt(m.recall5)} | {_fmt(m.mrr)} | {_fmt(m.recall10)}"
            f" | {m.no_result_rate:.0%} | {m.avg_returned:.1f} | {_fmt(m.noise_rate, 2)}"
            f" | {m.unjudged} |"
        )

    base = rows[0]
    for row in rows[1:-1]:
        diff = [
            f"{a.query.id}: 실험 {a.returned} / 서버 {b.returned}"
            for a, b in zip(base.results, row.results, strict=True)
            if a.returned != b.returned
        ]
        lines += ["", f"## 실험과 결과 목록이 다른 질문: {row.system.name}", ""]
        lines += [f"- {d}" for d in diff] or [
            "없음 (62개 질문 모두 같은 항목을 같은 순서로 돌려줌)"
        ]

    if live_times:
        lines += [
            "",
            "## 서버 search 응답 시간 (실제 API)",
            "",
            "검색어 임베딩 + BM25 + 코사인 + 리랭커 + DB 조회를 합친 q.search 한 번의 시간(초).",
            "",
            f"- 평균 {statistics.mean(live_times):.2f}, 중앙값 {statistics.median(live_times):.2f},"
            f" 최대 {max(live_times):.2f} (질문 {len(live_times)}개)",
            f"- 검색 한 번의 API 한도 {semantic.DEADLINE:g}초. 대체 경로를 탄 질문:"
            f" {', '.join(live_fallbacks) or '없음'}",
        ]
    path = REPORTS / "m10-r5-server.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
