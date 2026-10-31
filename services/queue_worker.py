import json, logging
from services.config import settings
from services.crawl_state import CrawlState
from services.page_worker import PageWorker

log=logging.getLogger('sujeeth-web-search.crawl-worker')

class QueueProcessor:
    def __init__(self, ontology, storage, search, resolver):
        self.state=CrawlState()
        self.worker=PageWorker(ontology,storage,search,resolver,self.state)
    def process(self, message):
        task=json.loads(message)
        self.worker.process(task)
        return {'job_id':task.get('job_id'),'url':task.get('url'),'status':'processed'}
