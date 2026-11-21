"""Split selected tests across up to 8 workers to minimise busiest load + 2 s per worker."""

import heapq

WORKER_MS = 2000
MAX_WORKERS = 8


def cost(workers, ms):
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(ms[t] for t in w) for w in workers) + WORKER_MS * len(workers)


def _lpt(items, ms, k):
    heap = [(0, i) for i in range(k)]
    bins = [[] for _ in range(k)]
    loads = [0] * k
    for t in items:
        load, i = heapq.heappop(heap)
        bins[i].append(t)
        loads[i] = load + ms[t]
        heapq.heappush(heap, (loads[i], i))
    return bins, loads


def _improve(bins, loads, ms):
    """Move or swap tests off the busiest worker while that lowers the maximum."""
    for _ in range(200):
        hi = max(range(len(loads)), key=loads.__getitem__)
        best = None
        for j in range(len(loads)):
            if j == hi:
                continue
            gap = loads[hi] - loads[j]
            for a in bins[hi]:
                d = ms[a]
                if 0 < d < gap and (best is None or max(loads[hi] - d, loads[j] + d) < best[0]):
                    best = (max(loads[hi] - d, loads[j] + d), "move", a, None, j)
                for b in bins[j]:
                    e = ms[b]
                    if 0 < d - e < gap:
                        new = max(loads[hi] - d + e, loads[j] + d - e)
                        if best is None or new < best[0]:
                            best = (new, "swap", a, b, j)
        if best is None or best[0] >= loads[hi]:
            return
        _, kind, a, b, j = best
        bins[hi].remove(a)
        bins[j].append(a)
        loads[hi] -= ms[a]
        loads[j] += ms[a]
        if kind == "swap":
            bins[j].remove(b)
            bins[hi].append(b)
            loads[j] -= ms[b]
            loads[hi] += ms[b]


def split(tests, ms):
    tests = sorted(set(tests), key=lambda t: (-ms[t], t))
    if not tests:
        return []
    best = None
    total = sum(ms[t] for t in tests)
    longest = ms[tests[0]]
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        lower = max(longest, -(-total // k)) + WORKER_MS * k
        if best is not None and lower >= best[0]:
            continue
        bins, loads = _lpt(tests, ms, k)
        if len(tests) <= 400:
            _improve(bins, loads, ms)
        c = max(loads) + WORKER_MS * k
        if best is None or c < best[0]:
            best = (c, [b for b in bins if b])
    return best[1]
