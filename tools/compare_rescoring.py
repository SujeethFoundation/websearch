"""Compare how the rescoring vectors rank, held at 8 bits over every dimension (the exact reference) against smaller copies.

Run it before changing LOCAL_RESCORE_BITS or LOCAL_RESCORE_DIMS on an index you care about. It reads the index table
(or whatever your settings point at) once, takes some chunks' own stored vectors as stand-in questions, and for each one
does what a search does: the 1-bit codes pick a shortlist of LOCAL_RESCORE_POOL chunks, then the shortlist is ranked
twice, once on the reference vectors and once on the smaller copies. It prints how often the two agree on the best chunk
and how much of the reference top 10 the smaller copies keep.

Needs the same storage connection strings the app uses (see tools/build_snapshot.py), about 1 GB of memory, and no
embedding calls:

    python tools/compare_rescoring.py                           # 4 bits over 1,024 dimensions, 500 questions
    python tools/compare_rescoring.py --bits 8 --dims 1024      # try another shape
    python tools/compare_rescoring.py --bits 4 --dims 0         # 4 bits, every dimension
    python tools/compare_rescoring.py --sweep                   # a table of shapes and 4-bit scalings on one index load
    python tools/compare_rescoring.py --settings other.json

A chunk's own vector is a fair question for ranking the neighbours it has, but a real question is shorter and vaguer than a
chunk, so treat the numbers as a guide: a drop that is small here will not be large on real questions, and a large drop
is a reason to keep the old setting (LOCAL_RESCORE_BITS=8, LOCAL_RESCORE_DIMS=0).
"""
import argparse
import os
import sys
import time

import numpy as np


def compare(index, bits, dims, queries=500, pool=300, seed=1, top=10, clip=None):
    """{"top1": share of questions where both rank the same chunk first, "recall": mean share of the reference top
    `top` kept, ...} for an index built with 8-bit rescoring over every dimension."""
    from services import local_index as li

    full = np.asarray(index.rvec)                                  # int8, every dimension
    if full.dtype != np.int8 or index.rescore_bits != 8 or index.rescore_dims != index.vec_dims:
        raise ValueError("build the reference index with LOCAL_RESCORE_BITS=8 and LOCAL_RESCORE_DIMS=0")
    n = index.n
    use_dims = index.vec_dims if not dims or dims >= index.vec_dims else int(dims)
    use_bits = 8 if bits >= 8 else 4
    packed = np.empty((n, li.rescore_width(use_dims, use_bits)), dtype=np.int8 if use_bits == 8 else np.uint8)
    norms = np.empty(n, dtype=np.float32)
    for start in range(0, n, li.RESCORE_BLOCK):
        packed[start:start + li.RESCORE_BLOCK], norms[start:start + li.RESCORE_BLOCK] = li.pack_rescore(
            full[start:start + li.RESCORE_BLOCK], use_dims, use_bits, clip)
    ref_norm = np.sqrt((full.astype(np.float32) ** 2).sum(axis=1))
    ref_norm[ref_norm == 0] = 1.0

    rng = np.random.default_rng(seed)
    picks = rng.choice(n, size=min(int(queries), n), replace=False)
    same_top, kept, kept_one = 0, 0.0, 0.0
    for row in picks:
        qvec = full[row]
        code = li._words([li.binary_code(qvec.astype(np.float32))], index.code_bytes)[0]
        distance = li._popcount_rows(index.codes ^ code)
        distance[row] = np.iinfo(np.int32).max                      # the question's own chunk is not an answer
        shortlist = np.argpartition(distance, min(pool, n - 1) - 1)[:pool] if n > pool else np.arange(n)
        shortlist = shortlist[shortlist != row]
        q = qvec.astype(np.float32)
        ref = (full[shortlist].astype(np.float32) @ q) / ref_norm[shortlist]
        qs = q[:use_dims]
        small = (li.unpack_rescore(packed[shortlist], use_dims, use_bits) @ qs) / (norms[shortlist] * (float(np.linalg.norm(qs)) or 1.0))
        k = min(top, len(shortlist))
        ref_top, small_top = shortlist[np.argsort(-ref)[:k]], shortlist[np.argsort(-small)[:k]]
        same_top += int(ref_top[0] == small_top[0])
        kept += len(set(ref_top.tolist()) & set(small_top.tolist())) / k
        kept_one += int(ref_top[0] in set(small_top.tolist()))
    count = len(picks)
    return {"queries": count, "bits": use_bits, "dims": use_dims, "pool": pool, "clip": clip,
            "top1": same_top / count, "best_in_top10": kept_one / count, "recall": kept / count,
            "megabytes": round((packed.nbytes + norms.nbytes) / 1e6, 1),
            "reference_megabytes": round((full.nbytes + ref_norm.nbytes) / 1e6, 1)}


