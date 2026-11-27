from pathlib import Path

from services import storage_service
from services.config import settings


def test_a_data_folder_that_cannot_be_made_does_not_stop_startup(monkeypatch, tmp_path):
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if str(self) == "data":
            raise PermissionError(13, "Permission denied", "data")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(settings, "local_data_dir", "data", raising=False)
    monkeypatch.setattr(storage_service.tempfile, "gettempdir", lambda: str(tmp_path))
    store = storage_service.StorageService()
    assert store.directory == tmp_path / "websearch-data"
    store.put("x", {"a": 1})
    assert store.get("x") == {"a": 1}
