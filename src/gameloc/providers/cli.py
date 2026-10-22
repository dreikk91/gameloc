"""Any command-line model runner: ``codex exec``, ``claude -p``, ``gemini``, ``agy``, ``ollama run``..."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import Completion, Provider, ProviderError, TransientError, classify

log = logging.getLogger("gameloc")


class CLIProvider(Provider):
    """Options:

    ``command``  list of arguments; placeholders ``{model}``, ``{output_file}``, ``{prompt}``.
    ``stdin``    send the prompt on stdin (default true; Windows command lines max out at 32k chars).
    ``env``      extra environment variables.
    ``conversation``  reuse a Codex exec session (default false; stdin and {output_file} required).
    ``codex_extensions``  retain configured Codex skills, plugins and hooks (default false).

    The reply is read from ``{output_file}`` when the command uses it, else from stdout.
    """

    type = "cli"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        command = options.get("command")
        if not command:
            raise ValueError("cli provider needs a 'command'")
        self.command = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
        self.stdin = bool(options.get("stdin", True))
        self.env = {**os.environ, **{k: str(v) for k, v in (options.get("env") or {}).items()}}
        self.conversation = bool(options.get("conversation", False))
        codex = Path(self.command[0]).stem.lower() == "codex" and self.command[1:2] == ["exec"]
        if self.conversation and (
                not codex or not self.stdin or self.command[-1] != "-"
                or not any("{output_file}" in arg for arg in self.command)
                or "--ephemeral" in self.command or "resume" in self.command):
            raise ValueError("conversation requires codex exec, stdin, {output_file}, final '-', "
                             "and persisted sessions")
        if codex and not options.get("codex_extensions", False):
            self.command[2:2] = [
                "--disable", "hooks", "--disable", "plugins", "--disable", "skill_search",
                "--enable", "skip_host_skill_discovery", "-c", "skills.bundled.enabled=false",
                "-c", "skills.include_instructions=false",
            ]
        self._session_id: str | None = None
        self._system: str | None = None
        self._model: str | None = None
        self._usage: dict[str, int] = {}

    def reset(self) -> None:
        self._session_id = None
        self._system = None
        self._model = None
        self._usage = {}

    def _send(self, model: str, system: str, user: str) -> Completion:
        if system != self._system or model != self._model:
            self.reset()
        continuing = self.conversation and self._session_id is not None
        prompt = f"{system}\n\n{user}" if system and not continuing else user
        uses_file = any("{output_file}" in arg for arg in self.command)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.txt"
            args = [arg.replace("{model}", model).replace("{output_file}", str(output))
                    .replace("{prompt}", prompt) for arg in self.command]
            if self.conversation:
                if "--json" not in args:
                    args.insert(2, "--json")
                if continuing:
                    args[-1:-1] = ["resume", str(self._session_id)]
            args[0] = shutil.which(args[0]) or args[0]
            try:
                proc = subprocess.run(args, input=prompt if self.stdin else None, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      timeout=self.timeout, env=self.env)
            except subprocess.TimeoutExpired as exc:
                self.reset()
                raise TransientError(f"{self.command[0]} timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                self.reset()
                raise ProviderError(f"cannot run {self.command[0]}: {exc}") from exc
            text = proc.stdout
            if self.conversation and not output.exists():
                self.reset()
                detail = (proc.stderr or proc.stdout).strip()[-2000:] or "Codex did not write its reply file"
                raise classify(0, detail)
            if uses_file and output.exists():
                text = output.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not text.strip():
            self.reset()
            detail = (proc.stderr or proc.stdout or "").strip()[-2000:] or f"exit code {proc.returncode}"
            raise classify(0, detail)
        usage: dict[str, int] = {}
        if self.conversation:
            session_id = None
            completed = False
            try:
                for line in proc.stdout.splitlines():
                    event = json.loads(line)
                    if event.get("type") == "thread.started":
                        session_id = event.get("thread_id")
                    elif event.get("type") == "turn.completed":
                        completed = True
                        usage = {k: v for k, v in (event.get("usage") or {}).items() if type(v) is int}
            except (ValueError, AttributeError, TypeError) as exc:
                self.reset()
                raise TransientError("Codex returned invalid JSON events") from exc
            if not completed or not isinstance(session_id, str) or not session_id:
                self.reset()
                raise TransientError("Codex did not report a session ID and completed turn")
            if continuing and session_id != self._session_id:
                self.reset()
                raise TransientError("Codex resumed a different session")
            # Codex reports cumulative session usage, including turns from earlier exec processes.
            totals = usage
            usage = {key: value - self._usage.get(key, 0) for key, value in totals.items()}
            self._usage = totals
            self._session_id, self._system, self._model = session_id, system, model
            log.info("Codex session %s (%s)", session_id, "resumed" if continuing else "new")
        return Completion(text.strip(), model, usage)
