"""Any command-line model runner: ``codex exec``, ``claude -p``, ``gemini``, ``agy``, ``ollama run``..."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base import Completion, Provider, ProviderError, TransientError, classify


class CLIProvider(Provider):
    """Options:

    ``command``  list of arguments; placeholders ``{model}``, ``{output_file}``, ``{prompt}``.
    ``stdin``    send the prompt on stdin (default true; Windows command lines max out at 32k chars).
    ``env``      extra environment variables.

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

    def _send(self, model: str, system: str, user: str) -> Completion:
        prompt = f"{system}\n\n{user}" if system else user
        uses_file = any("{output_file}" in arg for arg in self.command)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.txt"
            args = [arg.replace("{model}", model).replace("{output_file}", str(output))
                    .replace("{prompt}", prompt) for arg in self.command]
            args[0] = shutil.which(args[0]) or args[0]
            try:
                proc = subprocess.run(args, input=prompt if self.stdin else None, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      timeout=self.timeout, env=self.env)
            except subprocess.TimeoutExpired as exc:
                raise TransientError(f"{self.command[0]} timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise ProviderError(f"cannot run {self.command[0]}: {exc}") from exc
            text = proc.stdout
            if uses_file and output.exists():
                text = output.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not text.strip():
            detail = (proc.stderr or proc.stdout or "").strip()[-2000:] or f"exit code {proc.returncode}"
            raise classify(0, detail)
        return Completion(text.strip(), model, {})
