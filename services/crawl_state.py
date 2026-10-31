"""Persistent crawl job and page state backed by Azure Table Storage + Queue Storage."""
import hashlib, json, uuid
from datetime import datetime, timezone
from azure.data.tables import TableServiceClient, UpdateMode, MatchConditions
from azure.storage.queue import QueueServiceClient
from services.config import settings


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
            try: table.create_table()
            except Exception: pass
        self.queues = QueueServiceClient.from_connection_string(settings.azure_storage_connection)
        self.queue = self.queues.get_queue_client(settings.crawl_queue)
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

    def enqueue_page(self, job_id, domain, url, depth):
        job=self.get_job(job_id)
        if not job or job.get('status') in ('cancelled','failed','completed'):
            return False
        if int(job.get('pages_discovered',0)) >= int(job.get('page_limit',settings.max_pages)):
            return False
        key=digest(url)
        entity={'PartitionKey':job_id,'RowKey':key,'url':url,'depth':int(depth),'status':'queued','created_utc':utcnow()}
        try: self.pages.create_entity(entity)
        except Exception: return False
        self._change(job_id, inc_pending=1, inc_pages_discovered=1)
        self.queue.send_message(json.dumps({'job_id':job_id,'domain':domain,'url':url,'depth':int(depth)}))
        return True

    def enqueue_pages(self, job_id, domain, items):
        """Register a whole seed set before publishing any queue messages."""
        created=[]
        job=self.get_job(job_id)
        if not job: return 0
        cap=int(job.get('page_limit',settings.max_pages))
        remaining=max(0,cap-int(job.get('pages_discovered',0)))
        for url,depth in items:
            if len(created)>=remaining: break
            key=digest(url)
            entity={'PartitionKey':job_id,'RowKey':key,'url':url,'depth':int(depth),'status':'queued','created_utc':utcnow()}
            try:
                self.pages.create_entity(entity); created.append((url,int(depth)))
            except Exception:
                continue
        if created:
            self._change(job_id,inc_pending=len(created),inc_pages_discovered=len(created))
            for url,depth in created:
                self.queue.send_message(json.dumps({'job_id':job_id,'domain':domain,'url':url,'depth':depth}))
        return len(created)

    def page(self, job_id, url):
        try: return self.pages.get_entity(job_id,digest(url))
        except Exception: return None

    def mark_page(self, job_id, url, status, **fields):
        e=self.page(job_id,url)
        if not e: return
        e['status']=status; e['updated_utc']=utcnow()
        for k,v in fields.items(): e[k]=v
        try: self.pages.update_entity(e,mode=UpdateMode.REPLACE)
        except Exception: pass

    def finish_page(self, job_id, status='processed', **increments):
        changes={'inc_pending':-1}
        for k,v in increments.items(): changes['inc_'+k]=v
        e=self._change(job_id, **changes)
        if int(e.get('pending',0)) <= 0:
            self._change(job_id, status='completed', completed_utc=utcnow())
            return True
        return False

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
        self.mark_page(job_id,url,'failed',error=str(error)[:500])
        self.finish_page(job_id,'failed',pages_failed=1)
        job=self.get_job(job_id)
        if job and job.get('status')=='completed':
            self._change(job_id,status='completed_with_errors')

    def resume_job(self, job_id):
        job=self.get_job(job_id)
        if not job: return None
        failed=[p for p in self.page_records(job_id) if p.get('status')=='failed']
        if not failed: return {'job_id':job_id,'status':job.get('status'),'requeued':0,'message':'No failed page tasks to resume.'}
        self._change(job_id,status='running',inc_pending=len(failed),inc_pages_failed=-len(failed))
        for page in failed:
            page['status']='queued'; page['updated_utc']=utcnow()
            self.pages.update_entity(page,mode=UpdateMode.REPLACE)
            self.queue.send_message(json.dumps({'job_id':job_id,'domain':job['domain'],'url':page['url'],'depth':int(page.get('depth',0))}))
        return {'job_id':job_id,'status':'running','requeued':len(failed)}
