"""Azure Queue adapter for crawl and Azure AI Search indexing tasks."""
import json
import logging
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from services.config import settings
from services.crawl_state import CrawlState
from services.page_worker import PageWorker

log = logging.getLogger("sujeeth-web-search.crawl-worker")


class QueueProcessor:
    def __init__(self, ontology, storage, search, resolver):
        self.state = CrawlState()
        self.storage = storage
        self.search = search
        self.queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        self.worker = PageWorker(ontology, storage, search, resolver, self.state)

    def process(self, message):
        task = json.loads(message)
        task_type = task.get("task_type", "crawl_page")

        if task_type == "index_backfill_page":
            names, next_token = self.storage.list_blob_name_page(
                "documents/",
                continuation_token=task.get("continuation_token"),
                page_size=500,
            )
            batch_size = settings.index_backfill_batch_size
            queued = 0
            for start in range(0, len(names), batch_size):
                self.queue.send_message(json.dumps({
                    "task_type": "index_blob_batch",
                    "blob_names": names[start:start + batch_size],
                }))
                queued += 1
            if next_token:
                self.queue.send_message(json.dumps({
                    "task_type": "index_backfill_page",
                    "continuation_token": next_token,
                }))
            result = {
                "status": "backfill_page_queued",
                "blob_names_found": len(names),
                "index_batches_queued": queued,
                "more_pages": bool(next_token),
            }
        elif task_type == "index_blob_batch":
            names = task.get("blob_names") or []
            indexed = self.search.index_existing_blobs(names)
            result = {"status": "indexed", "blobs_read": len(names), "documents_indexed": indexed}
        else:
            result = self.worker.process(task)

        result = result or {"status": "no_result"}
        log.info("Queue disposition task_type=%s job_id=%s result=%s",
                 task_type, task.get("job_id"), result.get("status"))
        return {"job_id": task.get("job_id"), "task_type": task_type, **result}