def load_settings(path):
    import json
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
    parser.add_argument("--bits", type=int, default=4, help="bits a dimension in the smaller copy: 4 or 8 (default 4)")
    parser.add_argument("--dims", type=int, default=1024, help="dimensions in the smaller copy; 0 for every dimension (default 1024)")
    parser.add_argument("--queries", type=int, default=500, help="how many chunks to use as questions (default 500)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--clip", type=float, default=None, help="4-bit scaling: root-mean-squares of a row that reach the largest step (default: LOCAL_RESCORE_CLIP, 3.0)")
    parser.add_argument("--sweep", action="store_true", help="try 4 and 8 bits over 1,024 and every dimension, then four 4-bit clips, and print a table")
    args = parser.parse_args()
    loaded = load_settings(args.settings)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from services.config import settings
    from services.local_index import LocalIndexManager

    if not (settings.local_index_connection or os.getenv("BLOB_STORAGE_CONNECTION_STRING")):
        sys.exit("Set LOCAL_INDEX_CONNECTION_STRING or BLOB_STORAGE_CONNECTION_STRING (or use --settings).")
    settings.local_rescore, settings.local_rescore_bits, settings.local_rescore_dims = True, 8, 0   # the exact reference
    print(f"settings loaded from {args.settings}: {loaded} value(s); building the reference index (8 bits, every dimension)")
    manager = LocalIndexManager()
    began = time.perf_counter()
    manager.reload(use_snapshot=False, publish=False)
    index = manager.current()
    if index is None or index.rvec is None:
        sys.exit("The index has no stored vectors to compare (run the backfill with mode all).")
    print(f"{index.n:,} chunks, {index.vec_dims} dimensions, loaded in {time.perf_counter() - began:.0f} s")
    pool = settings.local_rescore_pool
    if args.sweep:
        runs = [(8, 1024, None), (4, 0, None), (4, 1024, None)] + [(4, 1024, c) for c in (2.5, 3.0, 3.5, 4.0)]
        print(f"\n{'bits':>4} {'dims':>5} {'clip':>5} {'MB':>7}  {'same best':>9} {'best in top10':>13} {'top10 kept':>10}   ({pool} shortlist, {args.queries} questions)")
        for bits, dims, clip in runs:
            r = compare(index, bits, dims, args.queries, pool, args.seed, clip=clip)
            print(f"{r['bits']:>4} {r['dims']:>5} {('-' if r['bits'] == 8 else (clip or settings.local_rescore_clip)):>5} {r['megabytes']:>7}  {r['top1']:>9.1%} {r['best_in_top10']:>13.1%} {r['recall']:>10.1%}")
        print("\nPick the smallest row whose numbers you are happy with. LOCAL_RESCORE_BITS and LOCAL_RESCORE_DIMS set bits and dims; "
              "set LOCAL_RESCORE_CLIP for a different clip.")
        return
    r = compare(index, args.bits, args.dims, args.queries, pool, args.seed, clip=args.clip)
    print(f"\n{r['bits']} bits over {r['dims']} dimensions, shortlist of {r['pool']}, {r['queries']} questions:")
    print(f"  same best chunk as the reference     {r['top1']:.1%}")
    print(f"  reference's best chunk in the top 10 {r['best_in_top10']:.1%}")
    print(f"  share of the reference top 10 kept   {r['recall']:.1%}")
    print(f"  vectors in memory                    {r['megabytes']} MB (reference {r['reference_megabytes']} MB)")

if __name__ == "__main__":
    main()
