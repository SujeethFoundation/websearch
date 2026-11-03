"""Inspect and recover messages in the crawl poison queue.

Messages land in the poison queue after the Functions host has dequeued them 5 times without success.
This script reads them, shows what they are, and can move them back to the main queue.

    $secure = Read-Host "Storage connection string" -AsSecureString
    $env:QCONN = [System.Net.NetworkCredential]::new("", $secure).Password
    python .\\poison_queue.py peek                       # read-only: what is in there
    python .\\poison_queue.py requeue                    # dry run: shows what would move
    python .\\poison_queue.py requeue --execute          # moves them back to the main queue
    python .\\poison_queue.py requeue --execute --type crawl_page --limit 5
    Remove-Item Env:QCONN

The app writes queue messages base64-encoded and the Functions host (host.json messageEncoding=base64)
can only read those. Base64 messages are moved back exactly as stored. A message stored as plain JSON
is base64-encoded on the way back, because the host would reject it. A message is deleted from the
poison queue only after it has been sent to the main queue.

Defaults match the app: main queue web-crawl-pages, poison queue web-crawl-pages-poison.
Override with --queue and --poison.
"""
import argparse
import base64
import json
import os
import sys

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.queue import QueueClient

PEEK_MAX = 32


def connection_string():
    # QCONN first: it is what qdepth.py reads, and the queues live in the AzureWebJobsStorage account.
    for name in ("QCONN", "AzureWebJobsStorage", "BCONN", "BLOB_STORAGE_CONNECTION_STRING"):
        if os.environ.get(name):
            return os.environ[name]
    sys.exit("Set QCONN to the queue storage account connection string first (see the top of this file).")


def decode_task(content):
    """Return (task, encoding): encoding is "base64", "plain" (JSON text), or None if unreadable.

    The Functions host is set to messageEncoding=base64 (host.json), so it can only read "base64"
    messages. A "plain" JSON message is rejected by the host before any function runs, which is why
    it shows no function errors and goes straight back to the poison queue.
    """
    try:
        return json.loads(base64.b64decode(content, validate=True).decode("utf-8")), "base64"
    except Exception:
        pass
    try:
        return json.loads(content), "plain"
    except Exception:
        return None, None


def describe(task):
    if not isinstance(task, dict):
        return "(unreadable)", ""
    kind = task.get("task_type", "crawl_page")
    if kind == "crawl_page":
        detail = f"job={str(task.get('job_id'))[:8]} url={task.get('url')}"
    elif "blob_names" in task:
        detail = f"{len(task['blob_names'])} blobs  attempt={task.get('attempt', 0)}  mode={task.get('mode')}"
    else:
        detail = f"domains={task.get('domains') or 'all'}"
    return kind, detail


def show(messages):
    print(f"{'#':>3}  {'Type':<20}{'Encoding':<10}{'Queued (UTC)':<17}Detail")
    counts = {}
    for i, msg in enumerate(messages, 1):
        task, enc = decode_task(msg.content)
        kind, detail = describe(task)
        counts[enc] = counts.get(enc, 0) + 1
        when = msg.inserted_on.strftime("%Y-%m-%d %H:%M") if getattr(msg, "inserted_on", None) else ""
        print(f"{i:>3}  {kind:<20}{str(enc):<10}{when:<17}{detail}")
    if counts.get("plain") or counts.get(None):
        print("\nWARNING: messages marked plain or None are not base64. The Functions host (messageEncoding=base64)")
        print("cannot read them and sends them straight back to the poison queue. requeue re-encodes plain ones")
        print("and leaves unreadable (None) ones in place.")


def account_name(poison):
    return getattr(poison, "account_name", "?")


