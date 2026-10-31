"""One-URL-at-a-time crawler worker. Queue retries make page work resumable."""
import hashlib, re, ipaddress, socket, time
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from services.config import settings

class PageWorker:
    def __init__(self, ontology, storage, search, resolver, state):
        self.ontology,self.storage,self.search,self.resolver,self.state=ontology,storage,search,resolver,state
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
                raise RuntimeError('Redirect target is outside the approved site')
            host=urlparse(current).hostname or ''
            if not self.safe_public_host(host):
                raise RuntimeError('URL resolves to a non-public IP address')
            response=self.session.get(current,headers=headers or {},timeout=settings.timeout,
                                      allow_redirects=False,stream=stream)
            if response.status_code in (301,302,303,307,308):
                location=response.headers.get('Location')
                response.close()
                if not location: raise RuntimeError('Redirect response omitted Location')
                current=self.normalize(urljoin(current,location))
                continue
            return response
        raise RuntimeError('Redirect limit exceeded')

    def process(self, task):
        job_id,domain,url,depth=task['job_id'],task['domain'],self.normalize(task['url']),int(task.get('depth',0))
        job=self.state.get_job(job_id)
        if not job or job.get('status') in ('cancelled','failed','completed','completed_with_errors'):
            return
        existing=self.state.page(job_id,url)
        if existing and existing.get('status') in ('processed','unchanged','skipped','failed'):
            return
        if not self.state.claim_page(job_id,url):
            return
        if depth>int(job.get('max_depth',settings.max_crawl_depth)):
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'depth_limit'},pages_skipped=1); return
        if not self.same_site(url,domain) or not self.safe_public_host(urlparse(url).hostname or ''):
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'off_site_or_non_public'},pages_skipped=1); return
        # Require an explicit robots policy response for the actual host.
        robots_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}/robots.txt"
        try:
            rr=self.safe_get(robots_url,domain)
            if rr.status_code in (401,403):
                self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'robots_denied'},pages_skipped=1); return
            if rr.status_code>=500: raise RuntimeError('robots.txt unavailable')
            if rr.status_code not in (200,404): raise RuntimeError(f'robots.txt returned HTTP {rr.status_code}')
            from urllib.robotparser import RobotFileParser
            rp=RobotFileParser(); rp.parse(rr.text.splitlines() if rr.status_code==200 else [])
            if not rp.can_fetch(settings.crawler_user_agent,url):
                self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'robots_disallowed'},pages_skipped=1); return
            if depth == 0:
                # Discover sitemap URLs in the background worker, not the HTTP start request.
                from services.crawler import Crawler
                sitemap_crawler=Crawler(self.ontology,self.storage,self.search,self.resolver)
                sitemap_seeds=[line.split(":",1)[1].strip() for line in rr.text.splitlines()
                               if line.strip().lower().startswith("sitemap:")]
                sitemap_urls, _, _, _ = sitemap_crawler._sitemap_urls(domain,sitemap_seeds)
                for sitemap_url in sitemap_urls:
                    self.state.enqueue_page(job_id,domain,sitemap_url,1)
        except Exception as exc:
            raise RuntimeError(f'robots policy could not be verified: {exc}')

        previous=self.state.manifest_get(domain,url)
        headers={}
        if previous:
            if previous.get('etag'): headers['If-None-Match']=previous['etag']
            if previous.get('last_modified'): headers['If-Modified-Since']=previous['last_modified']
        response=self.safe_get(url,domain,headers=headers,stream=True)
        if response.status_code==304:
            self.state.manifest_put(domain,url,last_seen_job=job_id,last_status=304)
            self.state.complete_page(job_id,url,'unchanged',pages_processed=1); self._finalize_if_safe(job_id,domain); return
        response.raise_for_status()
        final_url=self.normalize(response.url)
        if not self.same_site(final_url,domain):
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'redirected_off_site'},pages_skipped=1); return
        content_type=response.headers.get('content-type','').lower()
        raw=bytearray()
        for block in response.iter_content(chunk_size=65536):
            if block:
                raw.extend(block)
                if len(raw)>settings.max_page_bytes: break
        if len(raw)>settings.max_page_bytes:
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'page_too_large'},pages_skipped=1); return
        title=final_url; text=''; discovered=[]; page_type='html'
        if 'text/html' in content_type or 'application/xhtml' in content_type:
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8", errors="replace")
            soup = BeautifulSoup(raw, "html.parser")
            title=soup.title.get_text(' ',strip=True) if soup.title else final_url
            canonical=soup.find('link',rel=lambda x:x and 'canonical' in x)
            if canonical and canonical.get('href'):
                canonical_url=self.normalize(urljoin(final_url,canonical['href']))
                if self.same_site(canonical_url,domain): final_url=canonical_url
            for a in soup.find_all('a',href=True):
                candidate=self.normalize(urljoin(final_url,a['href']))
                if self.same_site(candidate,domain) and (not urlparse(candidate).path.rsplit('/',1)[-1].count('.') or urlparse(candidate).path.lower().endswith(tuple(settings.allowed_extensions))):
                    discovered.append(candidate)
            for tag in soup(['script','style','nav','footer','header','noscript','svg','form']): tag.decompose()
            for node in soup.select('[role=navigation],.cookie,.cookie-banner,.cookie-consent,.advertisement,.ads'):
                node.decompose()
            text=re.sub(r'\s+',' ',soup.get_text(' ',strip=True)).strip()
        elif 'application/pdf' in content_type or urlparse(final_url).path.lower().endswith('.pdf'):
            from pypdf import PdfReader
            import io
            page_type='pdf'; reader=PdfReader(io.BytesIO(raw)); text=' '.join((p.extract_text() or '') for p in reader.pages)
            title=urlparse(final_url).path.rsplit('/',1)[-1]
        elif 'wordprocessingml.document' in content_type or final_url.lower().endswith('.docx'):
            from docx import Document
            import io
            page_type='docx'; doc=Document(io.BytesIO(raw)); text=' '.join(p.text for p in doc.paragraphs)
            title=urlparse(final_url).path.rsplit('/',1)[-1]
        else:
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'unsupported_content_type'},pages_skipped=1); return
        text=re.sub(r'\s+',' ',text).strip()
        digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
        if len(text)<settings.min_text_chars:
            self.state.complete_page(job_id,url,'skipped',page_fields={'reason':'insufficient_text'},pages_skipped=1); return
        old_hash=previous.get('content_hash') if previous else None
        if old_hash==digest:
            self.state.manifest_put(domain,final_url,content_hash=digest,last_seen_job=job_id,etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'),page_type=page_type)
            self.state.complete_page(job_id,url,'unchanged',pages_processed=1); self._finalize_if_safe(job_id,domain); return
        # Remove prior chunks for this URL before writing a changed version, preventing
        # orphaned tail chunks when the new page is shorter.
        self.search.delete_url(final_url)
        page_id=hashlib.sha256(final_url.encode()).hexdigest(); chunks=tagged=untagged=0
        allowed=json.loads(job.get('allowed_concepts_json','[]'))
        for index,start in enumerate(range(0,len(text),settings.chunk_chars)):
            chunk=text[start:start+settings.chunk_chars]
            concepts=self.resolver.tag_document(title,chunk,allowed)
            self.search.upsert({'id':f'{page_id}-{index}','title':title,'content':chunk,'url':final_url,'domain':domain,
                'concepts':concepts,'content_hash':digest,'page_hash':digest,'crawl_depth':depth,'page_type':page_type,
                'etag':response.headers.get('ETag'),'last_modified':response.headers.get('Last-Modified'),
                'crawled_utc':datetime.now(timezone.utc).isoformat()})
            chunks+=1; tagged+=bool(concepts); untagged+=not bool(concepts)
        self.state.manifest_put(domain,final_url,content_hash=digest,last_seen_job=job_id,etag=response.headers.get('ETag'),
            last_modified=response.headers.get('Last-Modified'),page_type=page_type,chunk_count=chunks)
        # Discover URLs before marking this page complete, so pending never transiently reaches zero.
        for link in dict.fromkeys(discovered):
            if self.state.enqueue_page(job_id,domain,link,depth+1): pass
        self.state.complete_page(job_id,url,'processed',page_fields={'content_hash':digest,'final_url':final_url},pages_processed=1,chunks_indexed=chunks,chunks_tagged=tagged,chunks_untagged=untagged)
        self._finalize_if_safe(job_id,domain)
        if settings.crawl_delay>0: time.sleep(settings.crawl_delay)

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
