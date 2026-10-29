import json
import os
import threading


class JsonlLogger:
    """Thread-safe append-only JSON-lines writer (one record per line)."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.path = path

    def write(self, record):
        """Append one record as a JSON line (safe to call from many threads)."""
        line = json.dumps(record)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self):
        """Close the underlying log file."""
        self._fh.close()


def print_tally(results, wall_s, run_id, label_name, log_file):
    """Print a run summary: success counts, throughput, and breakdowns by status/type."""
    api_ok = sum(1 for r in results if r["api_status"] == 201)
    s3_ok = sum(1 for r in results if r["s3_status"] in (200, 204))
    full_ok = sum(1 for r in results
                  if r["api_status"] == 201 and r["s3_status"] in (200, 204))
    total = len(results)

    by_status = {}
    by_type = {}
    for r in results:
        by_status[r["api_status"]] = by_status.get(r["api_status"], 0) + 1
        by_type[r["file_type"]] = by_type.get(r["file_type"], 0) + 1

    print("\n" + "=" * 60)
    print("LOAD TEST TALLY (local sanity check only)")
    print("=" * 60)
    print(f"Total requests      : {total}")
    print(f"Upload URL 201      : {api_ok}    failed: {total - api_ok}")
    print(f"S3 PUT 200/204      : {s3_ok}    failed: {total - s3_ok}")
    print(f"Full-flow success   : {full_ok}")
    print(f"Wall clock          : {wall_s:.1f}s "
          f"({total / wall_s:.1f} req/s)" if wall_s else "")
    print(f"By upload_url status: "
          f"{ {str(k): v for k, v in by_status.items()} }")
    print(f"By file type        : {by_type}")
    print(f"Run id              : {run_id}")
    print(f"Run label           : {label_name}")
    print(f"Log file            : {log_file}")
    print("=" * 60)
