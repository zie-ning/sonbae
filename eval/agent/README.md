# 에이전트 평가 과제셋

M7 에이전트 A/B 평가용 과제 38개. 파일은 [tasks.toml](tasks.toml), 형식은 [../agent_tasks.py](../agent_tasks.py)의 `Task`다. 평가 조건(`mcp`·`none`·`web`·`web-ask`)과 결정 근거는 [docs/기획.md "에이전트 평가 (M7)"](../../docs/기획.md#에이전트-평가-m7), 진행 기록은 [docs/milestones/M7.md](../../docs/milestones/M7.md)에 있다.

```bash
uv run python eval/agent_tasks.py   # 검증 후 과제 목록과 관련 사례 수 출력
```

## 필드

| 필드 | 뜻 |
| --- | --- |
| `id` | `t01`~ (DB에 맞춰 고른 과제), `o01`~ (DB와 무관하게 고른 과제) |
| `category` | 과제 분류 (아래 표) |
| `prompt` | 에이전트에게 그대로 넣는 입력. 도구를 쓰라거나 사례를 찾으라는 말은 넣지 않는다(`absent` 일부는 사용자가 사례를 직접 요청하는 경우를 일부러 넣음) |
| `expect_call` | `required`(불러야 함) / `forbidden`(부르면 안 됨) / `optional`(호출 여부만 관찰). 분류마다 고정이며 로더가 검사한다 |
| `expect_tool` | 특정 도구를 기대할 때만 (`aggregate` 과제) |
| `search_refs` | 관련 사례 라벨을 가져올 [검색 평가셋](../search/queries.toml) 질의 id. 여러 질의의 라벨은 등급이 높은 쪽으로 합친다 |
| `source`, `check` | 출처, 이 과제에서 특히 볼 것 |

## 분류

| 분류 | 개수 | 기대 호출 | 보는 것 |
| --- | --- | --- | --- |
| `design` | 15 | required | 사례를 찾아 출처와 함께 소개하고, 자기 제안과 구분하는가. 문제 유형 21개 중 사례가 많은 주제를 고르게 담았다 |
| `few` | 1 | required | 사례가 적다는 사실을 밝히는가 |
| `noise` | 2 | required | 검색 잡음이 알려진 주제(M6에서 넘어온 "대기열"·"캐시")에서 무관 카드를 걸러 내는가 |
| `aggregate` | 2 | required | `aggregate`를 쓰고 모수를 해석하는가 |
| `absent` | 4 | optional | DB에 없는 주제에서 사례를 지어내거나 일부 관련 카드를 사례처럼 소개하지 않는가 |
| `no_call` | 3 | forbidden | 코드 수정·단순 CRUD·버그 수정에서 부르지 않는가 |
| `concept` | 1 | optional | 개념 질문에서의 호출 비율 (정답 없음) |
| `open` | 10 | optional | DB를 보지 않고 고른 흔한 백엔드·AI 설계 질문. DB에 맞춰 고른 위 과제들의 편향을 보완한다 |

"보는 것"은 과제를 고른 의도다. 최종 채점은 분류와 상관없이 같은 과제의 두 답변 중 질문한 개발자에게 더 도움이 되는 쪽을 판정한다([../agent_judge.py](../agent_judge.py)). 호출 금지 과제는 `mcp` 조건만 돌린다.

M1 실사용 시나리오 중 9개(1~6, 7, 7-b, 7-c, 8)는 문구를 그대로 썼다. 시나리오 6-b와 9는 prompt(`/mcp__techblog__techblog`)를 직접 부르는 경우라 `none`·`web` 조건과 비교할 수 없어 뺐다.

## 관련 사례 라벨의 한계

라벨은 M5에서 검색 결과 후보를 판정한 것이다. 사례 인용을 채점하는 방식은 MCP에 유리해 버렸으므로, 라벨은 채점이 아니라 인용한 사례가 주제에 맞는지 보는 진단에만 쓴다. 에이전트가 판정되지 않은 사례를 인용할 수 있으므로 라벨 밖의 인용은 미판정으로 따로 센다. `search_refs`가 비어 있는 과제(`aggregate` t20, `no_call`, `concept`, `open`)는 라벨이 없다.

## 호출률 평가 (이슈 #30)

M7은 빈 폴더에 서버 하나만 붙이고 첫 턴에 설계 질문을 던져, 다른 도구와 경쟁하거나 작업 도중에 설계 판단을 만나는 상황을 재현하지 못했다. 이 평가는 답변 품질이 아니라 **sonbae를 부르는지**만 실행 기록으로 센다(LLM 채점 없음).

```bash
uv run python eval/agent_call_run.py --dry-run --env-file <VOYAGE_API_KEY가 든 .env>   # 실행 계획과 Voyage 토큰 예상
uv run python eval/agent_call_run.py --env-file <.env>                                 # 실행
uv run python eval/agent_call_report.py                                                # → eval/reports/issue30-call-rate.md
```

| 환경 | 폴더 | 붙는 도구 | 과제 |
| --- | --- | --- | --- |
| E1 경쟁 | 빈 폴더 | sonbae, 미끼 MCP 3개, 웹 검색 | M7 과제 16개 ([call_tasks.toml](call_tasks.toml)의 `e1`) |
| E2 작업 도중 | [fixtures/shop-api](fixtures/shop-api) 복사본 | E1 + 코드 도구(Read·Edit·Write·Glob·Grep, python·ls 명령) | 새 과제 10개 (불러야 함 6, 부르면 안 됨 4) |

- **미끼 MCP**([../agent_decoy.py](../agent_decoy.py)): 사내 위키(`wiki`), 이슈 트래커(`tracker`), 라이브러리 문서(`devdocs`)를 흉내 낸 서버로, 늘 "결과 없음"을 돌려준다. 실제 서버는 결과가 매번 달라 비교하기 어려워 쓰지 않는다
- **서버 버전**: `before`(이슈 #30 변경 전, `0046dbf`)와 `after`(변경 후)를 `git archive`로 꺼내 `servers/`에 따로 설치한다. 두 버전 모두 같은 DB를 쓰고, 기본 동작(도구 정의 지연 로드)이다. 서버 이름은 사용자가 README대로 등록하는 `sonbae`다(M7은 `techblog`)
- **E2 예제 저장소**: 주문·상품·결제 API가 있는 작은 Python 프로젝트다. 과제 n02용으로 주문 금액에 수량을 곱하지 않는 버그를 일부러 두었다
- **지표**: 호출률(불러야 함), 잘못 부른 비율(부르면 안 됨), 늦은 호출(코드를 고친 뒤에야 처음 부름), 처음 부르기 전 도구 수, 미끼·웹 호출, 대체 검색 수(0이어야 의미 검색 조건)
