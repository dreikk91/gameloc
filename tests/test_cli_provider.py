from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gameloc.providers import QuotaExhausted, TransientError
from gameloc.providers.cli import CLIProvider


def options(**extra):
    return {"model": "test", "retries": 1, "conversation": True,
            "command": ["codex", "exec", "--sandbox", "read-only", "--model", "{model}",
                        "--output-last-message", "{output_file}", "-"], **extra}


def test_codex_conversation(monkeypatch):
    calls = []
    turns = 0

    def run(args, **kwargs):
        nonlocal turns
        turns = turns + 1 if "resume" in args else 1
        calls.append((args, kwargs["input"]))
        Path(args[args.index("--output-last-message") + 1]).write_text("answer", encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "session-1"},
                  {"type": "turn.completed", "usage": {"input_tokens": 100 * turns,
                                                        "cached_input_tokens": 80 * turns,
                                                        "output_tokens": 5 * turns}}]
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr("gameloc.providers.cli.subprocess.run", run)
    provider = CLIProvider(options())
    assert provider.complete("rules", "first").usage["cached_input_tokens"] == 80
    assert calls[-1][1] == "rules\n\nfirst" and "resume" not in calls[-1][0]
    assert "--json" in calls[-1][0]
    second = provider.complete("rules", "second")
    assert second.text == "answer" and second.usage == {
        "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}
    assert calls[-1][0][-3:] == ["resume", "session-1", "-"]
    assert calls[-1][1] == "second"
    provider.complete("changed rules", "third")
    assert "resume" not in calls[-1][0] and calls[-1][1] == "changed rules\n\nthird"
    provider._send("another-model", "changed rules", "fourth")
    assert "resume" not in calls[-1][0]
    provider.reset()
    provider.complete("changed rules", "fifth")
    assert "resume" not in calls[-1][0]


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing-file", "bad-json", "missing-session", "failed-turn"])
def test_codex_failure_resets_session(monkeypatch, failure):
    provider = CLIProvider(options())
    provider._session_id, provider._system, provider._model = "session-1", "rules", "test"

    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 1)
        if failure != "missing-file":
            Path(args[args.index("--output-last-message") + 1]).write_text("answer", encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "session-1"}, {"type": "turn.completed"}]
        if failure == "missing-session":
            events.pop(0)
        if failure == "failed-turn":
            events[-1] = {"type": "turn.failed"}
        stdout = "not json" if failure == "bad-json" else "\n".join(map(json.dumps, events))
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0, stdout, "failed")

    monkeypatch.setattr("gameloc.providers.cli.subprocess.run", run)
    with pytest.raises(TransientError):
        provider.complete("rules", "next")
    assert provider._session_id is None


@pytest.mark.parametrize("extra", [{"stdin": False}, {"command": ["claude", "-p"]},
                                   {"command": ["codex", "exec", "--ephemeral", "{output_file}", "-"]}])
def test_conversation_rejects_incompatible_commands(extra):
    with pytest.raises(ValueError, match="conversation requires"):
        CLIProvider(options(**extra))


def test_stateless_cli_unchanged(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["input"]))
        return subprocess.CompletedProcess(args, 0, "plain answer", "")

    monkeypatch.setattr("gameloc.providers.cli.subprocess.run", run)
    provider = CLIProvider(options(conversation=False, command=["runner", "{model}"]))
    for _ in range(2):
        assert provider.complete("rules", "request").text == "plain answer"
    assert all(prompt == "rules\n\nrequest" and "--json" not in args for args, prompt in calls)


@pytest.mark.parametrize("conversation", [False, True])
def test_codex_disables_extensions_by_default(conversation):
    provider = CLIProvider(options(conversation=conversation))
    assert provider.command[2:16] == [
        "--disable", "hooks", "--disable", "plugins", "--disable", "skill_search",
        "--enable", "skip_host_skill_discovery", "-c", "skills.bundled.enabled=false",
        "-c", "skills.include_instructions=false",
        "--sandbox", "read-only",
    ]


def test_codex_extensions_opt_in():
    config = options(codex_extensions=True)
    assert CLIProvider(config).command == config["command"]


@pytest.mark.parametrize("command", [["claude", "-p"], ["runner", "{model}"], ["codex", "review"]])
def test_other_commands_do_not_receive_codex_defaults(command):
    assert CLIProvider(options(command=command, conversation=False)).command == command


@pytest.mark.parametrize("reply_file", [False, True])
def test_codex_failure_reason_comes_from_json_events_not_stderr(monkeypatch, reply_file):
    limit = "You've hit your usage limit. Upgrade to Pro or try again at 6:57 PM."

    def run(args, **kwargs):
        if reply_file:
            Path(args[args.index("--output-last-message") + 1]).write_text("partial", encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "session-1"}, {"type": "error", "message": limit},
                  {"type": "turn.failed", "error": {"message": limit}}]
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)),
                                           "ERROR codex_core: failed to load skill SKILL.md: missing YAML frontmatter")

    monkeypatch.setattr("gameloc.providers.cli.subprocess.run", run)
    with pytest.raises(QuotaExhausted, match="usage limit"):
        CLIProvider(options()).complete("rules", "next")
