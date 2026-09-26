from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import tracking
from watchlist import load_watchlist, collect_fixed

def main():
    results, rows = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {pool.submit(tracking.collect_platform, q, p): (q, p)
                 for q in tracking.TARGETS for p in tracking.PLATFORMS}
        for target in load_watchlist():
            tasks[pool.submit(collect_fixed, target)] = (target['query'], target['platform'])
        for future in as_completed(tasks):
            q, p = tasks[future]
            try:
                data, status = future.result()
                rows.extend(data)
            except Exception as exc:
                status = {"query":q, "platform":p, "status":"error", "count":0,
                    "checked_at":tracking.now_tw().isoformat(timespec="seconds"),
                    "message": f"{type(exc).__name__}: {str(exc)[:180]}"}
            results.append(status)
            print(json.dumps(status, ensure_ascii=False), flush=True)
    tracking.save_observations(rows)
    report = {"finished_at":tracking.now_tw().isoformat(timespec="seconds"),
              "new_observations":len(rows), "results":results}
    tracking.STATUS.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Saved {len(rows)} actual observations; no cached or simulated values used.")
    # The workflow saves diagnostics even on failure.
    return 1 if any(r["status"]=="error" for r in results) or not rows else 0

if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
