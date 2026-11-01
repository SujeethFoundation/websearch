
import os
from collections import defaultdict
from azure.storage.blob import BlobServiceClient

# Read the existing connection string from environment
connection_string = os.getenv("BLOB_STORAGE_CONNECTION_STRING")

if not connection_string:
    raise RuntimeError(
        "BLOB_STORAGE_CONNECTION_STRING is not set. "
        "Load it from your local.settings.json."
    )

container_name = "web-content"

service = BlobServiceClient.from_connection_string(connection_string)
container = service.get_container_client(container_name)

stats = defaultdict(lambda: {"count": 0, "bytes": 0})

print(f"Inspecting container: {container_name}")

for blob in container.list_blobs():
    name = blob.name

    if name.startswith("documents/"):
        category = "Documents"
    elif name.startswith("crawl-"):
        category = "Crawl data"
    else:
        category = "Other"

    stats[category]["count"] += 1
    stats[category]["bytes"] += blob.size or 0

print("\nStorage summary")
print("-" * 65)

total_bytes = 0
total_count = 0

for category, values in sorted(stats.items()):
    size_gb = values["bytes"] / (1024 ** 3)
    total_bytes += values["bytes"]
    total_count += values["count"]

    print(
        f"{category:15} "
        f"{values['count']:>10,} blobs "
        f"{size_gb:>12,.2f} GB"
    )

print("-" * 65)
print(f"{'TOTAL':15} {total_count:>10,} blobs "
      f"{total_bytes / (1024 ** 3):>12,.2f} GB")
