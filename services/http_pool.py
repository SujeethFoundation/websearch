"""A requests Session whose connection pool is sized for the requests an instance runs at the same time."""
import requests
from requests.adapters import HTTPAdapter

from services.config import settings


def pooled_session():
    """Like requests.Session(), but keeps settings.http_pool_size connections per host instead of 10, so a busy
    instance reuses connections instead of opening and discarding one for every request over ten."""
    size = settings.http_pool_size
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=size, pool_maxsize=size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
