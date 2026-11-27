"""Build the prebuilt search-index snapshot from the table and save it to Blob Storage, from any computer.

A small instance (512 MB) cannot scan the whole index table: the scan peaks several hundred MB above the steady
size. With LOCAL_INDEX_SNAPSHOT_ONLY=true the instances never scan, so after a crawl (or any change to the table)
this script builds the snapshot, and every instance loads it within LOCAL_INDEX_POLL_SECONDS plus its reload jitter.

Run from the repo root (needs about 1 GB of memory and the same storage connection strings the app uses):

    pip install -r requirements.txt
    python tools/build_snapshot.py                      # reads local.settings.json (Values) when it exists
    python tools/build_snapshot.py --settings path.json

Set the same index settings the instances use, because the snapshot carries them: LOCAL_POSTING_MAX_DF,
LOCAL_POSTING_WEIGHT_DTYPE, LOCAL_INDEX_TEXT_IN_MEMORY, LOCAL_RESCORE, and the ontology URL and key (the ontology
decides which domains keep their text out of memory). Needs LOCAL_INDEX_CONNECTION_STRING or
BLOB_STORAGE_CONNECTION_STRING, and BLOB_CONTAINER.
"""
import argparse
import json
import os
import sys
import time


def load_settings(path):
    if not path or not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        values = json.load(handle).get("Values", {})
    count = 0
    for key, value in values.items():
        if key not in os.environ and value not in (None, ""):
            os.environ[key] = str(value)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--settings", default="local.settings.json", help="a Functions local.settings.json (default: that file, if present)")
    args = parser.parse_args()
    loaded = load_settings(args.settings)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from services.config import settings
    from services.local_index import LocalIndexManager

    if not (settings.local_index_connection or os.getenv("BLOB_STORAGE_CONNECTION_STRING")):
        sys.exit("Set LOCAL_INDEX_CONNECTION_STRING or BLOB_STORAGE_CONNECTION_STRING (or use --settings).")
    print(f"settings loaded from {args.settings}: {loaded} value(s); posting dtype {settings.local_posting_weight_dtype}, "
          f"max df {settings.local_posting_max_df}, rescore {settings.local_rescore}, text in memory {settings.local_index_text_in_memory}")
    manager = LocalIndexManager()
    began = time.perf_counter()
    manager.reload(use_snapshot=False, publish=True)
    status = manager.snapshot_status()
    print(f"built and published in {time.perf_counter() - began:.0f} s")
    print(json.dumps(status.get("current") or status, indent=2, default=str))
    if not (status.get("current") or {}).get("matches_table"):
        sys.exit("The snapshot does not match the table's version (something wrote to the table meanwhile); run this again.")


if __name__ == "__main__":
    main()
