"""호출률 평가 실행기 (이슈 #30): 다른 도구와 경쟁하거나 작업 도중일 때 sonbae를 부르는지 잰다.

    uv run python eval/agent_call_run.py --dry-run              # 실행 계획과 Voyage 토큰 예상
    uv run python eval/agent_call_run.py --tasks c01 --reps 1   # 일부만 실행
    uv run python eval/agent_call_run.py                        # 전체 (이미 끝난 실행은 건너뜀)
    uv run python eval/agent_call_run.py --rebuild              # 원본 로그로 records.jsonl 재생성

M7(eval/agent_run.py)은 빈 폴더에 sonbae 하나만 붙이고 첫 턴에 설계 질문을 던졌다. 여기서는
- E1(경쟁): 빈 폴더에 미끼 MCP 서버(eval/agent_decoy.py)와 웹 검색을 함께 붙이고 M7 과제를 푼다
- E2(작업 도중): 예제 저장소(eval/agent/fixtures/shop-api)에서 코딩 과제를 푼다. 과제 안에 설계
  판단이 숨어 있거나(불러야 함) 없다(부르면 안 됨)
서버는 이슈 #30 변경 전·후 두 버전을 git archive로 꺼내 따로 설치해 띄운다. 두 버전 모두 기본
동작(도구 정의 지연 로드)이다. 과제는 eval/agent/call_tasks.toml, 집계는 eval/agent_call_report.py.
결과는 eval/runs/agent/<name>/에 둔다 (raw/, servers/, mcp_*.json은 git 제외).
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 스크립트로 실행할 때도 eval 모듈을 import할 수 있게 한다
    sys.path.insert(0, str(ROOT))

from eval.agent_decoy import SERVERS as DECOY_SERVERS  # noqa: E402
from eval.agent_run import _event, _text, clean_env, is_complete  # noqa: E402
from eval.agent_tasks import load_tasks  # noqa: E402

RUNS_DIR = ROOT / "eval" / "runs" / "agent"
TASKS_PATH = ROOT / "eval" / "agent" / "call_tasks.toml"
FIXTURE = ROOT / "eval" / "agent" / "fixtures" / "shop-api"
DECOY_SCRIPT = ROOT / "eval" / "agent_decoy.py"

# 사용자가 README대로 등록한 이름과 같게 한다 (M7은 techblog였음). 도구 이름이 mcp__sonbae__*가 된다
SERVER_NAME = "sonbae"
SONBAE_PREFIX = f"mcp__{SERVER_NAME}__"
# 서버 버전: 이슈 #30 변경 전 커밋과 변경 후(기본은 실행할 때의 HEAD)
VERSIONS = {"before": "0046dbf", "after": "HEAD"}
# 서버 버전을 꺼낼 때 필요한 파일 (패키지 빌드·설치에 쓰는 것만)
SERVER_FILES = ["pyproject.toml", "uv.lock", "README.md", "LICENSE", "src"]

COMMON_TOOLS = [
    f"mcp__{SERVER_NAME}",
    *(f"mcp__{n}" for n in DECOY_SERVERS),
    "WebSearch",
    "WebFetch",
]
# 작업 도중 조건은 코드를 읽고 고치고 테스트를 돌릴 수 있어야 한다. Bash는 python·ls만 허용한다
CODE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python *)", "Bash(ls *)"]

# 검색 1회의 리랭커 토큰 (CLAUDE.md의 실측 평균). 실행 계획에 예상 사용량을 보여 준다
RERANK_TOKENS_PER_SEARCH = 21_700
SEARCHES_PER_RUN = 3  # M7 실측: 실행당 검색 3회

Env = Literal["e1", "e2"]
ExpectCall = Literal["required", "forbidden", "optional"]


class CallTask(BaseModel):
    id: str
    env: Env
    expect_call: ExpectCall
    prompt: str
    category: str = ""  # E1은 M7 과제 분류, E2는 비움
    check: str = ""


def load_call_tasks(path: Path = TASKS_PATH) -> list[CallTask]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    m7 = {t.id: t for t in load_tasks()}
    tasks = []
    for tid in raw["e1"]["tasks"]:
        t = m7[tid]
        tasks.append(
            CallTask(
                id=t.id, env="e1", expect_call=t.expect_call, prompt=t.prompt, category=t.category
            )
        )
    tasks += [CallTask(env="e2", **t) for t in raw.get("e2", [])]
    ids = [t.id for t in tasks]
    duplicated = {i for i in ids if ids.count(i) > 1}
    if duplicated:
        raise ValueError(f"과제 id 중복: {sorted(duplicated)}")
    return tasks


@dataclass(frozen=True)
class RunSpec:
    task: CallTask
    version: str
    rep: int

    @property
    def key(self) -> str:
        return f"{self.task.id}__{self.version}__r{self.rep}"


def plan_runs(tasks: list[CallTask], versions: list[str], reps: int) -> list[RunSpec]:
    return [RunSpec(t, v, r) for t in tasks for v in versions for r in range(1, reps + 1)]


def estimate_rerank_tokens(specs: list[RunSpec]) -> int:
    # 부르면 안 됨 과제는 검색하지 않는다고 본다
    runs = sum(s.task.expect_call != "forbidden" for s in specs)
    return runs * SEARCHES_PER_RUN * RERANK_TOKENS_PER_SEARCH


# ---------- 서버 버전 준비 ----------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def server_executable(server_dir: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return server_dir / ".venv" / scripts / f"sonbae{suffix}"


def prepare_server(out_dir: Path, version: str, rev: str) -> tuple[Path, str]:
    """서버 버전을 out_dir/servers/<version>/에 꺼내 설치하고 (실행 파일, 커밋)을 돌려준다.

    작업 폴더의 코드를 바로 쓰지 않고 커밋에서 꺼내므로, 실행 중에 코드를 고쳐도 결과가
    바뀌지 않는다.
    이미 꺼낸 버전은 커밋이 같으면 다시 쓴다.
    """
    commit = _git("rev-parse", rev)
    server_dir = out_dir / "servers" / version
    marker = server_dir / ".commit"
    if not (marker.exists() and marker.read_text().strip() == commit):
        if server_dir.exists():
            shutil.rmtree(server_dir)
        server_dir.mkdir(parents=True)
        archive = subprocess.run(
            ["git", "archive", "--format=tar", commit, *SERVER_FILES],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(server_dir, filter="data")
        # 서버 실행에 필요한 의존성만 설치한다 (dev·pipeline 그룹 제외)
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-default-groups", "--directory", str(server_dir)],
            check=True,
            capture_output=True,
        )
        marker.write_text(commit)
    return server_executable(server_dir), commit


def mcp_config(sonbae_exe: Path, db_path: Path) -> dict:
    servers = {
        SERVER_NAME: {
            "type": "stdio",
            "command": str(sonbae_exe),
            "args": [],
            # 두 버전이 같은 DB를 쓰게 고정한다.
            # VOYAGE_API_KEY는 설정 파일에 쓰지 않고 claude 프로세스 환경에서 물려받는다
            "env": {"SONBAE_DB": str(db_path)},
        }
    }
    for name in DECOY_SERVERS:
        servers[name] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(DECOY_SCRIPT), name],
        }
    return {"mcpServers": servers}


def allowed_tools(env: Env) -> list[str]:
    return COMMON_TOOLS + (CODE_TOOLS if env == "e2" else [])


def build_command(claude: str, spec: RunSpec, model: str, config_path: Path) -> list[str]:
    return [
        claude,
        "-p",
        spec.task.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--strict-mcp-config",
        "--mcp-config",
        str(config_path),
        "--setting-sources",
        "project",
        "--settings",
        json.dumps({"autoMemoryEnabled": False}),
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--allowedTools",
        " ".join(allowed_tools(spec.task.env)),
    ]


def read_env_file(path: Path | None) -> dict[str, str]:
    """KEY=VALUE 줄만 읽는다. 값은 출력하지 않는다."""
    if path is None or not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def run_env(env_file: Path | None) -> dict[str, str]:
    env = clean_env(dict(os.environ))
    key = env.get("VOYAGE_API_KEY") or read_env_file(env_file).get("VOYAGE_API_KEY")
    if key:
        env["VOYAGE_API_KEY"] = key
    return env


# 출력 없이 끝나는 실행을 다시 시도하는 횟수. 시험 실행에서 claude 여러 개를 동시에 띄우면 가끔
# 출력 없이 끝났고, 같은 실행을 혼자 다시 돌리면 정상이었다
RETRIES = 2


def run_one(
    spec: RunSpec, out_dir: Path, claude: str, model: str, timeout: int, env: dict[str, str]
) -> Path:
    raw = out_dir / "raw" / f"{spec.key}.jsonl"
    for _ in range(RETRIES + 1):
        _run_once(spec, out_dir, claude, model, timeout, env, raw)
        if is_complete(raw):
            break
    return raw


def _run_once(
    spec: RunSpec,
    out_dir: Path,
    claude: str,
    model: str,
    timeout: int,
    env: dict[str, str],
    raw: Path,
) -> None:
    command = build_command(claude, spec, model, out_dir / f"mcp_{spec.version}.json")
    # 실행마다 새 폴더에서 돌린다. 작업 도중 조건은 예제 저장소를 복사해 둔다
    with tempfile.TemporaryDirectory(prefix="sonbae-call-") as workspace:
        if spec.task.env == "e2":
            shutil.copytree(FIXTURE, workspace, dirs_exist_ok=True)
        try:
            proc = subprocess.run(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
            stdout, stderr = proc.stdout, proc.stderr
            if proc.returncode:
                stderr += f"\n[exit {proc.returncode}]".encode()
        except subprocess.TimeoutExpired as e:
            stdout, stderr = e.stdout or b"", (e.stderr or b"") + b"\n[timeout]"
    partial = raw.with_suffix(".part")
    partial.write_bytes(stdout)
    if stderr.strip():
        raw.with_suffix(".err").write_bytes(stderr)
    partial.replace(raw)


# ---------- 기록 추출 ----------

RESULT_PREVIEW = 300  # 도구 결과는 앞부분만 남긴다 (기록 크기를 줄이고, 대체 검색 여부만 보면 됨)


class ToolCall(BaseModel):
    name: str  # 전체 이름 (mcp__sonbae__search, mcp__wiki__search_pages, Read, ToolSearch 등)
    input: dict
    result: str | None = None
    is_error: bool = False


class CallRecord(BaseModel):
    key: str
    task_id: str
    version: str
    rep: int
    model: str | None = None
    mcp_status: dict[str, str] = {}
    tool_calls: list[ToolCall] = []
    denied: list[str] = []
    answer: str = ""
    result_subtype: str | None = None
    is_error: bool = False
    num_turns: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None


def parse_stream(lines: Iterable[str], spec_key: str) -> CallRecord:
    task_id, version, rep = spec_key.split("__")
    record = CallRecord(key=spec_key, task_id=task_id, version=version, rep=int(rep[1:]))
    calls: dict[str, ToolCall] = {}
    for line in lines:
        event = _event(line)
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            record.model = event.get("model")
            record.mcp_status = {
                s.get("name", ""): s.get("status", "") for s in event.get("mcp_servers") or []
            }
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    call = ToolCall(name=block["name"], input=block.get("input") or {})
                    calls[block["id"]] = call
                    record.tool_calls.append(call)
        elif kind == "user":
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    call = calls[block["tool_use_id"]]
                    call.result = _text(block.get("content"))[:RESULT_PREVIEW]
                    call.is_error = bool(block.get("is_error"))
        elif kind == "result":
            record.answer = event.get("result") or ""
            record.result_subtype = event.get("subtype")
            record.is_error = bool(event.get("is_error"))
            record.num_turns = event.get("num_turns")
            record.cost_usd = event.get("total_cost_usd")
            record.duration_ms = event.get("duration_ms")
            record.denied = [d.get("tool_name", "") for d in event.get("permission_denials") or []]
    if record.result_subtype is None:  # 시간 초과 등으로 result 이벤트가 없음
        record.is_error = True
    return record


def rebuild_records(out_dir: Path) -> list[CallRecord]:
    records = []
    for raw in sorted((out_dir / "raw").glob("*.jsonl")):
        records.append(parse_stream(raw.read_text(encoding="utf-8").splitlines(), raw.stem))
    with (out_dir / "records.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")
    return records


# ---------- 실행 환경 기록 ----------


def write_meta(out_dir: Path, claude: str, model: str, reps: int, commits: dict, db: Path) -> None:
    def output(command: list[str]) -> str:
        return subprocess.run(command, capture_output=True, text=True).stdout.strip()

    meta = {
        "claude_code_version": output([claude, "--version"]),
        "model": model,
        "reps": reps,
        "server_commits": commits,
        "runner_commit": _git("rev-parse", "HEAD"),
        "db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------- CLI ----------


def default_db() -> Path:
    repo_db = ROOT / "data" / "techblog.sqlite"
    if repo_db.exists():
        return repo_db
    from sonbae import db  # 저장소 DB가 없으면 서버가 받아 둔 캐시 DB를 쓴다

    return db.db_path()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default="issue30-call-sonnet")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=3, help="동시에 띄울 claude 프로세스 수")
    parser.add_argument("--timeout", type=int, default=900, help="실행 1회의 제한 시간(초)")
    parser.add_argument("--tasks", help="쉼표로 구분한 과제 id (기본: 전체)")
    parser.add_argument("--versions", default=",".join(VERSIONS))
    parser.add_argument("--after-rev", default=VERSIONS["after"], help="변경 후 버전의 커밋")
    parser.add_argument("--db", type=Path, help="검색 DB (기본: 저장소 DB, 없으면 캐시 DB)")
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="VOYAGE_API_KEY를 읽을 .env"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    out_dir = RUNS_DIR / args.name
    if args.rebuild:
        print(f"기록 {len(rebuild_records(out_dir))}건 → {out_dir / 'records.jsonl'}")
        return

    tasks = load_call_tasks()
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t.id in wanted]
    versions = args.versions.split(",")
    unknown = [v for v in versions if v not in VERSIONS]
    if unknown:
        parser.error(f"알 수 없는 버전: {unknown}")
    specs = plan_runs(tasks, versions, args.reps)
    todo = [s for s in specs if not is_complete(out_dir / "raw" / f"{s.key}.jsonl")]
    tokens = estimate_rerank_tokens(todo)
    print(f"계획 {len(specs)}회, 남은 실행 {len(todo)}회 → {out_dir}")
    print(f"Voyage 리랭커 예상: 약 {tokens:,} 토큰 (검색 {SEARCHES_PER_RUN}회/실행 가정)")
    if args.dry_run or not todo:
        return

    env = run_env(args.env_file)
    if "VOYAGE_API_KEY" not in env:
        sys.exit("VOYAGE_API_KEY가 없습니다. 환경 변수나 --env-file로 주세요")
    claude = shutil.which("claude")
    if claude is None:
        sys.exit("claude 명령을 찾을 수 없습니다")
    db_path = (args.db or default_db()).resolve()
    revs = {**VERSIONS, "after": args.after_rev}
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    commits = {}
    for version in versions:
        exe, commits[version] = prepare_server(out_dir, version, revs[version])
        config = json.dumps(mcp_config(exe, db_path), indent=2)
        (out_dir / f"mcp_{version}.json").write_text(config, encoding="utf-8")
        print(f"서버 {version}: {commits[version][:7]}")
    write_meta(out_dir, claude, args.model, args.reps, commits, db_path)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(run_one, s, out_dir, claude, args.model, args.timeout, env): s for s in todo
        }
        for i, future in enumerate(as_completed(futures), 1):
            spec = futures[future]
            raw = future.result()
            record = parse_stream(raw.read_text(encoding="utf-8").splitlines(), spec.key)
            sonbae = sum(c.name.startswith(SONBAE_PREFIX) for c in record.tool_calls)
            status = "오류" if record.is_error else "완료"
            print(
                f"[{i}/{len(todo)}] {spec.key} {status} · 도구 {len(record.tool_calls)}회"
                f" (sonbae {sonbae}) · {record.duration_ms}ms",
                flush=True,
            )

    records = rebuild_records(out_dir)
    print(f"기록 {len(records)}건 → {out_dir / 'records.jsonl'}")


if __name__ == "__main__":
    main()
