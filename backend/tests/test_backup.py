"""备份防护网测试（M1 做实，2026-08-26）。

不发起真实 pg_dump：monkeypatch 注入点 _spawn，验证命令参数、
密码走环境变量、产物落盘、滚动保留。解析用例直接用测试库连接串。
"""
from __future__ import annotations

import pytest

from app.services import backup
from app.settings import settings


class _FakeProc:
    def __init__(self, payload: bytes = b"FAKEDUMP"):
        self.returncode = 0
        self._payload = payload

    async def communicate(self):
        return self._payload, b""


def test_conn_parts_parses_test_url():
    conn = backup._conn_parts(settings.database_url_test)
    assert conn["user"] == "prof_graph"
    assert conn["host"] == "127.0.0.1"
    assert conn["port"].isdigit()
    assert conn["db"] == "prof_graph_test"


def test_conn_parts_rejects_garbage():
    with pytest.raises(ValueError):
        backup._conn_parts("mysql://nope")


async def test_run_backup_invokes_pg_dump_and_prunes(db_session, monkeypatch, tmp_path):
    """命令参数正确（密码只走环境变量）+ 产物落盘 + 滚动保留 7 份。"""
    calls: list[tuple] = []

    async def fake_spawn(*args, stdout=None, stderr=None, env=None):
        calls.append((args, env))
        return _FakeProc()

    monkeypatch.setattr(backup, "_spawn", fake_spawn)
    monkeypatch.setattr(settings, "backup_dir", tmp_path)
    fake_exe = tmp_path / "pg_dump.exe"
    fake_exe.write_bytes(b"")  # 真实存在的假可执行文件
    monkeypatch.setattr(settings, "pg_dump_path", str(fake_exe))

    # 预置 7 份旧备份（本次完成后共 8 份，最旧的应被清掉）
    for i in range(7):
        (tmp_path / f"prof_graph_2026082{i % 10}_0{i}000.dump").write_bytes(b"old")

    dest = await backup.run_backup()

    args, env = calls[0]
    assert args[0].endswith("pg_dump.exe") and "-Fc" in args
    assert "prof_graph_test" not in " ".join(args)  # 备份的是主库而非测试库
    assert "PGPASSWORD" in env  # 密码经环境变量传递
    assert dest.exists() and dest.read_bytes() == b"FAKEDUMP"
    remaining = sorted(tmp_path.glob("prof_graph_*.dump"))
    assert len(remaining) == backup.RETENTION  # 旧的 7 - 1 + 新 1 = 7
    assert dest in remaining


async def test_run_backup_failure_raises_and_cleans(db_session, monkeypatch, tmp_path):
    class _FailProc(_FakeProc):
        def __init__(self):
            super().__init__(b"")
            self.returncode = 1

    async def fake_spawn(*args, **kwargs):
        return _FailProc()

    monkeypatch.setattr(backup, "_spawn", fake_spawn)
    monkeypatch.setattr(settings, "backup_dir", tmp_path)
    fake_exe = tmp_path / "pg_dump.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(settings, "pg_dump_path", str(fake_exe))

    with pytest.raises(RuntimeError, match="pg_dump 失败"):
        await backup.run_backup()
    assert list(tmp_path.glob("*.dump")) == []  # 失败不留半截文件
