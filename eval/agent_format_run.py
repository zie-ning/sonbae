"""응답 형식 평가 실행기 (이슈 #36): get_details 응답을 줄인 서버와 이전 서버로 M7 과제를 풀게 한다.

    uv run python eval/agent_format_run.py --dry-run           # 실행 계획과 Voyage 토큰 예상
    uv run python eval/agent_format_run.py --tasks t02 --reps 1  # 일부만 실행
    uv run python eval/agent_format_run.py                     # 전체 (끝난 실행은 건너뜀)
    uv run python eval/agent_format_run.py --rebuild           # records.jsonl 재생성

환경은 M7 mcp 조건과 같다: 빈 폴더에 sonbae 하나만 붙이고(이름도 M7과 같은 techblog) 첫 턴에 설계
질문을 던진다. 서버는 두 버전을 따로 설치해 띄운다.
- before: 이슈 #36 변경 전 커밋 (근거 발췌·분류·기술·태그를 상세에 넣음)
- after: 변경 후. 기본은 작업 폴더의 코드(WORKTREE)이고, 커밋을 주면 그 커밋에서 꺼낸다
과제는 M7 과제 중 search·get_details를 쓰는 분류만 쓴다 (aggregate·no_call·concept 제외).
채점·집계는 eval/agent_format_eval.py. 결과는 eval/runs/agent/<name>/에 둔다
(raw/, servers/, mcp_*.json은 git 제외). records.jsonl은 M7 형식(eval/agent_run.RunRecord)이고
condition 자리에 서버 버전을 쓴다.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 스크립트로 실행할 때도 eval 모듈을 import할 수 있게 한다
    sys.path.insert(0, str(ROOT))

from eval.agent_call_run import (  # noqa: E402
    RERANK_TOKENS_PER_SEARCH,
    SERVER_FILES,
    _git,
    default_db,
    prepare_server,
    run_env,
    server_executable,
)
from eval.agent_run import (  # noqa: E402
    MCP_TOOLS,
    RUNS_DIR,
    _output,
    is_complete,
    parse_stream,
    rebuild_records,
)
from eval.agent_tasks import Task, load_tasks  # noqa: E402

# before는 이슈 #36 변경 전 main. after의 WORKTREE는 커밋하지 않은 작업 폴더 코드를 쓴다는 뜻
VERSIONS = {"before": "af1553b", "after": "WORKTREE"}
WORKTREE = "WORKTREE"
CATEGORIES = {"design", "open", "absent", "noise", "few"}
# M7 mcp 조건의 분류별 실행당 검색 수 (실측). 실행 계획에 Voyage 예상 사용량을 보여 준다
SEARCHES_PER_RUN = {"design": 2.8, "open": 2.87, "absent": 3.58, "noise": 2.33, "few": 2.33}


@dataclass(frozen=True)
class RunSpec:
    task: Task
    version: str
    rep: int

    @property
    def key(self) -> str:
        return f"{self.task.id}__{self.version}__r{self.rep}"


def select_tasks(tasks: list[Task], ids: list[str] | None = None) -> list[Task]:
    picked = [t for t in tasks if t.category in CATEGORIES]
    if ids:
        unknown = set(ids) - {t.id for t in picked}
        if unknown:
            raise ValueError(f"평가 대상이 아닌 과제: {sorted(unknown)}")
        picked = [t for t in picked if t.id in ids]
    return picked


def plan_runs(tasks: list[Task], versions: list[str], reps: int) -> list[RunSpec]:
    return [RunSpec(t, v, r) for t in tasks for v in versions for r in range(1, reps + 1)]


def estimate_rerank_tokens(specs: list[RunSpec]) -> int:
    return round(sum(SEARCHES_PER_RUN[s.task.category] for s in specs) * RERANK_TOKENS_PER_SEARCH)


# ---------- 서버 준비 ----------


def _worktree_hash() -> str:
    digest = hashlib.sha256()
    for name in SERVER_FILES:
        path = ROOT / name
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            digest.update(f.relative_to(ROOT).as_posix().encode())
            digest.update(f.read_bytes())
    return digest.hexdigest()


def prepare_worktree_server(out_dir: Path, version: str) -> tuple[Path, str]:
    """작업 폴더의 서버 코드를 복사해 설치한다. 내용이 같으면 다시 쓴다."""
    stamp = f"worktree:{_worktree_hash()[:16]}"
    server_dir = out_dir / "servers" / version
    marker = server_dir / ".commit"
    if not (marker.exists() and marker.read_text().strip() == stamp):
        if server_dir.exists():
            shutil.rmtree(server_dir)
        server_dir.mkdir(parents=True)
        for name in SERVER_FILES:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, server_dir / name, ignore=shutil.ignore_patterns("__pycache__")
                )
            else:
                shutil.copy2(source, server_dir / name)
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-default-groups", "--directory", str(server_dir)],
            check=True,
            capture_output=True,
        )
        marker.write_text(stamp)
    return server_executable(server_dir), stamp


def mcp_config(sonbae_exe: Path, db_path: Path) -> dict:
    # M7과 같은 이름(techblog)으로 붙여 도구 이름·기록 형식을 맞춘다.
    # VOYAGE_API_KEY는 설정 파일에 쓰지 않고 claude 프로세스 환경에서 물려받는다
    server = {
        "type": "stdio",
        "command": str(sonbae_exe),
        "args": [],
        "env": {"SONBAE_DB": str(db_path)},
    }
    return {"mcpServers": {"techblog": server}}


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
        " ".join(MCP_TOOLS),
    ]


# 출력 없이 끝나는 실행을 다시 시도하는 횟수 (eval/agent_call_run.py와 같은 이유)
RETRIES = 2


def run_one(
    spec: RunSpec, out_dir: Path, claude: str, model: str, timeout: int, env: dict[str, str]
) -> Path:
    raw = out_dir / "raw" / f"{spec.key}.jsonl"
    command = build_command(claude, spec, model, out_dir / f"mcp_{spec.version}.json")
    for _ in range(RETRIES + 1):
        # 실행마다 새 빈 폴더에서 돌려 이전 실행이 남긴 파일·프로젝트 맥락이 섞이지 않게 한다
        with tempfile.TemporaryDirectory(prefix="sonbae-format-") as workspace:
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
        if is_complete(raw):
            break
    return raw


def write_meta(out_dir: Path, claude: str, model: str, reps: int, servers: dict, db: Path) -> None:
    meta = {
        "claude_code_version": _output([claude, "--version"]),
        "model": model,
        "reps": reps,
        "servers": servers,
        "runner_commit": _git("rev-parse", "HEAD"),
        "db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------- CLI ----------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default="issue36-format-sonnet")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=3, help="동시에 띄울 claude 프로세스 수")
    parser.add_argument("--timeout", type=int, default=900, help="실행 1회의 제한 시간(초)")
    parser.add_argument("--tasks", help="쉼표로 구분한 과제 id (기본: 평가 대상 전체)")
    parser.add_argument("--versions", default=",".join(VERSIONS))
    parser.add_argument(
        "--after-rev", default=VERSIONS["after"], help="변경 후 버전 (커밋 또는 WORKTREE)"
    )
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

    tasks = select_tasks(load_tasks(), args.tasks.split(",") if args.tasks else None)
    versions = args.versions.split(",")
    unknown = [v for v in versions if v not in VERSIONS]
    if unknown:
        parser.error(f"알 수 없는 버전: {unknown}")
    specs = plan_runs(tasks, versions, args.reps)
    todo = [s for s in specs if not is_complete(out_dir / "raw" / f"{s.key}.jsonl")]
    print(f"계획 {len(specs)}회, 남은 실행 {len(todo)}회 → {out_dir}")
    print(f"Voyage 리랭커 예상: 약 {estimate_rerank_tokens(todo):,} 토큰 (M7 분류별 검색 수 기준)")
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
    servers = {}
    for version in versions:
        if revs[version] == WORKTREE:
            exe, servers[version] = prepare_worktree_server(out_dir, version)
        else:
            exe, servers[version] = prepare_server(out_dir, version, revs[version])
        config = json.dumps(mcp_config(exe, db_path), indent=2)
        (out_dir / f"mcp_{version}.json").write_text(config, encoding="utf-8")
        print(f"서버 {version}: {servers[version]}")
    write_meta(out_dir, claude, args.model, args.reps, servers, db_path)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(run_one, s, out_dir, claude, args.model, args.timeout, env): s for s in todo
        }
        for i, future in enumerate(as_completed(futures), 1):
            spec = futures[future]
            raw = future.result()
            record = parse_stream(raw.read_text(encoding="utf-8").splitlines(), spec.key)
            tools = ",".join(c.name for c in record.tool_calls) or "-"
            status = "오류" if record.is_error else "완료"
            print(
                f"[{i}/{len(todo)}] {spec.key} {status} · 도구 {tools} · {record.duration_ms}ms",
                flush=True,
            )

    records = rebuild_records(out_dir)
    print(f"기록 {len(records)}건 → {out_dir / 'records.jsonl'}")


if __name__ == "__main__":
    main()
