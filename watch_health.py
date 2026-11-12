"""Poll the websearch /api/health route and report which instance answered, how long it has been up, and what
its search result cache and local index are doing. Use it during a load test to tell a cache that works from one
that is off, and a restart (uptime drops, or the process id changes) from instances simply coming and going.

    python watch_health.py --url https://<websearch-app>.azurewebsites.net/api/health --key <function key> --every 5 --duration 300

The key is sent in the x-functions-key header and is never printed. Each poll reaches whichever instance the
platform picks, so with several instances running you will see several names. Poll often enough (every few
seconds) that each instance shows up more than once.
"""
import argparse
import json
import os
import sys
import time
import urllib.request


def fetch(url, key, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json", **({"x-functions-key": key} if key else {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def short(name):
    name = str(name or "?")
    return name if len(name) <= 12 else name[:6] + ".." + name[-4:]


def main():
    ap = argparse.ArgumentParser(description="Watch websearch instances, the result cache and the local index.")
    ap.add_argument("--url", required=True, help="the health route, ending in /api/health")
    ap.add_argument("--key", default=os.getenv("WEBSEARCH_FUNCTION_KEY", ""), help="function key (or set WEBSEARCH_FUNCTION_KEY)")
    ap.add_argument("--every", type=float, default=5.0, help="seconds between polls")
    ap.add_argument("--duration", type=float, default=120.0, help="seconds to keep polling")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    seen = {}        # instance -> dict(first_poll_time, last_pid, last_uptime, polls, restarts)
    restarts = []    # (clock time, instance, why)
    started = time.time()
    print("time      instance        pid   uptime  cache  entries     hits   misses  hit-rate  index-chunks")
    try:
        while time.time() - started < args.duration:
            clock = time.strftime("%H:%M:%S")
            try:
                body = fetch(args.url, args.key, args.timeout)
            except Exception as exc:
                print(f"{clock}  poll failed: {type(exc).__name__}")
                time.sleep(args.every)
                continue
            proc, cache, index = body.get("process") or {}, body.get("query_cache") or {}, body.get("local_index") or {}
            if not proc:
                sys.exit("This health route has no 'process' section. Deploy the version that reports it first.")
            name, pid, up = proc.get("instance"), proc.get("pid"), proc.get("uptime_seconds", 0)
            record = seen.get(name)
            flag = ""
            if record is None:
                seen[name] = {"first": clock, "pid": pid, "uptime": up, "polls": 1, "restarts": 0}
                flag = "  <- first time seen"
            else:
                if pid != record["pid"] or up < record["uptime"]:
                    record["restarts"] += 1
                    why = "process id changed" if pid != record["pid"] else "uptime went back down"
                    restarts.append((clock, name, why))
                    flag = f"  <- RESTART ({why})"
                record.update(pid=pid, uptime=up, polls=record["polls"] + 1)
            rate = cache.get("hit_rate")
            print(f"{clock}  {short(name):14s}  {pid!s:5s} {up:6d}s  {'on ' if cache.get('enabled') else 'off'}   {cache.get('entries', 0):7d} "
                  f"{cache.get('hits', 0):8d} {cache.get('misses', 0):8d}  {('%.0f%%' % (rate * 100)) if rate is not None else '   -':>8s}  {index.get('chunks', 0):10d}{flag}")
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nStopped.")

    print(f"\nInstances seen: {len(seen)}. Restarts detected: {len(restarts)}.")
    for name, record in seen.items():
        print(f"  {short(name):14s} first seen {record['first']}, polled {record['polls']}x, restarts {record['restarts']}, last uptime {record['uptime']}s")
    for clock, name, why in restarts:
        print(f"  RESTART at {clock}: {short(name)} ({why})")
    if len(seen) > 1 and not restarts:
        print("Several instances and no restarts: memory that falls is most likely instances being added or removed in the average.")


if __name__ == "__main__":
    main()
