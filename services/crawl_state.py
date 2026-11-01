"""Persistent crawl job and page state backed by Azure Table Storage + Queue Storage."""
import hashlib, json, uuid
from datetime import datetime, timezone
from azure.data.tables import TableServiceClient, UpdateMode
from azure.core import MatchConditions
from azure.storage.queue import (
    QueueServiceClient,
    TextBase64EncodePolicy
)
from services.config import settings
from azure.core.exceptions import ResourceExistsError


def utcnow(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(value.encode('utf-8')).hexdigest()

class CrawlState:
    def __init__(self):
        if not settings.azure_storage_connection:
            raise RuntimeError('AzureWebJobsStorage is required for persistent crawl jobs.')
        self.tables = TableServiceClient.from_connection_string(settings.azure_storage_connection)
        self.jobs = self.tables.get_table_client(settings.crawl_jobs_table)
        self.pages = self.tables.get_table_client(settings.crawl_pages_table)
        self.manifest = self.tables.get_table_client(settings.crawl_manifest_table)
        for table in (self.jobs, self.pages, self.manifest):
            try:
                table.create_table()
            except ResourceExistsError:
                pass
        self.queues = QueueServiceClient.from_connection_string(settings.azure_storage_connection)
        self.queue = self.queues.get_queue_client(
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy()
        )
        try: self.queue.create_queue()
        except Exception: pass

    def create_job(self, domain, cap, depth, concepts):
        job_id = str(uuid.uuid4())
        entity = {'PartitionKey':'jobs','RowKey':job_id,'domain':domain,'status':'queued','created_utc':utcnow(),
                  'updated_utc':utcnow(),'pages_discovered':0,'pages_processed':0,'pages_skipped':0,
                  'pages_failed':0,'chunks_indexed':0,'chunks_tagged':0,'chunks_untagged':0,
                  'pending':0,'page_limit':cap,'max_depth':depth,'allowed_concepts_json':json.dumps(concepts)}
        self.jobs.create_entity(entity)
        return job_id

    def get_job(self, job_id):
        try:
            e=self.jobs.get_entity('jobs',job_id); e.pop('odata.etag',None)
            for k in ('allowed_concepts_json',):
                if k in e:
                    try: e[k.replace('_json','')]=json.loads(e[k])
                    except Exception: pass
            return e
        except Exception: return None

    def list_jobs(self, limit=100):
        rows=list(self.jobs.query_entities("PartitionKey eq 'jobs'"))
        rows.sort(key=lambda x:x.get('created_utc',''), reverse=True)
        return rows[:limit]

    def _change(self, job_id, **changes):
        # Table ETag retries protect counters from concurrent page workers.
        for _ in range(8):
            e=self.jobs.get_entity('jobs',job_id)
            for k,v in changes.items():
                if k.startswith('inc_'):
                    target=k[4:]; e[target]=int(e.get(target,0))+int(v)
                else: e[k]=v
            e['updated_utc']=utcnow()
            if 'status' not in changes and e.get('status')=='queued': e['status']='running'
            try:
                etag=e.metadata.get('etag','*') if hasattr(e,'metadata') else '*'
                self.jobs.update_entity(e, mode=UpdateMode.REPLACE, etag=etag, match_condition=MatchConditions.IfNotModified if etag!='*' else MatchConditions.Unconditionally)
                return e
            except Exception: continue
        raise RuntimeError('Could not update crawl job after concurrent updates')

    def _publish(self, job_id, domain, url, depth):
        payload = {"job_id": job_id, "domain": domain, "url": url, "depth": int(depth)}
        self.queue.send_message(json.dumps(payload))

    def enqueue_page(self, job_id, domain, url, depth):
        job = self.get_job(job_id)
        if not job or job.get("status") in ("cancelled", "failed", "completed", "completed_with_errors"):
            return False
        key = digest(url)
        try:
            self.pages.create_entity({"PartitionKey": job_id, "RowKey": key, "url": url,
                "depth": int(depth), "status": "queued", "attempts": 0, "created_utc": utcnow(),
                "updated_utc": utcnow()})
        except Exception:
            # Existing page is not necessarily queued; never duplicate its accounting.
            existing = self.page(job_id, url)
            if not existing or existing.get("status") in ("processing", "processed", "unchanged", "skipped", "failed"):
                return False
            if existing.get("status") != "queued":
                return False
            # A queued page can be republished during recovery; normal discovery avoids this path.
            self._publish(job_id, domain, url, depth)
            return True
        self._change(job_id, inc_pending=1, inc_pages_discovered=1)
        try:
            self._publish(job_id, domain, url, depth)
        except Exception as exc:
            self.mark_page(job_id, url, "publish_failed", error=str(exc)[:500])
            self._change(job_id, inc_pending=-1, inc_pages_discovered=-1)
            raise
        return True

    def enqueue_pages(self, job_id, domain, items):
        count = 0
        for url, depth in items:
            job = self.get_job(job_id)
            if not job or int(job.get("pages_discovered", 0)) >= int(job.get("page_limit", settings.max_pages)):
                break
            if self.enqueue_page(job_id, domain, url, depth):
                count += 1
        return count

    def page(self, job_id, url):
        try:
            entity = self.pages.get_entity(job_id, digest(url))
            entity.pop("odata.etag", None)
            return entity
        except Exception:
            return None

    def mark_page(self, job_id, url, status, **fields):
        # ETag-guarded update; retries avoid overwriting concurrent page transitions.
        for _ in range(5):
            entity = self.pages.get_entity(job_id, digest(url))
            entity["status"] = status
            entity["updated_utc"] = utcnow()
            entity.update(fields)
            try:
                etag = entity.metadata.get("etag", "*")
                self.pages.update_entity(entity, mode=UpdateMode.REPLACE, etag=etag,
                    match_condition=MatchConditions.IfNotModified)
                return True
            except Exception:
                continue
        return False

    def claim_page(self, job_id, url, lease_minutes=20):
        for _ in range(6):
            try:
                entity = self.pages.get_entity(job_id, digest(url))
            except Exception:
                return False
            status = entity.get("status")
            if status in ("processed", "unchanged", "skipped", "failed"):
                return False
            now = datetime.now(timezone.utc)
            lease_text = entity.get("lease_until_utc")
            if status == "processing" and lease_text:
                try:
                    if datetime.fromisoformat(lease_text) > now:
                        return False
                except ValueError:
                    pass
            token = uuid.uuid4().hex
            entity["status"] = "processing"
            entity["attempts"] = int(entity.get("attempts", 0)) + 1
            entity["lease_token"] = token
            entity["lease_until_utc"] = (now + __import__('datetime').timedelta(minutes=lease_minutes)).isoformat()
            entity["updated_utc"] = now.isoformat()
            try:
                self.pages.update_entity(entity, mode=UpdateMode.REPLACE,
                    etag=entity.metadata.get("etag", "*"), match_condition=MatchConditions.IfNotModified)
                return token
            except Exception:
                continue
        return None

    def release_page_claim(self, job_id, url, claim_token, error=None):
        """Release only the lease owned by this failed queue execution."""
        for _ in range(6):
            try:
                entity = self.pages.get_entity(job_id, digest(url))
            except Exception:
                return False
            if entity.get("status") != "processing" or entity.get("lease_token") != claim_token:
                return False
            entity["status"] = "queued"
            entity["updated_utc"] = utcnow()
            entity.pop("lease_until_utc", None)
            entity.pop("lease_token", None)
            if error:
                entity["last_error"] = str(error)[:500]
            try:
                self.pages.update_entity(entity, mode=UpdateMode.REPLACE,
                    etag=entity.metadata.get("etag", "*"), match_condition=MatchConditions.IfNotModified)
                return True
            except Exception:
                continue
        return False

    def complete_page(self, job_id, url, status="processed", page_fields=None, claim_token=None, **increments):
        # Page transition is the idempotency gate. Only the winner changes job counters.
        terminal = ("processed", "unchanged", "skipped", "failed")
        for _ in range(6):
            try:
                entity = self.pages.get_entity(job_id, digest(url))
            except Exception:
                return False
            if entity.get("status") in terminal:
                return False
            if claim_token and entity.get("lease_token") != claim_token:
                return False
            entity["status"] = status
            entity["updated_utc"] = utcnow()
            entity.pop("lease_until_utc", None)
            entity.pop("lease_token", None)
            if page_fields:
                entity.update(page_fields)
            try:
                self.pages.update_entity(entity, mode=UpdateMode.REPLACE,
                    etag=entity.metadata.get("etag", "*"), match_condition=MatchConditions.IfNotModified)
            except Exception:
                continue
            changes = {"inc_pending": -1}
            for key, value in increments.items():
                changes["inc_" + key] = value
            job = self._change(job_id, **changes)
            if int(job.get("pending", 0)) <= 0:
                final_status = "completed_with_errors" if int(job.get("pages_failed", 0)) else "completed"
                self._change(job_id, status=final_status, completed_utc=utcnow())
            return True
        raise RuntimeError("Could not complete page due to concurrent updates")

    def manifest_get(self, domain, url):
        try: return self.manifest.get_entity(digest(domain),digest(url))
        except Exception: return None

    def manifest_put(self, domain, url, **values):
        e={'PartitionKey':digest(domain),'RowKey':digest(url),'domain':domain,'url':url,'updated_utc':utcnow()}
        e.update(values)
        self.manifest.upsert_entity(e,mode=UpdateMode.MERGE)

    def manifest_for_domain(self, domain):
        return list(self.manifest.query_entities(f"PartitionKey eq '{digest(domain)}'"))

    def page_records(self, job_id):
        return list(self.pages.query_entities(f"PartitionKey eq '{job_id}'"))

    def cancel_job(self, job_id):
        job=self.get_job(job_id)
        if not job: return None
        return self._change(job_id,status='cancelled',cancelled_utc=utcnow())

    def fail_page(self, job_id, url, error):
        self.complete_page(job_id, url, "failed", pages_failed=1)
        self.mark_page(job_id, url, "failed", error=str(error)[:500])

    def reconcile_job(self, job_id):
        job = self.get_job(job_id)
        if not job:
            return None
        pages = self.page_records(job_id)
        counts = {"queued": 0, "processing": 0, "processed": 0, "unchanged": 0, "skipped": 0, "failed": 0, "publish_failed": 0}
        for page in pages:
            status = page.get("status", "queued")
            counts[status] = counts.get(status, 0) + 1
        pending = counts.get("queued", 0) + counts.get("processing", 0) + counts.get("publish_failed", 0)
        processed = counts.get("processed", 0) + counts.get("unchanged", 0)
        # Repair aggregate counters from the authoritative page table.
        self._change(job_id, pending=pending, pages_discovered=len(pages), pages_processed=processed,
                     pages_skipped=counts.get("skipped", 0), pages_failed=counts.get("failed", 0))
        if pending == 0 and job.get("status") not in ("cancelled", "failed"):
            final_status = "completed_with_errors" if counts.get("failed", 0) else "completed"
            self._change(job_id, status=final_status, completed_utc=utcnow())
        return {"job_id": job_id, "page_count": len(pages), "page_states": counts,
                "expected_pending": pending, "previous_pending": int(job.get("pending", 0))}

    def _requeue_if_stale(self, job_id, page, now, stale_before):
        """Atomically change only a still-stale page to queued."""
        url = page.get("url")
        if not url:
            return False
        for _ in range(6):
            try:
                entity = self.pages.get_entity(job_id, digest(url))
            except Exception:
                return False
            status = entity.get("status", "queued")
            # Explicit recovery may retry terminal failures as well as stale queue work.
            eligible = status == "failed"
            if status in ("queued", "publish_failed"):
                eligible = True
                try:
                    stamp = entity.get("updated_utc") or entity.get("created_utc")
                    eligible = datetime.fromisoformat(stamp) < stale_before
                except Exception:
                    eligible = True
            elif status == "processing":
                lease = entity.get("lease_until_utc")
                try:
                    eligible = not lease or datetime.fromisoformat(lease) < now
                except Exception:
                    eligible = True
            else:
                eligible = False
            if not eligible:
                return False
            entity["status"] = "queued"
            entity["updated_utc"] = utcnow()
            entity.pop("lease_until_utc", None)
            entity.pop("lease_token", None)
            entity["recovery_utc"] = utcnow()
            try:
                self.pages.update_entity(entity, mode=UpdateMode.REPLACE,
                    etag=entity.metadata.get("etag", "*"), match_condition=MatchConditions.IfNotModified)
                return True
            except Exception:
                continue
        return False

    def recover_job(self, job_id, stale_minutes=25):
        job = self.get_job(job_id)
        if not job:
            return None
        if job.get("status") in ("cancelled", "failed"):
            return {"job_id": job_id, "status": job.get("status"), "requeued": 0, "message": "Job is not recoverable."}
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        stale_before = now - timedelta(minutes=max(1, int(stale_minutes)))
        requeued = []
        publish_errors = []
        for page in self.page_records(job_id):
            url = page.get("url")
            if not url or not self._requeue_if_stale(job_id, page, now, stale_before):
                continue
            try:
                self._publish(job_id, job["domain"], url, int(page.get("depth", 0)))
                requeued.append(url)
            except Exception as exc:
                self.mark_page(job_id, url, "publish_failed", error=str(exc)[:500])
                publish_errors.append({"url": url, "error": str(exc)[:300]})
        reconciliation = self.reconcile_job(job_id)
        return {"job_id": job_id, "status": "recovery_submitted", "requeued": len(requeued),
                "urls": requeued, "publish_errors": publish_errors, "reconciliation": reconciliation}

    def resume_job(self, job_id):
        return self.recover_job(job_id)
