
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from services.config import settings

try:
    from azure.storage.blob import BlobServiceClient
except ImportError:
    BlobServiceClient = None


class StorageService:
    def __init__(self):
        self.directory = Path(settings.local_data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.container = None
        if settings.blob_connection_string and BlobServiceClient:
            service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
            self.container = service.get_container_client(settings.blob_container)
            try:
                self.container.create_container()
            except Exception:
                pass
        self.mode = "azure_blob" if self.container else "local_filesystem"

    def _path(self, name):
        return self.directory / (hashlib.sha256(name.encode()).hexdigest() + ".json")

    def put(self, name, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self.container:
            self.container.upload_blob(name, raw, overwrite=True, content_type="application/json")
        else:
            self._path(name).write_bytes(raw)

    def get(self, name):
        try:
            raw = (
                self.container.download_blob(name).readall()
                if self.container else self._path(name).read_bytes()
            )
            return json.loads(raw)
        except Exception:
            return None

    def list(self, prefix):
        if self.container:
            for blob in self.container.list_blobs(name_starts_with=prefix):
                try:
                    yield json.loads(self.container.download_blob(blob.name).readall())
                except Exception:
                    continue
        else:
            for path in self.directory.glob("*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if item.get("_key", "").startswith(prefix):
                        yield item
                except Exception:
                    continue

    def status(self):
        documents = list(self.list("documents/"))
        return {
            "storage_mode": self.mode,
            "document_count": len(documents),
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }

    def delete(self, name):
        if self.container:
            try: self.container.delete_blob(name)
            except Exception: pass
        else:
            try: self._path(name).unlink()
            except FileNotFoundError: pass
