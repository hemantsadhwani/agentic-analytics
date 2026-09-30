"""The CLI: pause → resume approve/reject, --list, --no-gate, and crash → --resume."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from agentic_reporting import audit  # noqa: E402
from agentic_reporting.providers.fake import FactsFakeProvider  # noqa: E402
from agentic_reporting.run import main  # noqa: E402
from conftest import SALES_SPEC  # noqa: E402


def _args(tmp_path, db, *extra):
    return ["--spec", str(SALES_SPEC), "--db", str(db), "--out", str(tmp_path / "out"),
            "--checkpoint-db", str(tmp_path / "cp.sqlite"), *extra]


def _cp(tmp_path):
    return ["--checkpoint-db", str(tmp_path / "cp.sqlite"), "--out", str(tmp_path / "out")]


def test_run_pauses_then_approve_publishes(tmp_path, sales_db, capsys, monkeypatch):
    monkeypatch.setenv("REPORT_APPROVER", "carol")
    assert main(_args(tmp_path, sales_db, "--thread", "t1", "--provider", "fake")) == 0
    out = capsys.readouterr().out
    assert "PAUSED thread=t1 (publish?)" in out
    assert "report: 4 sections, 37 numbers checked, 0 unverified" in out
    assert "budget: $0.0000/∞ · llm calls 4/∞ · stopped: paused" in out
    assert not (tmp_path / "out").exists()

    assert main(["--resume", "--thread", "t1", "--decision", "approve", *_cp(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "PUBLISHED " in out and "stopped: published" in out
    [row] = audit.recent(tmp_path / "cp.sqlite")
    assert row["decided_by"] == "carol" and row["decision"] == "approved"

    assert main(["--list", *_cp(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "approved" in out and "carol" in out


def test_reject_prints_rejected(tmp_path, sales_db, capsys):
    main(_args(tmp_path, sales_db, "--thread", "t2", "--provider", "fake"))
    assert main(["--resume", "--thread", "t2", "--decision", "reject", *_cp(tmp_path)]) == 0
    assert "REJECTED thread=t2" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_no_gate_auto_publishes(tmp_path, sales_db, capsys):
    assert main(_args(tmp_path, sales_db, "--no-gate", "--provider", "fake")) == 0
    out = capsys.readouterr().out
    assert "PUBLISHED " in out and "PAUSED" not in out
    assert audit.recent(tmp_path / "cp.sqlite")[0]["decision"] == "auto (--no-gate)"


def test_default_provider_is_fake_with_a_note(tmp_path, sales_db, capsys):
    assert main(_args(tmp_path, sales_db, "--no-gate")) == 0
    captured = capsys.readouterr()
    assert "using the offline 'fake' provider" in captured.err
    assert "provider=fake" in captured.out


class CrashOnSecondSection(FactsFakeProvider):
    """Raises on the second model call, once, like a process dying mid-run."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.crashed = False

    def complete(self, **kw):
        self.attempts += 1
        if self.attempts == 2 and not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated crash on section 2")
        return super().complete(**kw)


def test_crash_then_resume_completes_without_redrafting(tmp_path, sales_db, capsys):
    provider = CrashOnSecondSection()
    rc = main(_args(tmp_path, sales_db, "--thread", "tc", "--no-gate"), provider=provider)
    err = capsys.readouterr().err
    assert rc == 1 and "CRASHED thread=tc" in err and "--resume --thread tc" in err
    assert len(provider.calls) == 1                     # section 1 drafted and checkpointed

    rc = main(["--resume", "--thread", "tc", *_cp(tmp_path)], provider=provider)
    out = capsys.readouterr().out
    assert rc == 0 and "continuing thread tc" in out and "PUBLISHED " in out
    assert len(provider.calls) == 4                     # section 1 was NOT re-drafted
    assert audit.recent(tmp_path / "cp.sqlite")[0]["thread"] == "tc"


def test_resume_guards(tmp_path, sales_db, capsys):
    assert main(["--resume", "--thread", "nope", *_cp(tmp_path)]) == 2
    main(_args(tmp_path, sales_db, "--thread", "t3", "--provider", "fake"))
    assert main(["--resume", "--thread", "t3", *_cp(tmp_path)]) == 2      # paused: needs decision
    assert "paused at the gate" in capsys.readouterr().err
