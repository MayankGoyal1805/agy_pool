import stat

import pytest
from typer.testing import CliRunner

from agy_pool import launchers, procs, switching, tokens, usage
from agy_pool.cli import app
from agy_pool.store import AUTH_API_KEY, Instance, StoreError

from .conftest import write_token


def test_atomic_copy_mode_and_content(tmp_path):
    src = write_token(tmp_path / "a", "a@x.com")
    dst = tmp_path / "d" / "tok"
    tokens.atomic_copy(src, dst)
    assert tokens.token_email(dst) == "a@x.com"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert [p.name for p in dst.parent.iterdir()] == ["tok"]  # no temp leftovers


def test_token_email_bad_files(tmp_path):
    assert tokens.token_email(tmp_path / "missing") is None
    (tmp_path / "junk").write_text("not json")
    assert tokens.token_email(tmp_path / "junk") is None


def test_import_and_duplicates(env, tmp_path):
    src = write_token(tmp_path / "src" / "antigravity-oauth-token", "a@x.com")
    acc = env.import_token("main", src)
    assert acc.email == "a@x.com"
    with pytest.raises(StoreError, match="already stored"):
        env.import_token("other", src)
    with pytest.raises(StoreError, match="already exists"):
        env.import_token("main", src)
    with pytest.raises(StoreError, match="invalid"):
        env.import_token("Bad Label!", src)


def test_default_instance_present(env):
    (inst,) = env.instances()
    assert inst.name == "flash" and inst.app_dir == "antigravity-cli"


def _setup_two(env, tmp_path):
    a = env.import_token("a", write_token(tmp_path / "ta", "a@x.com", "A1"))
    b = env.import_token("b", write_token(tmp_path / "tb", "b@x.com", "B1"))
    return a, b


def test_switch_writes_back_and_installs(env, tmp_path, live_token):
    a, b = _setup_two(env, tmp_path)
    write_token(live_token, "a@x.com", "A2-refreshed")  # live copy is fresher than stored
    plan = switching.plan_switch(env, "flash", "b")
    res = switching.execute_switch(env, plan, stop=lambda p: [])
    assert res.written_back == "a"
    assert "A2-refreshed" in a.token.read_text()
    assert tokens.token_email(live_token) == "b@x.com"
    assert env.get_instance("flash").account == "b"
    assert not switching.live_status(env, env.get_instance("flash")).drift


def test_switch_unknown_live_token_is_backed_up(env, tmp_path, live_token):
    _setup_two(env, tmp_path)
    write_token(live_token, "stranger@x.com")
    res = switching.execute_switch(env, switching.plan_switch(env, "flash", "a"), stop=lambda p: [])
    assert res.orphan_backup and tokens.token_email(res.orphan_backup) == "stranger@x.com"
    assert tokens.token_email(live_token) == "a@x.com"


def test_switch_from_logged_out(env, tmp_path, live_token):
    _setup_two(env, tmp_path)
    switching.execute_switch(env, switching.plan_switch(env, "flash", "b"), stop=lambda p: [])
    assert tokens.token_email(live_token) == "b@x.com"


def test_switch_stops_only_planned_processes(env, tmp_path, live_token, monkeypatch):
    _setup_two(env, tmp_path)
    write_token(live_token, "a@x.com")
    marked = [procs.Proc(111, "agy acp", "/x/agy", 5)]
    unmarked = [procs.Proc(222, "agy", "/x/agy", 5)]
    monkeypatch.setattr(procs, "scan", lambda name, d: (marked, unmarked))
    stopped = []
    plan = switching.plan_switch(env, "flash", "b")
    switching.execute_switch(env, plan, stop=lambda p: stopped.extend(x.pid for x in p) or [])
    assert stopped == [111]


def test_drift_detected(env, tmp_path, live_token):
    _setup_two(env, tmp_path)
    switching.execute_switch(env, switching.plan_switch(env, "flash", "b"), stop=lambda p: [])
    write_token(live_token, "a@x.com")  # some stale process rewrote it
    assert switching.live_status(env, env.get_instance("flash")).drift


def test_cannot_switch_api_key_instance(env, tmp_path):
    _setup_two(env, tmp_path)
    env.save_instance(Instance("argon", "antigravity-cli", AUTH_API_KEY))
    with pytest.raises(StoreError, match="api-key"):
        switching.plan_switch(env, "argon", "a")


def test_remove_active_account_refused(env, tmp_path):
    _setup_two(env, tmp_path)
    switching.execute_switch(env, switching.plan_switch(env, "flash", "a"), stop=lambda p: [])
    with pytest.raises(StoreError, match="active"):
        env.remove_account("a")
    env.remove_account("b")
    assert [a.label for a in env.accounts()] == ["a"]


def test_rename_updates_instance(env, tmp_path):
    _setup_two(env, tmp_path)
    switching.execute_switch(env, switching.plan_switch(env, "flash", "a"), stop=lambda p: [])
    env.rename_account("a", "main")
    assert env.get_instance("flash").account == "main"
    assert env.get_account("main").email == "a@x.com"
    with pytest.raises(StoreError):
        env.rename_account("main", "b")


def test_launcher_render(env):
    flash = env.get_instance("flash")
    text = launchers.render(env, flash, "/opt/agy")
    assert "export AGY_POOL_INSTANCE=flash" in text
    assert "exec /opt/agy --app_data_dir=antigravity-cli \"$@\"" in text
    argon = Instance("argon", "antigravity-cli", AUTH_API_KEY, base_url="http://h:2048")
    text = launchers.render(env, argon, "/opt/agy")
    assert "GEMINI_API_KEY" in text and "unset GOOGLE_API_KEY" in text
    assert "AGY_POOL_INSTANCE=argon" in text
    assert "export GOOGLE_GEMINI_BASE_URL=http://h:2048" in text
    assert "acp --cli_dir_name=argon" in text
    assert "--settings '{\"modelProvider\":\"gemini\"}'" in text


