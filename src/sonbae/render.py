"""도구 결과를 에이전트가 읽기 쉬운 텍스트로 만든다 (JSON보다 토큰이 적고 인용하기 쉽다)."""

from sonbae.search.query import (
    AggregateResult,
    Detail,
    Filters,
    GroupBy,
    SearchResult,
    TechnologyResolution,
)

# 사례는 설계 정답이 아니라 참고 자료라는 원칙 (docs/기획.md "호출 유도"의 사례 활용 원칙).
# search 설명에 둔다. 출처(회사명·원문 링크)는 search 설명의 인용 규칙에서 따로 말한다.
# 인용 규칙·이 원칙은 결과마다 붙이지 않는다. 결과에는 결과 상태에 따른 안내(없음, 적음 등)만
# 넣는다 (사용자 결정, 이슈 #30)
REFERENCE_PRINCIPLE = (
    "사례는 해당 회사의 규모·조직·기존 인프라에서 내린 선택이다. "
    "사용자에게 참고 사례로 소개하되 그대로 적용하라고 권하지 말고, "
    "사용자 상황과 다른 점을 함께 짚는다. 사례 소개와 자신의 제안은 구분해서 제시한다."
)
_MAX_COMPANY_NAMES = 5

GROUP_BY_LABELS: dict[str, str] = {
    "technology": "기술",
    "problem_type": "문제 유형",
    "domain": "도메인",
    "company": "회사",
    "rejected_alternative": "버린 대안",
}


def _header(entry: dict) -> str:
    return (
        f"[{entry['id']}] {entry['company']} · {entry['published_at']}"
        f" · {', '.join(entry['problem_types'])} / {', '.join(entry['domains'])}"
    )


def _first(points: list[dict]) -> str:
    return points[0]["text"] if points else "-"


def card(entry: dict) -> str:
    # 제목은 에이전트가 get_details로 열 글을 고르는 단서 (요약 한 줄로는 주제가 안 보일 때가 있음)
    lines = [_header(entry), f"제목: {entry['post_title']}"]
    # 문제 상황은 팁·활용 경험 글에서 비어 있을 수 있다. 성능·운영 줄은 근거의 세기를 보여 주는
    # 표시이기도 하다 (사례·인사이트 구분을 없앤 대신, docs/기획.md)
    if entry["problem_situation"]:
        lines.append(f"문제 상황: {_first(entry['problem_situation'])}")
    lines.append(f"해결 방법: {_first(entry['solution'])}")
    if entry["performance_ops"]:
        lines.append(f"성능·운영: {_first(entry['performance_ops'])}")
    if entry["technologies"]:
        lines.append(f"기술: {', '.join(entry['technologies'])}")
    rejected = list(dict.fromkeys(r["name"] for r in entry.get("rejected_alternatives", [])))
    if rejected:
        lines.append(f"버린 대안: {', '.join(rejected)}")
    lines.append(f"원문: {entry['post_url']}")
    return "\n".join(lines)


def _condition(query: str | None, filters: Filters) -> str:
    parts = [f'검색어 "{query}"'] if query else []
    parts += filters.describe()
    return ", ".join(parts) if parts else "조건 없음"


def unknown_technologies(resolution: TechnologyResolution) -> list[str]:
    lines = []
    for name, similar in resolution.unknown.items():
        hint = f" 비슷한 기술: {', '.join(similar)}" if similar else ""
        lines.append(f"※ '{name}'은(는) 기술 사전에 없어 기술 필터에서 뺐습니다.{hint}")
    return lines


# 이 건수 이하면 사례가 적다고 알린다
FEW_RESULTS = 3


def search_result(query: str, filters: Filters, result: SearchResult, notes: list[str]) -> str:
    lines = [f"조건: {_condition(query, filters)}", *notes]
    if result.fallback:
        # 의미 검색을 못 쓰면 같은 뜻의 다른 표현으로 쓴 사례가 빠질 수 있다 (M10 결정)
        lines.append(
            f"※ 의미 검색을 쓰지 못해({result.fallback}) 단어 일치 검색으로 찾았습니다."
            " 같은 뜻을 다른 표현으로 쓴 사례는 빠질 수 있으니, 결과가 없거나 적으면"
            " 다른 표현(한국어/영어 표기 등)으로 다시 검색하세요."
        )
    if not result.entries:
        lines.append(
            "\n결과 없음: 이 조건에 맞는 국내 기업 사례가 DB에 없습니다. "
            "사례를 지어내지 말고, 참고할 국내 사례를 찾지 못했다고 답하세요. "
            "검색어가 길면 하위 주제별로 나누거나, 다른 표현으로 바꾸거나, "
            "필터를 빼서 다시 검색해 볼 수 있습니다."
        )
        return "\n".join(lines)

    shown = len(result.entries)
    if result.total <= FEW_RESULTS:
        lines.append(
            f"결과: {result.total}건뿐입니다. 이 주제의 사례가 적다는 점을 답변에 밝히세요."
        )
    elif result.total > shown:
        lines.append(f"결과: 관련도 순 상위 {shown}건 (관련도 기준을 넘은 항목 {result.total}건)")
    else:
        lines.append(f"결과: 관련도 순 {shown}건")
    lines.append("")
    lines.append("\n\n".join(card(e) for e in result.entries))
    return "\n".join(lines)


