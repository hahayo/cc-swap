"""Tests for quota-based switching of saved Codex accounts."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap import codex
from claude_swap.autoswitch import TickOutcome
from claude_swap.codex_autoswitch import CodexAutoSwitchEngine
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import AutoSwitchSettings
from claude_swap.usage_store import UsageEntry


def _account(number: str, pct: float, *, active: bool = False) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        email=f"{number}@example.com",
        org_name="Codex",
        org_uuid=number,
        is_active=active,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(last_good={"five_hour": {"pct": pct}}),
    )


class FakeCodexSwitcher:
    def __init__(self, tmp_path: Path, accounts: tuple[AccountSnapshot, ...]) -> None:
        self.backup_dir = tmp_path
        self.accounts = accounts
        self.switched_to: list[str] = []
        self.fetches: list[set[str] | None] = []

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetches.append(fetch)
        active = next((a.number for a in self.accounts if a.is_active), None)
        return AccountsSnapshot(active, self.accounts, taken_at=0)

    def switch_to(self, number: str, *, json_output: bool) -> dict:
        self.switched_to.append(number)
        source = next(account for account in self.accounts if account.is_active)
        target = next(account for account in self.accounts if account.number == number)
        return {
            "switched": True,
            "from": {"number": int(source.number), "email": source.email},
            "to": {"number": int(target.number), "email": target.email},
        }


def test_switches_to_the_healthiest_candidate_and_records_restart_warning(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(codex, "is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 95, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    assert switcher.fetches == [None]
    assert switcher.switched_to == ["2"]
    switch = events[-1]
    assert switch.kind == "switch"
    assert len(switch.warnings) == 1
    assert "next time you start Codex" in switch.warnings[0]


def test_real_switcher_blocks_auto_switch_while_codex_is_running(
    tmp_path, monkeypatch
):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex, "get_backup_root", lambda: tmp_path / "backup")
    switcher = codex.CodexAccountSwitcher()

    def auth(email: str, account_id: str) -> dict:
        return {
            "auth_mode": "chatgpt",
            "agent_identity": {"email": email},
            "tokens": {
                "account_id": account_id,
                "access_token": f"fake-access-{account_id}",
                "refresh_token": f"fake-refresh-{account_id}",
            },
        }

    first = auth("one@example.com", "account-one")
    second = auth("two@example.com", "account-two")
    (home / "auth.json").write_text(json.dumps(first), encoding="utf-8")
    switcher.add_account(assume_yes=True)
    (home / "auth.json").write_text(json.dumps(second), encoding="utf-8")
    switcher.add_account(assume_yes=True)
    live_before_tick = (home / "auth.json").read_bytes()
    monkeypatch.setattr(
        switcher,
        "accounts_snapshot",
        lambda fetch=None: AccountsSnapshot(
            "2",
            (_account("2", 95, active=True), _account("1", 20)),
            taken_at=0,
        ),
    )
    monkeypatch.setattr(codex, "is_codex_running", lambda: True)
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.ERROR
    assert events[-1].kind == "error"
    assert "Codex is running" in events[-1].message
    assert (home / "auth.json").read_bytes() == live_before_tick


def test_records_next_launch_warning_when_codex_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_swap.codex.is_codex_running", lambda: False)
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 95, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    switch = events[-1]
    assert len(switch.warnings) == 1
    assert "next time you start Codex" in switch.warnings[0]


def test_does_not_switch_while_active_account_is_below_threshold(tmp_path):
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 75, active=True), _account("2", 10))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher, AutoSwitchSettings(), events.append, clock=lambda: 1_000
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.NO_ACTION
    assert switcher.switched_to == []
    assert events[-1].kind == "no-switch"
    assert events[-1].reason == "below-threshold"


def test_dry_run_reports_a_switch_without_changing_the_active_account(tmp_path):
    switcher = FakeCodexSwitcher(
        tmp_path, (_account("1", 100, active=True), _account("2", 20))
    )
    events = []
    engine = CodexAutoSwitchEngine(
        switcher,
        AutoSwitchSettings(),
        events.append,
        dry_run=True,
        clock=lambda: 1_000,
    )

    outcome = engine.tick()

    assert outcome is TickOutcome.SWITCHED
    assert switcher.switched_to == []
    assert events[-1].kind == "switch"
    assert events[-1].dry_run
