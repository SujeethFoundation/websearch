"""Azure Queue adapter for crawl and Azure AI Search indexing tasks."""
import json
import os
from urllib.parse import urlparse
import logging
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from services.config import settings
from services.crawl_state import CrawlState
from services.page_worker import PageWorker
from services.path_rules import is_excluded_url
from services.search_service import IndexBusyError

log = logging.getLogger("sujeeth-web-search.crawl-worker")

# When the search index is busy, indexing is retried later: after 2, 5, 10, 20, 30 minutes, then hourly,
# up to INDEX_MAX_DEFERRALS times. After that the task fails normally and POST /api/index/backfill
# can pick the chunks up again from Blob Storage (their vectors are stored, so nothing is re-embedded).
DEFERRAL_DELAYS = (120, 300, 600, 1200, 1800, 3600)
MAX_INDEX_DEFERRALS = int(os.getenv("INDEX_MAX_DEFERRALS", "12") or 12)


class QueueProcessor:
    def __init__(self, ontology, storage, search, resolver):
        self.state = CrawlState()
        self.storage = storage
        self.search = search
        self.ontology = ontology
        self.resolver = resolver
        self.queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        self.worker = PageWorker(ontology, storage, search, resolver, self.state)
        self.worker.defer_indexing = self._defer_index_batch

    def _retag_changes(self, names, domains=None, collect_examples=0):
        """Compare stored tags with what the current ontology would assign.

        Returns (changes, stats, examples). Each change is (blob_name, doc, new_concepts).
        """
        only = {str(x).lower() for x in (domains or []) if str(x).strip()}
        allowed_by_domain = {}
        changes, examples = [], []
        stats = {"read": 0, "checked": 0, "unchanged": 0, "skipped": 0}
        for name in names:
            doc = self.storage.get(name)
            if not doc or not doc.get("id"):
                continue
            stats["read"] += 1
            domain = str(doc.get("domain") or "").lower()
            if only and domain not in only:
                stats["skipped"] += 1
                continue
            if domain not in allowed_by_domain:
                allowed_by_domain[domain] = self.ontology.domain_concepts(domain)
            allowed = allowed_by_domain[domain]
            if not allowed:
                stats["skipped"] += 1
                continue
            stats["checked"] += 1
            new_concepts = self.resolver.tag_document(doc.get("title", ""), doc.get("content", ""), allowed)
            if list(new_concepts) == list(doc.get("concepts") or []):
                stats["unchanged"] += 1
                continue
            changes.append((name, doc, new_concepts))
            if len(examples) < collect_examples:
                examples.append({
                    "title": doc.get("title"), "url": doc.get("url"),
                    "before": doc.get("concepts") or [], "after": new_concepts,
                })
        return changes, stats, examples

    def retag_blobs(self, names, domains=None):
        """Re-tag stored documents with the current ontology, then merge tags into the index."""
        changes, stats, _ = self._retag_changes(names, domains)
        updates = []
        for name, doc, new_concepts in changes:
            doc["concepts"] = new_concepts
            self.storage.put(name, doc)  # Blob Storage stays the source of truth
            updates.append((doc["id"], new_concepts))
        indexed = not_indexed = 0
        if updates:
            indexed, not_indexed = self.search.update_concepts(updates)
        log.info("Retag batch read=%s checked=%s changed=%s indexed=%s not_indexed=%s",
                 stats["read"], stats["checked"], len(changes), indexed, not_indexed)
        return {"status": "retagged", **stats, "changed": len(changes),
                "index_updated": indexed, "not_yet_indexed": not_indexed}

    def retag_preview(self, domains=None, scan_limit=1000, examples=5):
        """Read-only sample: how many stored documents would change. Writes nothing."""
        names, _ = self.storage.list_blob_name_page("documents/", page_size=max(1, min(int(scan_limit), 1000)))
        changes, stats, sample = self._retag_changes(names, domains, collect_examples=examples)
        return {"status": "preview", "blob_names_scanned": len(names), **stats,
                "would_change": len(changes), "examples": sample}

    def _defer_index_batch(self, names, attempt=0):
        """Queue a delayed indexing task for chunks that are stored but not yet in the index."""
        delay = DEFERRAL_DELAYS[min(int(attempt), len(DEFERRAL_DELAYS) - 1)]
        names = list(names)
        # Queue messages are limited in size, so a page with very many chunks is sent in groups.
        for start in range(0, len(names), 100):
            self.queue.send_message(
                json.dumps({"task_type": "index_blob_batch", "blob_names": names[start:start + 100], "attempt": int(attempt)}),
                visibility_timeout=delay,
            )
        return delay

    def _purge_matches(self, names, domains=None):
        """Find stored chunks whose page is under an excluded path for its domain."""
        only = {str(x).lower() for x in (domains or []) if str(x).strip()}
        rules_by_domain = self.worker.path_policy.all_rules()
        matches = []
        stats = {"read": 0}
        for name in names:
            doc = self.storage.get(name)
            if not doc or not doc.get("id"):
                continue
            stats["read"] += 1
            domain = str(doc.get("domain") or "").lower()
            if only and domain not in only:
                continue
            rules = rules_by_domain.get(domain)
            if rules and is_excluded_url(doc.get("url"), rules):
                matches.append((name, doc))
        return matches, stats

    def purge_blobs(self, names, domains=None):
        """Delete stored chunks under excluded paths: index entries, blobs, and manifest rows."""
        matches, stats = self._purge_matches(names, domains)
        ids = [doc["id"] for _, doc in matches]
        # Index first. If that fails the blobs are still there and the retry finds them again.
        index_deleted = self.search.delete_ids(ids) if ids else 0
        for name, doc in matches:
            self.storage.delete(name)
            self.storage.delete(self.search.vector_key(doc["id"]))
            self.state.manifest_delete(str(doc.get("domain") or "").lower(), doc.get("url"))
        log.info("Purge batch read=%s removed=%s index_deleted=%s", stats["read"], len(matches), index_deleted)
        return {"status": "purged", **stats, "removed": len(matches), "index_deleted": index_deleted}

    def purge_preview(self, domains=None, scan_limit=1000, examples=5):
        """Read-only sample of how many stored chunks a purge would remove. Writes nothing."""
        names, _ = self.storage.list_blob_name_page("documents/", page_size=max(1, min(int(scan_limit), 1000)))
        matches, stats = self._purge_matches(names, domains)
        by_section = {}
        for _, doc in matches:
            section = (urlparse(str(doc.get("url") or "")).path.strip("/").split("/") or [""])[0]
            key = f"{str(doc.get('domain') or '').lower()}/{section}"
            by_section[key] = by_section.get(key, 0) + 1
        return {
            "status": "preview", "blob_names_scanned": len(names), **stats,
            "would_remove": len(matches), "by_section": by_section,
            "examples": [doc.get("url") for _, doc in matches[:examples]],
            "rules": {d: list(r) for d, r in self.worker.path_policy.all_rules().items()},
        }

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
        elif task_type == "index_retag_page":
            domains = task.get("domains") or []
            names, next_token = self.storage.list_blob_name_page(
                "documents/",
                continuation_token=task.get("continuation_token"),
                page_size=500,
            )
            queued = 0
            for start in range(0, len(names), 25):
                self.queue.send_message(json.dumps({
                    "task_type": "index_retag_batch",
                    "blob_names": names[start:start + 25],
                    "domains": domains,
                }))
                queued += 1
            if next_token:
                self.queue.send_message(json.dumps({
                    "task_type": "index_retag_page",
                    "continuation_token": next_token,
                    "domains": domains,
                }))
            result = {
                "status": "retag_page_queued",
                "blob_names_found": len(names),
                "retag_batches_queued": queued,
                "more_pages": bool(next_token),
            }
        elif task_type == "index_retag_batch":
            result = self.retag_blobs(task.get("blob_names") or [], task.get("domains") or [])
        elif task_type == "index_purge_page":
            domains = task.get("domains") or []
            names, next_token = self.storage.list_blob_name_page(
                "documents/",
                continuation_token=task.get("continuation_token"),
                page_size=500,
            )
            queued = 0
            for start in range(0, len(names), 25):
                self.queue.send_message(json.dumps({
                    "task_type": "index_purge_batch",
                    "blob_names": names[start:start + 25],
                    "domains": domains,
                }))
                queued += 1
            if next_token:
                self.queue.send_message(json.dumps({
                    "task_type": "index_purge_page",
                    "continuation_token": next_token,
                    "domains": domains,
                }))
            result = {
                "status": "purge_page_queued",
                "blob_names_found": len(names),
                "purge_batches_queued": queued,
                "more_pages": bool(next_token),
            }
        elif task_type == "index_purge_batch":
            names = task.get("blob_names") or []
            domains = task.get("domains") or []
            attempt = int(task.get("attempt", 0))
            try:
                result = self.purge_blobs(names, domains)
            except IndexBusyError:
                # The index delete runs before any blob is removed, so nothing was lost: try again later.
                if attempt >= MAX_INDEX_DEFERRALS:
                    raise
                delay = DEFERRAL_DELAYS[min(attempt, len(DEFERRAL_DELAYS) - 1)]
                self.queue.send_message(
                    json.dumps({"task_type": "index_purge_batch", "blob_names": names, "domains": domains, "attempt": attempt + 1}),
                    visibility_timeout=delay,
                )
                result = {"status": "index_busy_deferred", "blobs": len(names), "attempt": attempt + 1, "retry_in_seconds": delay}
        elif task_type == "index_blob_batch":
            names = task.get("blob_names") or []
            attempt = int(task.get("attempt", 0))
            try:
                indexed = self.search.index_existing_blobs(names)
                result = {"status": "indexed", "blobs_read": len(names), "documents_indexed": indexed}
            except IndexBusyError:
                if attempt >= MAX_INDEX_DEFERRALS:
                    raise
                delay = self._defer_index_batch(names, attempt + 1)
                result = {"status": "index_busy_deferred", "blobs": len(names), "attempt": attempt + 1, "retry_in_seconds": delay}
        else:
            result = self.worker.process(task)

        result = result or {"status": "no_result"}
        log.info("Queue disposition task_type=%s job_id=%s result=%s",
                 task_type, task.get("job_id"), result.get("status"))
        return {"job_id": task.get("job_id"), "task_type": task_type, **result}
