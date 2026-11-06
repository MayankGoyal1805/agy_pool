"""Switching the account behind an instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import procs, tokens
from .store import AUTH_ACCOUNT, Account, Instance, Store, StoreError


@dataclass
class LiveStatus:
    instance: Instance
    token_path: Path
    email: Optional[str]  # email in the live token
    matched: Optional[Account]  # stored account with that email
    recorded: Optional[str]  # account label recorded at last switch

    @property
    def drift(self) -> bool:
        """Live token doesn't match what we last switched to."""
        if self.recorded is None:
            return False
        return self.matched is None or self.matched.label != self.recorded


def live_status(store: Store, inst: Instance) -> LiveStatus:
    tok = store.instance_token(inst)
    email = tokens.token_email(tok)
    return LiveStatus(inst, tok, email, store.find_by_email(email), inst.account)


@dataclass
class SwitchPlan:
    instance: Instance
    target: Account
    live: LiveStatus
    to_stop: list[procs.Proc] = field(default_factory=list)
    unmarked: list[procs.Proc] = field(default_factory=list)


@dataclass
class SwitchResult:
    plan: SwitchPlan
    written_back: Optional[str] = None  # label whose stored token was refreshed from the live one
    orphan_backup: Optional[Path] = None  # live token of an unknown account, saved here
    killed: list[int] = field(default_factory=list)


def plan_switch(store: Store, instance: str, label: str) -> SwitchPlan:
    inst = store.get_instance(instance)
    if inst.auth != AUTH_ACCOUNT:
        raise StoreError(f"instance {inst.name!r} uses {inst.auth} auth; it has no account to switch")
    target = store.get_account(label)
    if not target.has_token or not target.email:
        raise StoreError(f"account {target.label!r} has no valid token; run `agy-pool login {target.label}`")
    live = live_status(store, inst)
    marked, unmarked = procs.scan(inst.name, store.instance_dir(inst))
    return SwitchPlan(inst, target, live, marked, unmarked)


def execute_switch(
    store: Store,
    plan: SwitchPlan,
    stop: Callable[[list[procs.Proc]], list[int]] = procs.stop,
) -> SwitchResult:
    res = SwitchResult(plan)
    inst = plan.instance

    # 1. Stop this instance's processes so none of them keeps (and later rewrites) the old token.
    if plan.to_stop:
        res.killed = stop(plan.to_stop)

    live_tok = store.instance_token(inst)
    store.instance_dir(inst).mkdir(parents=True, exist_ok=True)

    # 2. Write the live token back to its stored account (it's the freshest copy).
    if live_tok.is_file():
        email = tokens.token_email(live_tok)
        owner = store.find_by_email(email)
        if owner is not None:
            if not tokens.same_content(live_tok, owner.token):
                tokens.atomic_copy(live_tok, owner.token)
                res.written_back = owner.label
        else:
            res.orphan_backup = tokens.backup(live_tok, store.paths.backups, f"unknown-{email or 'unreadable'}")
        tokens.backup(live_tok, store.paths.backups, f"{inst.name}-before-switch")

    # 3. Atomically install the target account's token.
    tokens.atomic_copy(plan.target.token, live_tok)
    if tokens.token_email(live_tok) != plan.target.email:
        raise StoreError("post-switch verification failed: live token email mismatch")

    # 4. Record.
    inst.account = plan.target.label
    store.save_instance(inst)
    return res
