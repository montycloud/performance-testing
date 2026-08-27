"""Cleanup mode: delete a previous run's documents by its loadtest-<run_id> label."""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_cleanup(args, client):
    """Delete every document tagged loadtest-<run_id> across the collections."""
    if not args.run_id:
        sys.exit("--cleanup requires --run-id (to resolve the run label loadtest-<run_id>).")
    collection_ids = [c.strip() for c in args.collection_id.split(",") if c.strip()]
    label_name = f"loadtest-{args.run_id}"

    label_id = _resolve_label_id(client, label_name)
    print(f"Cleaning up documents tagged '{label_name}' ({label_id}) "
          f"across {len(collection_ids)} collection(s). Deletion is async.")

    pairs = _collect_docs(client, collection_ids, label_id)
    if not pairs:
        print("No documents found for that label; nothing to delete.")
        return
    deleted = _delete_docs(client, pairs, args.concurrency)
    print(f"Requested deletion of {deleted}/{len(pairs)} document(s).")


def _resolve_label_id(client, label_name):
    """Look up the label id for label_name, or exit if it doesn't exist."""
    page = 1
    while True:
        lr = client.list_labels(page=page, search=label_name)
        if lr.status_code != 200:
            sys.exit(f"Could not list labels (HTTP {lr.status_code}): {lr.text[:300]}")
        labels = lr.json().get("data") or []
        if not labels:
            break
        for label in labels:
            if label.get("name") == label_name:
                return label["id"]
        page += 1
    sys.exit(f"Label '{label_name}' not found for this org; nothing to clean up. "
             f"(If the run used --org-id, pass the same --org-id here.)")


def _collect_docs(client, collection_ids, label_id):
    """Page every collection and return [(collection_id, doc_id)] for the label."""
    pairs = []
    for cid in collection_ids:
        page = 1
        while True:
            resp = client.list_documents(cid, label_id, page)
            if resp.status_code != 200:
                print(f"  list failed for {cid} (HTTP {resp.status_code}); skipping")
                break
            docs = resp.json().get("data") or []
            if not docs:
                break
            pairs.extend((cid, doc["id"]) for doc in docs)
            page += 1
    return pairs


def _delete_docs(client, pairs, concurrency):
    """Delete all (collection_id, doc_id) pairs concurrently; return success count."""
    deleted_count = 0
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="del") as executor:
        tasks = {executor.submit(client.delete_document, collection_id, doc_id): doc_id
                 for collection_id, doc_id in pairs}
        for future in as_completed(tasks):
            doc_id = tasks[future]
            try:
                response = future.result()
            except Exception as error:  # noqa: BLE001 - record and continue
                print(f"  delete {doc_id} -> {type(error).__name__}: {error}")
                continue
            if response.status_code in (200, 202, 204):
                deleted_count += 1
            else:
                print(f"  delete {doc_id} -> HTTP {response.status_code}")
    return deleted_count
