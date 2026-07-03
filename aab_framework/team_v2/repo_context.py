from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .schemas import IssueExecutionPlan, TeamRunConfig


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
}

SYMBOL_RE = re.compile(r"^\s*(class|def|function|interface|struct|enum|type)\s+([A-Za-z_][A-Za-z0-9_]*)")
IMPORT_RE = re.compile(r"^\s*(import|from|#include|require\(|use\s+|package\s+)")


@dataclass(frozen=True)
class RepoContextResult:
    enabled: bool
    repo_source: str
    files_scanned: int = 0
    bytes_scanned: int = 0
    symbols_extracted: int = 0
    imports_extracted: int = 0
    context_bundle_path: str = ""
    index_path: str = ""
    git_history_files_scanned: int = 0
    git_history_bytes_scanned: int = 0
    git_history_sec: float = 0.0
    git_log_path: str = ""
    pytest_collect_status: str = "skipped"
    pytest_collect_sec: float = 0.0
    pytest_collect_log_path: str = ""
    scan_repo_sec: float = 0.0
    build_context_sec: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def augment_prompt(self, prompt: str, *, max_chars: int = 8192) -> str:
        if not self.enabled or self.error or not self.context_bundle_path or max_chars <= 0:
            return prompt
        bundle = Path(self.context_bundle_path)
        if not bundle.exists():
            return prompt
        text = bundle.read_text(encoding="utf-8", errors="replace")[:max_chars]
        return (
            f"{prompt}\n\n"
            "Repo context bundle generated before agent execution:\n"
            f"{text}\n"
        )