def hidden_note(poison, visible):
    """Explain a gap between the queue's message count and what can be read right now."""
    total = poison.get_queue_properties().approximate_message_count
    print(f"\nQueue {poison.queue_name} on account {account_name(poison)}: about {total} messages, {visible} readable right now.")
    if total > visible:
        print(f"{total - visible} message(s) are hidden. A message is hidden while the Functions host is processing it or waiting")
        print("to retry it (host.json: 15-minute visibility timeout, 5 tries). Peek again later, or check the")
        print(f"'{poison.queue_name}-poison' queue, where the host moves messages that keep failing.")
    return total


def peek(poison):
    messages = list(poison.peek_messages(max_messages=PEEK_MAX))
    if messages:
        show(messages)
    hidden_note(poison, len(messages))
    return 0


def requeue(main, poison, execute, only_type, limit):
    if not execute:
        # A dry run only peeks, so no message is hidden or changed.
        chosen = []
        for m in poison.peek_messages(max_messages=PEEK_MAX):
            task, enc = decode_task(m.content)
            kind, detail = describe(task)
            if enc is None or (only_type and kind != only_type):
                continue
            chosen.append((kind, detail, enc))
        if limit:
            chosen = chosen[:limit]
        for kind, detail, enc in chosen:
            print(f"would move  {kind}  {detail}" + ("  (will be base64-encoded)" if enc == "plain" else ""))
        print(f"\nDry run: {len(chosen)} message(s) would move. Add --execute to do it. "
              f"A dry run looks at the first {PEEK_MAX} messages only.")
        return 0
    moved = skipped = unreadable = 0
    # Received messages stay hidden for 60 seconds. Ones we do not move reappear after that.
    for msg in poison.receive_messages(messages_per_page=PEEK_MAX, visibility_timeout=60):
        if limit and moved >= limit:
            break
        task, enc = decode_task(msg.content)
        kind, detail = describe(task)
        if enc is None:
            unreadable += 1
            print(f"left   (unreadable body, not moved)  {msg.content[:60]!r}")
            continue
        if only_type and kind != only_type:
            skipped += 1
            continue
        # The host needs base64. Base64 messages go back exactly as stored; plain ones are encoded first.
        body = msg.content if enc == "base64" else base64.b64encode(msg.content.encode("utf-8")).decode("ascii")
        main.send_message(body)
        poison.delete_message(msg)       # only after the send succeeded
        moved += 1
        print(f"moved  {kind}  {detail}" + ("  (re-encoded to base64)" if enc == "plain" else ""))
    print(f"\nMoved {moved} message(s) back to the main queue; {skipped} left by --type; {unreadable} unreadable.")
    hidden_note(poison, skipped + unreadable)
    return 0


def main(argv=None, main_queue=None, poison_queue=None):
    parser = argparse.ArgumentParser(description="Inspect and recover crawl poison-queue messages.")
    parser.add_argument("command", choices=["peek", "requeue"])
    parser.add_argument("--queue", default=os.getenv("CRAWL_QUEUE_NAME", "web-crawl-pages"))
    parser.add_argument("--poison", default=os.getenv("CRAWL_POISON_QUEUE_NAME", "web-crawl-pages-poison"))
    parser.add_argument("--execute", action="store_true", help="Actually move messages (requeue only)")
    parser.add_argument("--type", dest="only_type", help="Only move this task type, e.g. crawl_page or index_blob_batch")
    parser.add_argument("--limit", type=int, default=0, help="Move at most this many messages")
    args = parser.parse_args(argv)
    if poison_queue is None:
        conn = connection_string()
        # No encode/decode policy on purpose: bodies pass through exactly as stored.
        main_queue = QueueClient.from_connection_string(conn, args.queue)
        poison_queue = QueueClient.from_connection_string(conn, args.poison)
    try:
        if args.command == "peek":
            return peek(poison_queue)
        return requeue(main_queue, poison_queue, args.execute, args.only_type, args.limit)
    except ResourceNotFoundError:
        sys.exit(f"Queue not found on storage account {account_name(poison_queue)}. "
                 f"Check the queue names ({args.queue}, {args.poison}) and that QCONN is the AzureWebJobsStorage account.")


if __name__ == "__main__":
    sys.exit(main())
