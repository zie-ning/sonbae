"""호출률 평가(이슈 #30)용 미끼 MCP 서버: sonbae와 겹칠 법한 다른 서버를 흉내 낸다.

    python eval/agent_decoy.py wiki     # 서버 하나를 stdio로 띄운다 (wiki / tracker / devdocs)

실제 사용 환경에서는 sonbae 말고도 여러 MCP 서버가 붙어 있어, 에이전트가 sonbae를 부를지 다른
도구와 견주어 정한다. 실제 서버(Context7 등)는 결과가 매번 달라 비교가 어려우므로, 이름과 설명만
그럴듯하고 늘 "결과 없음"을 돌려주는 서버를 쓴다. 결과가 없으면 에이전트는 다른 도구(sonbae 포함)로
넘어갈 수 있어, 미끼를 먼저 부른 뒤 sonbae를 부르는지도 기록으로 볼 수 있다.
"""

import sys
from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
EMPTY = "검색 결과가 없습니다."


@dataclass(frozen=True)
class DecoyTool:
    name: str
    description: str


@dataclass(frozen=True)
class DecoyServer:
    instructions: str
    tools: tuple[DecoyTool, ...]


SERVERS: dict[str, DecoyServer] = {
    # 사내 위키: "설계 문서", "운영 가이드"처럼 sonbae와 겹치는 말을 쓴다
    "wiki": DecoyServer(
        instructions="사내 위키의 문서를 검색·조회한다. 설계 문서, 회의록, 운영 가이드, 장애 회고, "
        "온보딩 문서가 있다. 팀의 기존 결정이나 사내 표준을 확인할 때 쓴다.",
        tools=(
            DecoyTool(
                "search_pages", "사내 위키 문서를 키워드로 검색한다. 제목·본문·태그를 찾는다."
            ),
            DecoyTool("get_page", "문서 ID로 위키 문서 본문을 가져온다."),
            DecoyTool("list_spaces", "위키 스페이스(팀별 문서 공간) 목록을 돌려준다."),
            DecoyTool(
                "search_incident_reports", "장애 회고 문서를 서비스 이름이나 키워드로 찾는다."
            ),
            DecoyTool("get_design_doc", "설계 문서(ADR 포함)를 제목이나 ID로 가져온다."),
        ),
    ),
    # 이슈 트래커: 작업 중간 과제(E2)에서 관련 티켓을 찾을 법한 도구
    "tracker": DecoyServer(
        instructions="팀 이슈 트래커의 티켓을 검색·조회한다. "
        "버그, 작업, 에픽, 스프린트 정보를 다룬다.",
        tools=(
            DecoyTool("search_issues", "티켓을 키워드·상태·담당자로 검색한다."),
            DecoyTool("get_issue", "티켓 키로 티켓 상세(설명, 댓글, 연결 티켓)를 가져온다."),
            DecoyTool("list_sprint_issues", "현재 스프린트의 티켓 목록을 돌려준다."),
            DecoyTool("get_epic", "에픽 키로 에픽과 하위 티켓을 가져온다."),
        ),
    ),
    # 라이브러리 문서: 기술 비교·도입 질문에서 sonbae와 경쟁한다
    "devdocs": DecoyServer(
        instructions="오픈소스 라이브러리·프레임워크의 공식 문서와 예제 코드를 검색한다. "
        "API 사용법, 설정 방법, 버전별 변경 사항을 확인할 때 쓴다.",
        tools=(
            DecoyTool("resolve_library", "라이브러리 이름으로 문서 ID를 찾는다."),
            DecoyTool("search_docs", "문서 ID와 주제로 공식 문서 내용을 검색한다."),
            DecoyTool("get_examples", "라이브러리의 예제 코드를 주제별로 가져온다."),
            DecoyTool("get_changelog", "라이브러리의 버전별 변경 사항을 가져온다."),
        ),
    ),
}


def build(name: str) -> MCPServer:
    spec = SERVERS[name]
    server = MCPServer(name=name, instructions=spec.instructions)
    for tool in spec.tools:

        def handler(query: str = "") -> str:
            return EMPTY

        server.add_tool(
            handler,
            name=tool.name,
            description=tool.description,
            annotations=READ_ONLY,
            structured_output=False,
        )
    return server


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SERVERS:
        sys.exit(f"사용법: python eval/agent_decoy.py {{{'|'.join(SERVERS)}}}")
    build(sys.argv[1]).run()


if __name__ == "__main__":
    main()