@dataclass(frozen=True)
class RepoWorkspaceResult:
    mode: str
    source: str
    path: str
    prepared: bool
    prepare_repo_sec: float = 0.0
    cleanup_status: str = "not_requested"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RepoWorkspaceManager:
    def __init__(self, config: TeamRunConfig) -> None:
        self.config = config

    def prepare(self, plan: IssueExecutionPlan, issue_dir: Path) -> RepoWorkspaceResult:
        source = Path(str(plan.workdir or self.config.repo_source or "")).expanduser()
        started = time.time()
        if not source.exists() or not source.is_dir():
            return RepoWorkspaceResult(
                mode=self.config.repo_workspace_mode,
                source=str(source),
                path=str(source),
                prepared=False,
                prepare_repo_sec=round(time.time() - started, 3),
                error=f"repo source is not a directory: {source}",
            )
        if self.config.repo_workspace_mode == "source":
            return RepoWorkspaceResult(
                mode="source",
                source=str(source),
                path=str(source),
                prepared=True,
                prepare_repo_sec=round(time.time() - started, 3),
            )

        workspace = (issue_dir / "workspace").resolve()
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.parent.mkdir(parents=True, exist_ok=True)

        if self.config.repo_workspace_mode == "worktree" and _is_git_repo(source):
            result = subprocess.run(
                ["git", "-C", str(source), "worktree", "add", "--detach", str(workspace), "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return RepoWorkspaceResult(
                    mode="worktree",
                    source=str(source),
                    path=str(workspace),
                    prepared=True,
                    prepare_repo_sec=round(time.time() - started, 3),
                )

        shutil.copytree(source, workspace, ignore=_copy_ignore)
        return RepoWorkspaceResult(
            mode="copy" if self.config.repo_workspace_mode == "copy" else "copy_fallback",
            source=str(source),
            path=str(workspace),
            prepared=True,
            prepare_repo_sec=round(time.time() - started, 3),
        )


class RepoContextBuilder:
    def __init__(self, config: TeamRunConfig) -> None:
        self.config = config

    def build(self, plan: IssueExecutionPlan, issue_dir: Path) -> RepoContextResult:
        if not self.config.repo_context_enabled:
            return RepoContextResult(enabled=False, repo_source="")

        source = Path(str(plan.workdir or self.config.repo_source or "")).expanduser()
        out_dir = issue_dir / "repo_context"
        out_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = out_dir / "context_bundle.txt"
        index_path = out_dir / "repo_index.json"
        if not source.exists() or not source.is_dir():
            result = RepoContextResult(
                enabled=True,
                repo_source=str(source),
                context_bundle_path=str(bundle_path),
                index_path=str(index_path),
                error=f"repo_source is not a directory: {source}",
            )
            self._write_outputs(bundle_path, index_path, result, [])
            return result

        scan_started = time.time()
        files = self._select_files(source)
        scan_ended = time.time()
        build_started = time.time()
        entries: list[dict[str, Any]] = []
        git_history_entries: list[dict[str, Any]] = []
        bundle_parts = [
            f"# Repo context for issue {plan.issue_id}",
            f"repo_source: {source}",
            f"agent_id: {plan.agent_id}",
            "",
        ]
        bytes_scanned = 0
        symbols = 0
        imports = 0
        bundle_bytes = sum(len(part.encode("utf-8")) for part in bundle_parts)

        for path in files:
            remaining = self.config.repo_context_max_bytes - bytes_scanned
            if remaining <= 0:
                break
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if len(raw) > remaining:
                raw = raw[:remaining]
            bytes_scanned += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            text = raw.decode("utf-8", errors="replace")
            rel = path.relative_to(source).as_posix()
            file_symbols, file_imports = _extract_file_context(text)
            symbols += len(file_symbols)
            imports += len(file_imports)
            entries.append(
                {
                    "path": rel,
                    "bytes": len(raw),
                    "sha256": digest,
                    "symbols": file_symbols,
                    "imports": file_imports,
                }
            )
            snippet = _file_bundle(rel, text, file_symbols, file_imports)
            snippet_bytes = len(snippet.encode("utf-8"))
            if bundle_bytes + snippet_bytes <= self.config.repo_context_bundle_max_bytes:
                bundle_parts.append(snippet)
                bundle_bytes += snippet_bytes

        git_log_path = out_dir / "git_log.txt"
        if self.config.repo_context_include_git_history:
            git_started = time.time()
            git_history_entries, git_bytes = self._scan_git_history(source, git_log_path)
            git_ended = time.time()
        else:
            git_bytes = 0
            git_ended = build_started
            git_started = build_started

        pytest_log_path = out_dir / "pytest_collect.log"
        pytest_status, pytest_sec = self._run_pytest_collect(source, pytest_log_path)

        build_ended = time.time()
        result = RepoContextResult(
            enabled=True,
            repo_source=str(source),
            files_scanned=len(entries),
            bytes_scanned=bytes_scanned,
            symbols_extracted=symbols,
            imports_extracted=imports,
            git_history_files_scanned=len(git_history_entries),
            git_history_bytes_scanned=git_bytes,
            git_history_sec=round(git_ended - git_started, 3),
            git_log_path=str(git_log_path) if self.config.repo_context_include_git_history else "",
            pytest_collect_status=pytest_status,
            pytest_collect_sec=pytest_sec,
            pytest_collect_log_path=str(pytest_log_path) if self.config.repo_context_pytest_collect else "",
            context_bundle_path=str(bundle_path),
            index_path=str(index_path),
            scan_repo_sec=round(scan_ended - scan_started, 3),
            build_context_sec=round(build_ended - build_started, 3),
        )
        if git_history_entries:
            bundle_parts.append(
                "\n## Git history context\n"
                f"files_scanned: {len(git_history_entries)}\n"
                f"bytes_scanned: {git_bytes}\n"
                f"log_path: {git_log_path}\n"
            )
        if self.config.repo_context_pytest_collect:
            bundle_parts.append(
                "\n## Pytest collection context\n"
                f"status: {pytest_status}\n"
                f"log_path: {pytest_log_path}\n"
            )
        self._write_outputs(bundle_path, index_path, result, entries, bundle_parts, git_history_entries)
        return result

    def _select_files(self, source: Path) -> list[Path]:
        extensions = set(self.config.repo_context_extensions)
        selected: list[Path] = []
        for root, dirs, files in os.walk(source):
            dirs[:] = sorted(item for item in dirs if item not in SKIP_DIRS)
            for name in sorted(files):
                if len(selected) >= self.config.repo_context_max_files:
                    return selected
                path = Path(root) / name
                if path.suffix not in extensions:
                    continue
                selected.append(path)
        return selected

    def _write_outputs(
        self,
        bundle_path: Path,
        index_path: Path,
        result: RepoContextResult,
        entries: list[dict[str, Any]],
        bundle_parts: list[str] | None = None,
        git_history_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        if bundle_parts is None:
            bundle_parts = [f"# Repo context\nerror: {result.error or ''}\n"]
        bundle_path.write_text("\n".join(bundle_parts) + "\n", encoding="utf-8")
        index_path.write_text(
            json.dumps(
                {
                    "summary": result.to_dict(),
                    "files": entries,
                    "git_history": {"files": git_history_entries or []},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _scan_git_history(self, source: Path, git_log_path: Path) -> tuple[list[dict[str, Any]], int]:
        common_dir = _git_common_dir(source)
        if common_dir is None:
            git_log_path.write_text("not a git repository\n", encoding="utf-8")
            return [], 0
        log_result = subprocess.run(
            ["git", "-C", str(source), "log", "--stat", f"--max-count={self.config.repo_context_git_log_limit}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            check=False,
        )
        git_log_path.write_text(log_result.stdout, encoding="utf-8", errors="replace")
        candidates: list[Path] = []
        for rel in ["packed-refs", "logs/HEAD"]:
            path = common_dir / rel
            if path.exists() and path.is_file():
                candidates.append(path)
        for pattern in ["objects/pack/*.pack", "objects/pack/*.idx", "logs/refs/**/*", "refs/**/*"]:
            candidates.extend(path for path in common_dir.glob(pattern) if path.is_file())
        entries: list[dict[str, Any]] = []
        total = 0
        for path in sorted(dict.fromkeys(candidates)):
            remaining = self.config.repo_context_git_history_max_bytes - total
            if remaining <= 0:
                break
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if len(raw) > remaining:
                raw = raw[:remaining]
            total += len(raw)
            entries.append(
                {
                    "path": str(path),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        return entries, total

    def _run_pytest_collect(self, source: Path, log_path: Path) -> tuple[str, float]:
        if not self.config.repo_context_pytest_collect:
            return "skipped", 0.0
        started = time.time()
        try:
            result = subprocess.run(
                ["bash", "-lc", self.config.repo_context_pytest_command],
                cwd=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.config.repo_context_pytest_timeout_sec,
                check=False,
            )
            output = result.stdout
            status = "passed" if result.returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            status = "timeout"
        log_path.write_text(output or "", encoding="utf-8", errors="replace")
        return status, round(time.time() - started, 3)


def _extract_file_context(text: str) -> tuple[list[str], list[str]]:
    symbols: list[str] = []
    imports: list[str] = []
    for line in text.splitlines():
        if len(symbols) < 20:
            match = SYMBOL_RE.match(line)
            if match:
                symbols.append(match.group(0).strip())
        if len(imports) < 20 and IMPORT_RE.match(line):
            imports.append(line.strip())
        if len(symbols) >= 20 and len(imports) >= 20:
            break
    return symbols, imports


def _file_bundle(rel_path: str, text: str, symbols: list[str], imports: list[str]) -> str:
    lines = text.splitlines()
    snippet = "\n".join(lines[:80])
    return (
        f"\n## FILE {rel_path}\n"
        f"symbols: {symbols}\n"
        f"imports: {imports}\n"
        "```text\n"
        f"{snippet}\n"
        "```\n"
    )


def _is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_common_dir(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = path / common
    return common.resolve()


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SKIP_DIRS}
