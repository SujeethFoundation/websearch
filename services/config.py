
import os


class Settings:
    ontology_api_url = os.getenv("ONTOLOGY_API_URL", "").rstrip("/")
    ontology_function_key = os.getenv("ONTOLOGY_FUNCTION_KEY", "")
    local_data_dir = os.getenv("LOCAL_DATA_DIR", "data")
    blob_connection_string = os.getenv("BLOB_STORAGE_CONNECTION_STRING", "")
    blob_container = os.getenv("BLOB_CONTAINER", "web-content")
    embedding_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    embedding_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    embedding_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "")
    embedding_api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    max_pages = int(os.getenv("MAX_PAGES_PER_DOMAIN", "1000"))
    max_crawl_depth = int(os.getenv("MAX_CRAWL_DEPTH", "5"))
    crawl_delay = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
    max_sitemap_urls = int(os.getenv("MAX_SITEMAP_URLS", "5000"))
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    max_query_concepts = int(os.getenv("MAX_QUERY_CONCEPTS", "5"))
    embedding_configured = bool(
        embedding_endpoint and embedding_key and embedding_deployment
    )


settings = Settings()
