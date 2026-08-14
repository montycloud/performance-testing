import datetime
import itertools
import os
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from logger import JsonlLogger, print_tally


def utcnow_iso():
    """Current UTC time as an ISO-8601 string (used to stamp each record)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def timed(call):
    """Run call() and return (its response, elapsed milliseconds)."""
    t0 = time.monotonic()
    resp = call()
    return resp, round((time.monotonic() - t0) * 1000, 1)


def split_ids(raw):
    """Split a comma-separated id string into a clean list of ids."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def load_corpus(corpus_dir, file_type):
    """Return [(ext, size_bytes, path)] for the selected file type."""
    if not os.path.isdir(corpus_dir):
        sys.exit(f"Corpus dir not found: {corpus_dir}. "
                 f"Pass --corpus-dir, or restore the checked-in corpus/ directory.")
    files = []
    for name in sorted(os.listdir(corpus_dir)):
        path = os.path.join(corpus_dir, name)
        if not os.path.isfile(path) or "." not in name:
            continue
        ext = name.rsplit(".", 1)[1].lower()
        if file_type != "random" and ext != file_type.lower():
            continue
        files.append((ext, os.path.getsize(path), path))
    if not files:
        sys.exit(f"No corpus files matching --file-type={file_type} in {corpus_dir}.")
    return files


def new_record(run_id, org_id, collection_id, ext, size):
    """Build a blank result record; the upload steps fill in the rest."""
    return {
        "request_ts": utcnow_iso(),
        "run_id": run_id,
        "worker_id": threading.current_thread().name,
        "org_id": org_id,
        "collection_id": collection_id,
        "document_id": None,
        "file_type": ext,
        "file_size_bytes": size,
        "api_status": None,
        "api_latency_ms": None,
        "s3_status": None,
        "s3_latency_ms": None,
        "error": None,
    }


def upload_body(doc_name, size, ext, run_id, label_id):
    """Assemble the JSON body for the upload_url request (metadata only, no bytes)."""
    return {
        "name": doc_name,
        "size": size,
        "type": ext,
        "document_type": "document",
        "additional_context": f"loadtest run {run_id}",
        "label_ids": [label_id],
    }


def request_upload_url(client, collection_id, body, record):
    resp, record["api_latency_ms"] = timed(
        lambda: client.generate_upload_url(collection_id, body))
    record["api_status"] = resp.status_code
    if resp.status_code != 201:
        record["error"] = f"upload_url: {resp.text[:300]}"
        return None
    data = resp.json().get("data") or {}
    record["document_id"] = data.get("document_id")
    presigned_url = data.get("presigned_url")
    if not presigned_url:
        record["error"] = "upload_url: missing presigned_url in response"
    return presigned_url


def put_file_to_s3(client, presigned_url, path, record):
    with open(path, "rb") as fh:
        payload = fh.read()
    resp, record["s3_latency_ms"] = timed(
        lambda: client.put_to_s3(presigned_url, payload))
    record["s3_status"] = resp.status_code
    if resp.status_code not in (200, 204):
        record["error"] = f"s3_put: HTTP {resp.status_code}"


def do_upload(client, logger, run_id, collection_id, label_id, corpus_file):
    ext, size, path = corpus_file
    doc_name = f"loadtest-{run_id}-{uuid.uuid4().hex[:12]}.{ext}"
    record = new_record(run_id, client.org_id, collection_id, ext, size)
    body = upload_body(doc_name, size, ext, run_id, label_id)
    try:
        presigned_url = request_upload_url(client, collection_id, body, record)
        if presigned_url:
            put_file_to_s3(client, presigned_url, path, record)
    except Exception as e:  # noqa: BLE001 - load tool: record and continue
        record["error"] = f"{type(e).__name__}: {e}"
    finally:
        logger.write(record)
    return record


def preflight(client, run_id):
    print(f"Preflight: GET {client.base_url}/labels ...")
    pre = client.list_labels()
    if pre.status_code != 200:
        sys.exit(f"Preflight failed (HTTP {pre.status_code}): {pre.text[:300]}\n"
                 f"Check --base-url, --token (expired?) and --org-id.")
    label_name = f"loadtest-{run_id}"
    lr = client.create_label(label_name)
    if lr.status_code not in (200, 201):
        sys.exit(f"Could not create run label '{label_name}' "
                 f"(HTTP {lr.status_code}): {lr.text[:300]}")
    label_id = (lr.json().get("data") or {}).get("id")
    return label_name, label_id


def run_batches(client, logger, args, collection_ids, label_id):
    results = []
    col_cycle = itertools.cycle(collection_ids)
    corpus = load_corpus(args.corpus_dir, args.file_type)
    total = args.concurrency * args.batches
    print(f"Firing {total} uploads: {args.batches} batches x "
          f"{args.concurrency} concurrent, {args.batch_delay}s between batches.\n")

    for batch in range(1, args.batches + 1):
        with ThreadPoolExecutor(max_workers=args.concurrency,
                                thread_name_prefix="w") as pool:
            futures = [
                pool.submit(do_upload, client, logger, args.run_id,
                            next(col_cycle), label_id, random.choice(corpus))
                for _ in range(args.concurrency)
            ]
            for fut in as_completed(futures):
                results.append(fut.result())
        print(f"  batch {batch}/{args.batches} done ({len(results)}/{total})")
        if batch < args.batches and args.batch_delay > 0:
            time.sleep(args.batch_delay)
    return results


def run_load(args, client):
    collection_ids = split_ids(args.collection_id)
    label_name, label_id = preflight(client, args.run_id)
    print(f"Run id: {args.run_id}\nRun label: {label_name} ({label_id})\n"
          f"Log file: {args.log_file}")

    logger = JsonlLogger(args.log_file)
    start = time.monotonic()
    try:
        results = run_batches(client, logger, args, collection_ids, label_id)
    finally:
        logger.close()
    print_tally(results, time.monotonic() - start, args.run_id, label_name,
                args.log_file)
