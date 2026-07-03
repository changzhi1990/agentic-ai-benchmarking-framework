from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from shlex import quote
from typing import Any

from .schemas import TeamRunConfig


@dataclass(frozen=True)
class DockerRuntimeConfig:
    type: str = "docker"
    image: str = "aab-mini-swe-agent:latest"
    workdir: str = "/workspace"
    network: str = "host"
    cleanup: bool = True
    cpu_limit: str | None = None
    memory_limit: str | None = None
    container_name_prefix: str = "aab-team-v2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DockerRuntime:
    """Encapsulates Docker sandbox commands for Agent Team v2.

    The mock adapter can run without launching Docker, but production command
    construction stays in this runtime boundary instead of being scattered in
    workers or the CLI.
    """

    def __init__(self, config: TeamRunConfig) -> None:
        self.config = DockerRuntimeConfig(
            type=config.runtime_type,
            image=config.runtime_image,
            workdir=config.runtime_workdir,
            network=config.runtime_network,
            cleanup=config.runtime_cleanup,
            cpu_limit=config.runtime_cpu_limit,
            memory_limit=config.runtime_memory_limit,
            container_name_prefix=config.runtime_container_name_prefix,
        )

    def to_result_config(self) -> dict[str, Any]:
        return self.config.to_dict()

    def available(self) -> bool:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def container_name(self, *, run_id: str, issue_id: str, agent_id: str) -> str:
        safe = f"{run_id}-{agent_id}-{issue_id}".replace("_", "-").replace("/", "-")
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in safe).strip("-")
        return f"{self.config.container_name_prefix}-{safe}"[:120]

    def build_create_command(
        self,
        *,
        container_name: str,
        source_dir: Path,
        work_dir: Path,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            self.config.network,
            "-w",
            self.config.workdir,
            "-v",
            f"{source_dir.resolve()}:{self.config.workdir}/source:ro",
            "-v",
            f"{work_dir.resolve()}:{self.config.workdir}/work",
        ]
        if self.config.cpu_limit:
            command.extend(["--cpus", self.config.cpu_limit])
        if self.config.memory_limit:
            command.extend(["--memory", self.config.memory_limit])
        for key, value in sorted((env or {}).items()):
            command.extend(["-e", f"{key}={value}"])
        command.extend([self.config.image, "sleep", "infinity"])
        return command

    def build_exec_command(self, container_name: str, command: list[str]) -> list[str]:
        return ["docker", "exec", container_name, *command]

    def build_one_shot_command(
        self,
        *,
        container_name: str,
        host_output_dir: Path,
        command: list[str],
        host_workdir: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        container_cwd = self.config.workdir
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            self.config.network,
            "-v",
            f"{host_output_dir.resolve()}:{self.config.workdir}/output",
        ]
        if host_workdir is not None:
            container_cwd = f"{self.config.workdir}/repo"
            docker_command.extend(["-v", f"{host_workdir.resolve()}:{container_cwd}"])
        docker_command.extend(["-w", container_cwd])
        if self.config.cpu_limit:
            docker_command.extend(["--cpus", self.config.cpu_limit])
        if self.config.memory_limit:
            docker_command.extend(["--memory", self.config.memory_limit])
        for key, value in sorted((env or {}).items()):
            docker_command.extend(["-e", f"{key}={value}"])
        docker_command.append(self.config.image)
        docker_command.extend(command)
        return docker_command

    def build_cleanup_command(self, container_name: str) -> list[str]:
        return ["docker", "rm", "-f", container_name]

    def shell_join(self, command: list[str]) -> str:
        return " ".join(quote(part) for part in command)
