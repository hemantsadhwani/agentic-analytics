"""Private mode: with LLM_PROVIDER=ollama the run makes no network connection at all.

Every socket connect is patched to raise and record. Only the Ollama HTTP call is stubbed,
at the provider boundary (``OllamaProvider._chat``), returning what a local model returns.
Everything else (spec, read-only SQLite, checker, checkpoint, audit, publish) runs for real.
"""

from __future__ import annotations

import json
import socket

import pytest

pytest.importorskip("langgraph")

from agentic_reporting import audit  # noqa: E402
from agentic_reporting.providers.fake import payload_from, prose  # noqa: E402
from agentic_reporting.providers.ollama_provider import OllamaProvider  # noqa: E402
from agentic_reporting.run import main  # noqa: E402
from conftest import OPS_SPEC  # noqa: E402


def test_ollama_run_attempts_no_other_connection(tmp_path, ops_db, monkeypatch, capsys):
    attempts: list[object] = []

    def refuse(*args, **kwargs):
        attempts.append(args[1:] or kwargs)
        raise OSError("network disabled in private-mode test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    chats: list[str] = []

    def local_model(self, system, messages, max_tokens, schema):
        chats.append(self.model)
        p = payload_from(messages)
        text, used = prose(p["columns"], p["rows"])
        return {"message": {"content": json.dumps({"text": text, "numbers_used": used})},
                "prompt_eval_count": 100, "eval_count": 40}

    monkeypatch.setattr(OllamaProvider, "_chat", local_model)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    cp = ["--checkpoint-db", str(tmp_path / "cp.sqlite"), "--out", str(tmp_path / "out")]

    assert main(["--spec", str(OPS_SPEC), "--db", str(ops_db), "--thread", "p1", *cp]) == 0
    assert "PAUSED thread=p1" in capsys.readouterr().out
    assert main(["--resume", "--thread", "p1", "--decision", "approve", *cp]) == 0
    assert "PUBLISHED " in capsys.readouterr().out

    assert attempts == []                                # nothing tried the network
    assert chats == ["qwen2.5:7b-instruct"] * 4          # one local call per section
    [row] = audit.recent(tmp_path / "cp.sqlite")
    assert (row["provider"], row["model"], row["usd"], row["unverified"]) == \
        ("ollama", "qwen2.5:7b-instruct", 0.0, 0)


def test_the_guard_itself_bites(monkeypatch):
    """Sanity check: the patch really blocks a connection attempt."""
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("example.com", 80))
