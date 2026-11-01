"""Azure Queue adapter for persistent page-level crawl jobs."""
import json
import logging

from services.crawl_state import CrawlState
from services.page_worker import PageWorker

log = logging.getLogger("sujeeth-web-search.crawl-worker")


class QueueProcessor:
    def __init__(self, ontology, storage, search, resolver):
        self.state = CrawlState()
        self.worker = PageWorker(ontology, storage, search, resolver, self.state)

    def process(self, message):
        task = json.loads(message)
        result = self.worker.process(task)
        # Return the worker's actual disposition. In particular, do not label a
        # duplicate/already-claimed message as a successfully processed page.
        result = result or {"status": "no_result"}
        log.info(
            "Crawl queue disposition job_id=%s url=%s result=%s",
            task.get("job_id"), task.get("url"), result.get("status")
        )
        return {
            "job_id": task.get("job_id"),
            "url": task.get("url"),
            **result,
        }
