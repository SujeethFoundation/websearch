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
            raw = (self.container.download_blob(name).readall()
                   if self.container else self._path(name).read_bytes())
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

    def list_blob_name_page(self, prefix, continuation_token=None, page_size=250):
        """Return blob names only, without downloading blob contents."""
        if not self.container:
            names = []
            for path in self.directory.glob("*.json"):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    name = item.get("_key", "")
                    if name.startswith(prefix):
                        names.append(name)
                except Exception:
                    continue
            names.sort()
            start = int(continuation_token or 0)
            page = names[start:start + page_size]
            next_token = str(start + len(page)) if start + len(page) < len(names) else None
            return page, next_token
        # results_per_page belongs to list_blobs(); by_page() only accepts the continuation token.
        pager = self.container.list_blobs(
            name_starts_with=prefix, results_per_page=page_size
        ).by_page(continuation_token=continuation_token)
        try:
            page = next(pager)
        except StopIteration:
            return [], None
        names = [blob.name for blob in page]
        return names, pager.continuation_token

    def status(self):
        if self.container:
            count = sum(1 for _ in self.container.list_blobs(name_starts_with="documents/"))
        else:
            count = sum(1 for _ in self.list("documents/"))
        return {
            "storage_mode": self.mode,
            "document_count": count,
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        }

    def delete(self, name):
        if self.container:
            try:
                self.container.delete_blob(name)
            except Exception:
                pass
        else:
            try:
                self._path(name).unlink()
            except FileNotFoundError:
                pass