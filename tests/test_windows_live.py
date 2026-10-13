"""Windows: запрет сна; переключение тестового/боевого режима одной командой."""
import shutil
import tempfile
from pathlib import Path

from app.main import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, keep_awake
from app.setup_wizard import parse_env
from app.storage import Storage
from app.tools import set_mode


class FakeKernel32:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):  # noqa: N802 - имя из WinAPI
        self.calls.append(flags)
        return 1


def test_windows_keep_awake_only_on_windows():
    kernel = FakeKernel32()
    assert keep_awake("win32", kernel) is True
    assert kernel.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert keep_awake("darwin", kernel) is False and len(kernel.calls) == 1


def test_windows_keep_awake_failure_is_not_fatal():
    class Broken:
        def SetThreadExecutionState(self, flags):  # noqa: N802
            raise OSError("no")

    assert keep_awake("win32", Broken()) is False


def project_with_env(dry="true") -> Path:
    root = Path(tempfile.mkdtemp())
    text = (Path(".env.example").read_text(encoding="utf-8")).replace("DRY_RUN=true", f"DRY_RUN={dry}")
    (root / ".env").write_text(text, encoding="utf-8")
    (root / "data").mkdir()
    from datetime import datetime, timezone
    storage = Storage(str(root / "data" / "state.db"))
    now = datetime.now(timezone.utc)
    storage.claim_card(1, "t", "u", now, now, now)
    storage.remember_chat(["username:curator_amina"], 555, now, 1, "Амина")
    storage.close()
    return root


def test_go_live_switches_dry_run_and_wipes_test_state():
    root = project_with_env()
    lines = set_mode(root, live=True)
    env = parse_env((root / ".env").read_text(encoding="utf-8"))
    storage = Storage(str(root / "data" / "state.db"))
    assert env["DRY_RUN"] == "false" and not storage.has_card(1)
    assert storage.find_chat("username:curator_amina")["chat_id"] == 555  # F2: чаты не забываем
    storage.close()
    assert "Боевой режим" in lines[0]
    assert "# Как сервис узнаёт" in (root / ".env").read_text(encoding="utf-8")  # комментарии целы
    shutil.rmtree(root)


def test_go_live_back_to_test_mode_keeps_state():
    root = project_with_env(dry="false")
    set_mode(root, live=False)
    assert parse_env((root / ".env").read_text(encoding="utf-8"))["DRY_RUN"] == "true"
    assert (root / "data" / "state.db").exists()
    shutil.rmtree(root)


def test_go_live_without_env_explains_what_to_do():
    root = Path(tempfile.mkdtemp())
    assert "start" in set_mode(root, live=True)[0]