def _points(title: str, points: list[dict]) -> list[str]:
    if not points:
        return []
    lines = ["", f"## {title}"]
    for p in points:
        lines.append(f"- {p['text']}")
        lines.append(f'  근거: "{p["evidence"]}"')
    return lines


def detail(d: Detail) -> str:
    e = d.entry
    lines = [
        f"[{e['id']}] {e['post_title']}",
        f"회사: {e['company']} · 발행일: {e['published_at']}",
        f"원문: {e['post_url']}",
        f"문제 유형: {', '.join(e['problem_types'])} · 도메인: {', '.join(e['domains'])}",
    ]
    if e["technologies"]:
        lines.append(f"기술: {', '.join(e['technologies'])}")
    if e["tags"]:
        lines.append(f"태그: {', '.join(e['tags'])}")

    lines += _points("문제 상황", e["problem_situation"])
    lines += _points("해결 방법", e["solution"])
    lines += _points("성능·운영 포인트", e["performance_ops"])
    if e["rejected_alternatives"]:
        lines += ["", "## 버린 대안"]
        for r in e["rejected_alternatives"]:
            kind = " (설계 방식)" if r.get("kind") == "설계 방식" else ""
            lines.append(f"- {r['name']}{kind}: {r['reason']}")
            lines.append(f'  근거: "{r["evidence"]}"')

    if d.siblings:
        lines += ["", f"같은 글의 다른 항목: {', '.join(d.siblings)}"]
    return "\n".join(lines)


def details_result(
    details: list[Detail], missing: list[str], dropped: list[str], max_ids: int
) -> str:
    parts = [detail(d) for d in details]
    notes = []
    if missing:
        notes.append(f"※ 없는 ID: {', '.join(missing)} (search 결과의 ID를 쓰세요)")
    if dropped:
        notes.append(
            f"※ 한 번에 {max_ids}건까지만 조회합니다. 제외된 ID: {', '.join(dropped)} "
            "(필요하면 나눠서 다시 호출하세요)"
        )
    return "\n\n---\n\n".join(parts + ["\n".join(notes)] if notes else parts)


_MAX_MEMBERS = 5


def _count(total: int, with_metrics: int) -> str:
    return f"{total}건 (수치 있는 성능·운영 포인트 {with_metrics})"


def aggregate_result(
    group_by: GroupBy, filters: Filters, result: AggregateResult, notes: list[str]
) -> str:
    label = GROUP_BY_LABELS[group_by]
    lines = [
        f"조건: {_condition(None, filters)} → {_count(result.total, result.with_metrics)}"
        f" / {result.companies}개사",
        f"기준: {label} (상위 {len(result.groups)}개 / 전체 {result.group_count}개)",
        *notes,
    ]
    if group_by == "rejected_alternative":
        lines.append(
            "※ 버린 대안이 글에 명시된 항목 중 기술·제품 대안만 셉니다."
            " 설계 방식 대안(패턴, 구현 방식)은 get_details에서 확인하세요."
        )
    if group_by == "technology":
        lines.append(
            "※ 같은 계열의 제품은 상위 기술로 합쳐 셉니다(예: Amazon MSK는 Kafka 계열)."
            " 한 항목이 계열 안의 여러 제품을 써도 한 번만 세고,"
            " '포함'의 건수는 제품별로 센 값입니다."
        )
    if group_by in ("technology", "problem_type", "domain"):
        lines.append(
            f"※ 한 항목이 여러 {label}에 걸리면 각각 셉니다."
            " 건수의 합은 전체 건수보다 클 수 있습니다."
        )
    if result.total == 0:
        lines.append("\n결과 없음: 이 조건에 맞는 항목이 DB에 없습니다. 수치를 지어내지 마세요.")
        return "\n".join(lines)

    lines.append("")
    for rank, g in enumerate(result.groups, start=1):
        if group_by == "company":
            # 회사로 묶었으면 회사 수·이름은 항목 이름과 같아 중복이다
            companies = ""
        else:
            names = ", ".join(g.companies[:_MAX_COMPANY_NAMES])
            if len(g.companies) > _MAX_COMPANY_NAMES:
                names += ", …"
            companies = f" · {len(g.companies)}개사 ({names})"
        members = ""
        if g.members:
            top = sorted(g.members.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_MEMBERS]
            members = f"  포함: {', '.join(f'{name} {n}' for name, n in top)}"
        lines.append(
            f"{rank}. {g.key}  {_count(g.total, g.with_metrics)}{companies}{members}"
            f"  예시: {', '.join(g.examples)}"
        )
    lines.append("")
    lines.append(
        "수치 있는 성능·운영 포인트는 성능·운영 포인트에 숫자(측정값, 규모 등)가 있는"
        " 항목 수입니다."
        " 없는 항목에도 정성적 결과나 운영 경험이 있을 수 있습니다."
        " 예시 ID는 get_details로 확인할 수 있습니다."
    )
    return "\n".join(lines)
