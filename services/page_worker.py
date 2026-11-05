"""One-URL-at-a-time crawler worker. Queue retries make page work resumable."""
import gc, hashlib, re, ipaddress, socket, time, json
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from services.config import settings
from services.path_rules import PathPolicy
from services.search_service import IndexBusyError
from services.limits import TooLarge, chunk_groups, read_capped

# HTTP statuses that retrying will not fix. 408/429/5xx are not listed and stay retryable.
PERMANENT_HTTP_STATUSES = {400, 401, 403, 404, 405, 406, 410, 451}


class PermanentPageError(Exception):
    """A condition a queue retry cannot fix. The page is skipped, not re-queued."""
    def __init__(self, reason, message=None):
        super().__init__(message or reason)
        self.reason = reason


class PageWorker:
    def __init__(self, ontology, storage, search, resolver, state):
        self.ontology,self.storage,self.search,self.resolver,self.state=ontology,storage,search,resolver,state
        self.path_policy=PathPolicy(ontology)
        self.defer_indexing=None  # set by the queue processor: queues a delayed indexing task
        self.session=requests.Session(); self.session.headers.update({'User-Agent':settings.crawler_user_agent})

    @staticmethod
    def normalize(url):
        from services.crawler import Crawler
        return Crawler._normalize_url(url)

    @staticmethod
    def same_site(url, domain):
        from services.crawler import Crawler
        return Crawler._same_site(url,domain)

    def safe_public_host(self, host):
        try:
            for info in socket.getaddrinfo(host,None):
                ip=ipaddress.ip_address(info[4][0])
                if not ip.is_global: return False
            return True
        except Exception: return False

    def safe_get(self, url, domain, headers=None, stream=False):
        current=url
        for _ in range(6):
            if not self.same_site(current,domain):
                raise PermanentPageError('redirect_off_site','Redirect target is outside the approved site')
            host=urlparse(current).hostname or ''
            if not self.safe_public_host(host):
                raise PermanentPageError('non_public_ip','URL resolves to a non-public IP address')
            response=self.session.get(current,headers=headers or {},timeout=settings.timeout,
                                      allow_redirects=False,stream=stream)
            if response.status_code in (301,302,303,307,308):
                location=response.headers.get('Location')
                response.close()
                if not location: raise PermanentPageError('redirect_no_location','Redirect response omitted Location')
                current=self.normalize(urljoin(current,location))
                continue
            return response
        raise PermanentPageError('redirect_limit','Redirect limit exceeded')

    def process(self, task):
        job_id, domain, url, depth = task['job_id'], task['domain'], self.normalize(task['url']), int(task.get('depth', 0))
        job = self.state.get_job(job_id)
        if not job or job.get('status') in ('cancelled', 'failed', 'completed', 'completed_with_errors'):
            return {'status': 'job_not_active'}
        existing = self.state.page(job_id, url)
        if existing and existing.get('status') in ('processed', 'unchanged', 'skipped', 'failed'):
            return {'status': 'already_terminal', 'page_status': existing.get('status')}
        claim_token = self.state.claim_page(job_id, url)
        if not claim_token:
            return {'status': 'already_claimed'}
        try:
            return self._process_claimed(task, job_id, domain, url, depth, job, claim_token)
        except Exception as exc:
            # Release only this execution's lease. Azure Queue can then retry immediately
            # without waiting for the lease to expire or consuming a duplicate message.
            self.state.release_page_claim(job_id, url, claim_token, error=str(exc))
            raise
        finally:
            # Python keeps freed memory for itself unless the cycle collector runs. Parsing a page builds
            # a large object tree, so collect after every page.
            gc.collect()

    def _complete(self, job_id, url, claim_token, status='processed', page_fields=None, **increments):
        completed = self.state.complete_page(
            job_id, url, status=status, page_fields=page_fields,
            claim_token=claim_token, **increments
        )
        if not completed:
            raise RuntimeError(f"Page claim was lost before completion: {url}")
        return True

    def _process_claimed(self, task, job_id, domain, url, depth, job, claim_token):
        if depth>int(job.get('max_depth',settings.max_crawl_depth)):
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'depth_limit'},pages_skipped=1); return {'status': 'completed'}
        # Pages under a path the ontology excludes for this domain are never fetched. They are not
        # counted as skipped, so excluding a section does not block stale-page cleanup.
        if self.path_policy.is_excluded(domain,url):
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'path_excluded'}); return {'status': 'completed'}
        if not self.same_site(url,domain) or not self.safe_public_host(urlparse(url).hostname or ''):
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'off_site_or_non_public'},pages_skipped=1); return {'status': 'completed'}
        # Require an explicit robots policy response for the actual host.
        robots_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}/robots.txt"
        try:
            rr=self.safe_get(robots_url,domain)
            if rr.status_code in (401,403):
                self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'robots_denied'},pages_skipped=1); return {'status': 'completed'}
            if rr.status_code>=500: raise RuntimeError('robots.txt unavailable')
            if rr.status_code not in (200,404): raise RuntimeError(f'robots.txt returned HTTP {rr.status_code}')
            from urllib.robotparser import RobotFileParser
            rp=RobotFileParser(); rp.parse(rr.text.splitlines() if rr.status_code==200 else [])
            if not rp.can_fetch(settings.crawler_user_agent,url):
                self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'robots_disallowed'},pages_skipped=1); return {'status': 'completed'}
            if depth == 0:
                # Discover sitemap URLs in the background worker, not the HTTP start request.
                from services.crawler import Crawler
                sitemap_crawler=Crawler(self.ontology,self.storage,self.search,self.resolver)
                sitemap_seeds=[line.split(":",1)[1].strip() for line in rr.text.splitlines()
                               if line.strip().lower().startswith("sitemap:")]
                sitemap_urls, _, _, _ = sitemap_crawler._sitemap_urls(domain,sitemap_seeds)
                for sitemap_url in sitemap_urls:
                    if self.path_policy.is_excluded(domain,sitemap_url): continue
                    self.state.enqueue_page(job_id,domain,sitemap_url,1)
        except PermanentPageError as exc:
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'robots_'+exc.reason},pages_skipped=1); return {'status': 'completed'}
        except Exception as exc:
            raise RuntimeError(f'robots policy could not be verified: {exc}')

        previous=self.state.manifest_get(domain,url)
        headers={}
        if previous:
            if previous.get('etag'): headers['If-None-Match']=previous['etag']
            if previous.get('last_modified'): headers['If-Modified-Since']=previous['last_modified']
        try:
            response=self.safe_get(url,domain,headers=headers,stream=True)
        except PermanentPageError as exc:
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':exc.reason},pages_skipped=1); return {'status': 'completed'}
        if response.status_code==304:
            self.state.manifest_put(domain,url,last_seen_job=job_id,last_status=304)
            self._complete(job_id,url,claim_token,'unchanged',pages_processed=1); self._finalize_if_safe(job_id,domain); return {'status': 'completed'}
        if response.status_code in PERMANENT_HTTP_STATUSES:
            response.close()
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':f'http_{response.status_code}'},pages_skipped=1); return {'status': 'completed'}
        response.raise_for_status()
        final_url=self.normalize(response.url)
        if not self.same_site(final_url,domain):
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'redirected_off_site'},pages_skipped=1); return {'status': 'completed'}
        content_type=response.headers.get('content-type','').lower()
        # Pick the kind from the headers first. An unsupported file is then never downloaded, and each kind
        # gets its own size cap: HTML costs about 35x its size in memory to parse, so it is capped lowest.
        if 'text/html' in content_type or 'application/xhtml' in content_type: kind='html'
        elif 'application/pdf' in content_type or urlparse(final_url).path.lower().endswith('.pdf'): kind='pdf'
        elif 'wordprocessingml.document' in content_type or final_url.lower().endswith('.docx'): kind='docx'
        else:
            response.close()
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'unsupported_content_type'},pages_skipped=1); return {'status': 'completed'}
        try:
            raw=read_capped(response, settings.max_html_bytes if kind=='html' else settings.max_doc_bytes)
        except TooLarge:
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'page_too_large'},pages_skipped=1); return {'status': 'completed'}
        title=final_url; text=''; discovered=[]; page_type='html'
        if kind=='html':
            raw_text = raw.decode('utf-8', errors='replace'); del raw
            soup = BeautifulSoup(raw_text, 'html.parser'); del raw_text
            title=soup.title.get_text(' ',strip=True) if soup.title else final_url
            canonical=soup.find('link',rel=lambda x:x and 'canonical' in x)
            if canonical and canonical.get('href'):
                canonical_url=self.normalize(urljoin(final_url,canonical['href']))
                if self.same_site(canonical_url,domain): final_url=canonical_url
            for a in soup.find_all('a',href=True):
                candidate=self.normalize(urljoin(final_url,a['href']))
                if self.same_site(candidate,domain) and not self.path_policy.is_excluded(domain,candidate) and (not urlparse(candidate).path.rsplit('/',1)[-1].count('.') or urlparse(candidate).path.lower().endswith(tuple(settings.allowed_extensions))):
                    discovered.append(candidate)
            for tag in soup(['script','style','nav','footer','header','noscript','svg','form']): tag.decompose()
            for node in soup.select('[role=navigation],.cookie,.cookie-banner,.cookie-consent,.advertisement,.ads'):
                node.decompose()
            text=re.sub(r'\s+',' ',soup.get_text(' ',strip=True)).strip()
            try: soup.decompose()
            except Exception: pass
            del soup
        elif kind=='pdf':
            from pypdf import PdfReader
            import io
            page_type='pdf'; reader=PdfReader(io.BytesIO(raw)); text=' '.join((p.extract_text() or '') for p in reader.pages)
            title=urlparse(final_url).path.rsplit('/',1)[-1]
            del reader, raw
        else:
            from docx import Document
            import io
            page_type='docx'; doc=Document(io.BytesIO(raw)); text=' '.join(p.text for p in doc.paragraphs)
            title=urlparse(final_url).path.rsplit('/',1)[-1]
            del doc, raw
        text=re.sub(r'\s+',' ',text).strip()
        # A page this long is cut at the chunk cap. Hash and index the same text, so a later crawl compares like with like.
        truncated=False
        if settings.max_chunks_per_page and len(text)>settings.max_chunks_per_page*settings.chunk_chars:
            text=text[:settings.max_chunks_per_page*settings.chunk_chars]; truncated=True
        digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
        if len(text)<settings.min_text_chars:
            self._complete(job_id,url,claim_token,'skipped',page_fields={'reason':'insufficient_text'},pages_skipped=1); return {'status': 'completed'}
        old_hash=previous.get('content_hash') if previous else None
        if old_hash==digest:
            self.state.manifest_put(domain,final_url,content_hash=digest,last_seen_job=job_id,etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'),page_type=page_type)
            self._complete(job_id,url,claim_token,'unchanged',pages_processed=1); self._finalize_if_safe(job_id,domain); return {'status': 'completed'}
        # Remove prior chunks for this URL before writing a changed version, preventing
        # orphaned tail chunks when the new page is shorter.
        self.search.delete_url(final_url)
        page_id=hashlib.sha256(final_url.encode()).hexdigest(); chunks=tagged=untagged=0
        allowed=json.loads(job.get('allowed_concepts_json','[]'))
        # Embed and store a few chunks at a time. Each vector is about 50 KB as a Python list, so holding every
        # chunk of a long page at once costs tens of megabytes.
        deferred=[]
        for group in chunk_groups(text,settings.chunk_chars,settings.max_chunks_per_page,settings.chunk_group_size):
            page_documents=[]
            for index,chunk in group:
                concepts=self.resolver.tag_document(title,chunk,allowed)
                page_documents.append({'id':f'{page_id}-{index}','title':title,'content':chunk,'url':final_url,'domain':domain,
                    'concepts':concepts,'content_hash':digest,'page_hash':digest,'crawl_depth':depth,'page_type':page_type,
                    'etag':response.headers.get('ETag'),'last_modified':response.headers.get('Last-Modified'),
                    'crawled_utc':datetime.now(timezone.utc).isoformat()})
                chunks+=1; tagged+=bool(concepts); untagged+=not bool(concepts)
            _, group_deferred=self.search.upsert_or_defer(page_documents, persist_blobs=True)
            deferred.extend(group_deferred)
        index_deferred=False
        if deferred:
            # The chunks and their vectors are safely stored. Index them later instead of
            # failing the page, which would re-fetch and re-embed it on every retry.
            if not self.defer_indexing:
                raise IndexBusyError(f'Search index busy; {len(deferred)} chunk(s) stored but not indexed.')
            self.defer_indexing(deferred); index_deferred=True
        self.state.manifest_put(domain,final_url,content_hash=digest,last_seen_job=job_id,etag=response.headers.get('ETag'),
            last_modified=response.headers.get('Last-Modified'),page_type=page_type,chunk_count=chunks)
        # Discover URLs before marking this page complete, so pending never transiently reaches zero.
        for link in dict.fromkeys(discovered):
            if self.state.enqueue_page(job_id,domain,link,depth+1): pass
        self._complete(job_id,url,claim_token,'processed',page_fields={'content_hash':digest,'final_url':final_url,**({'index_deferred':True} if index_deferred else {}),**({'truncated_at_chunk_cap':True} if truncated else {})},pages_processed=1,chunks_indexed=chunks,chunks_tagged=tagged,chunks_untagged=untagged)
        self._finalize_if_safe(job_id,domain)
        if settings.crawl_delay > 0: time.sleep(settings.crawl_delay)
        return {'status': 'completed'}


    def _finalize_if_safe(self, job_id, domain):
        job=self.state.get_job(job_id)
        if not job or job.get('status')!='completed': return
        # Only remove stale pages after a clean, non-truncated crawl with no skipped or failed URLs.
        if int(job.get('pages_failed',0)) or int(job.get('pages_skipped',0)) or int(job.get('pages_processed',0)) >= int(job.get('page_limit',0)):
            return
        removed_pages=0; removed_chunks=0
        for record in self.state.manifest_for_domain(domain):
            if record.get('last_seen_job') == job_id: continue
            url=record.get('url')
            if not url: continue
            removed_chunks += self.search.delete_url(url)
            record['status']='stale'; record['stale_detected_utc']=datetime.now(timezone.utc).isoformat()
            try:
                from azure.data.tables import UpdateMode
                self.state.manifest.upsert_entity(record, mode=UpdateMode.MERGE)
            except Exception: pass
            removed_pages += 1
        if removed_pages:
            self.state._change(job_id, stale_pages_removed=removed_pages, stale_chunks_removed=removed_chunks)