def test_launcher_argv_dispatch(env, tmp_path):
    """Run the rendered argon launcher with a fake agy that echoes its argv + env."""
    import os, subprocess
    fake = tmp_path / "fakeagy"
    fake.write_text('#!/bin/sh\necho "$GEMINI_API_KEY|$GOOGLE_GEMINI_BASE_URL|$AGY_POOL_INSTANCE|$*"\n')
    fake.chmod(0o755)
    argon = Instance("argon", "antigravity-cli", AUTH_API_KEY, base_url="http://h:2048")
    key = env.api_key_file(argon)
    key.parent.mkdir(parents=True)
    key.write_text("K1")
    sh = tmp_path / "agy-argon"
    sh.write_text(launchers.render(env, argon, str(fake)))
    sh.chmod(0o755)
    run = lambda *a: subprocess.run([str(sh), *a], capture_output=True, text=True,
                                    env={**os.environ, "GOOGLE_API_KEY": "x"}).stdout.strip()
    assert run("acp", "--foo") == "K1|http://h:2048|argon|--app_data_dir=antigravity-cli acp --cli_dir_name=argon --foo"
    assert run("-p", "hi") == 'K1|http://h:2048|argon|--app_data_dir=antigravity-cli --settings {"modelProvider":"gemini"} -p hi'


def test_seed_cli_settings(env):
    import json
    shared = env.paths.gemini_root / "antigravity-cli" / "settings.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"model": "M", "permissions": {"allow": ["x"]}}))
    argon = Instance("argon", "antigravity-cli", AUTH_API_KEY)
    out = env.seed_cli_settings(argon)
    assert out == env.paths.gemini_root / "antigravity-cli" / "argon" / "settings.json"
    assert json.loads(out.read_text()) == {"model": "M", "permissions": {"allow": ["x"]}, "modelProvider": "gemini"}
    assert "modelProvider" not in json.loads(shared.read_text())


def test_launcher_refuses_foreign_file(env):
    flash = env.get_instance("flash")
    path = launchers.launcher_path(env.paths, flash)
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError):
        launchers.write(env, flash)
    launchers.write(env, flash, force=True)
    assert "Managed by agy-pool" in path.read_text()


def test_usage_parse():
    raw = "Gemini Models\tFive hour limit\t86%\t2026-11-05T17:05:00Z\nGemini Models\tWeekly limit\t50%\njunk line\n"
    lims = usage.parse(raw)
    assert [(l.name, l.pct) for l in lims] == [("Five hour limit", 86), ("Weekly limit", 50)]
    assert lims[0].reset is not None and lims[1].reset is None


def test_cli_flow(env, tmp_path, live_token):
    r = CliRunner()
    write_token(tmp_path / "s1" / "antigravity-oauth-token", "a@x.com")
    write_token(tmp_path / "s2" / "antigravity-oauth-token", "b@x.com")
    write_token(live_token, "a@x.com")
    assert r.invoke(app, ["import", "a", "--from", str(tmp_path / "s1")]).exit_code == 0
    assert r.invoke(app, ["import", "b", "--from", str(tmp_path / "s2")]).exit_code == 0
    out = r.invoke(app, ["list"]).output
    assert "a@x.com" in out and "flash" in out
    res = r.invoke(app, ["switch", "b", "--yes"])
    assert res.exit_code == 0, res.output
    assert tokens.token_email(live_token) == "b@x.com"
    assert "b@x.com" in r.invoke(app, ["current"]).output
    res = r.invoke(app, ["instance", "create", "argon", "--api-key", "--base-url", "http://h:2048/"], input="KEY123\n")
    assert res.exit_code == 0, res.output
    key = env.paths.secrets / "argon.key"
    assert key.read_text() == "KEY123" and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert (env.paths.bin_dir / "agy-argon").exists()
    assert env.get_instance("argon").base_url == "http://h:2048"
    assert "http://h:2048" in r.invoke(app, ["instance", "list"]).output
    assert r.invoke(app, ["switch", "a", "-i", "argon"]).exit_code == 1


def test_accounts_addressable_by_position(env, tmp_path):
    import pytest
    from agy_pool.store import StoreError
    for lbl in ("acc2", "acc10", "acc1"):
        d = env.account(lbl).dir
        d.mkdir(parents=True)
    assert [a.label for a in env.accounts()] == ["acc1", "acc2", "acc10"]  # natural order
    assert env.get_account("2").label == "acc2"
    assert env.get_account("3").label == "acc10"
    assert env.number(env.get_account("acc10")) == 3
    # removing from the middle renumbers
    env.remove_account("acc2")
    assert env.get_account("2").label == "acc10"
    with pytest.raises(StoreError, match="no account #3"):
        env.get_account("3")
    with pytest.raises(StoreError, match="reserved"):
        env.account("7")


def test_remaining_countdown_is_precise():
    from agy_pool.cli import _remaining
    assert _remaining(6 * 86400 + 3 * 3600 + 59) == "6d 3h"
    assert _remaining(4 * 3600 + 12 * 60 + 5) == "4h 12m"
    assert _remaining(3600) == "1h 0m"
    assert _remaining(35 * 60 + 9) == "35m"
    assert _remaining(40) == "40s"
    assert _remaining(-5) == "0s"
