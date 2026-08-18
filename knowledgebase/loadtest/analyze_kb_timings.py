import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze KB lifecycle timings from a log analytics JSON export."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default="Temp/log-analytics-results-2026-08-13.json",
        help="Path to the JSON file to analyze.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "tsv", "csv"],
        default="markdown",
        help="Output format for the results table.",
    )
    return parser.parse_args()


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration(start_ts, end_ts):
    if not start_ts or not end_ts:
        return ""
    seconds = (parse_timestamp(end_ts) - parse_timestamp(start_ts)).total_seconds()
    return f"{seconds:.3f}"


def load_rows(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return build_rows(payload)


def build_rows(payload):
    """Group a list of {"@message": {"data": {...}}} rows into per-document results."""
    by_document = defaultdict(dict)
    for row in payload:
        message = row.get("@message")
        if not isinstance(message, dict):
            continue

        data = message.get("data")
        if not isinstance(data, dict):
            continue

        document_id = data.get("document_id")
        action = data.get("action")
        timestamp = data.get("timestamp")

        if not document_id or not action or not timestamp:
            continue

        document = by_document[document_id]
        document["document_id"] = document_id
        document["organization_id"] = data.get("OrganizationId", "")
        document["collection_id"] = data.get("collection_id", "")
        document["document_type"] = data.get("document_type", "")
        document["document_size"] = str(data.get("document_size", ""))
        document[action] = timestamp

    return [build_result(by_document[document_id]) for document_id in sorted(by_document)]


def build_result(document):
    upload_url_generated = document.get("upload_url_generated", "")
    document_uploaded = document.get("document_uploaded", "")
    metadata_uploaded = document.get("metadata_uploaded", "")
    summarization_started = document.get("summarization_started", "")
    summarization_completed = document.get("summarization_completed", "")
    kb_ingestion_started = document.get("kb_ingestion_started", "")
    kb_ingestion_completed = document.get("kb_ingestion_completed", "")

    return {
        "document_id": document.get("document_id", ""),
        "organization_id": document.get("organization_id", ""),
        "collection_id": document.get("collection_id", ""),
        "document_type": document.get("document_type", ""),
        "document_size": document.get("document_size", ""),
        "upload_url_generated": upload_url_generated,
        "document_uploaded": document_uploaded,
        "metadata_uploaded": metadata_uploaded,
        "summarization_started": summarization_started,
        "summarization_completed": summarization_completed,
        "kb_ingestion_started": kb_ingestion_started,
        "kb_ingestion_completed": kb_ingestion_completed,
        "s3_upload_time_s": format_duration(upload_url_generated, document_uploaded),
        "metadata_creation_time_s": format_duration(document_uploaded, metadata_uploaded),
        "summarization_time_s": format_duration(
            summarization_started, summarization_completed
        ),
        "kb_ingestion_time_s": format_duration(
            kb_ingestion_started, kb_ingestion_completed
        ),
    }


def render_markdown(rows, headers):
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        print("| " + " | ".join(row[header] for header in headers) + " |")


def render_tsv(rows, headers):
    print("\t".join(headers))
    for row in rows:
        print("\t".join(row[header] for header in headers))


def render_csv(rows, headers):
    writer = csv.writer(sys.stdout)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row[header] for header in headers])


def main():
    args = parse_args()
    results = load_rows(args.input_file)
    headers = [
        "document_id",
        "organization_id",
        "collection_id",
        "document_type",
        "document_size",
        "s3_upload_time_s",
        "metadata_creation_time_s",
        "summarization_time_s",
        "kb_ingestion_time_s",
    ]

    if args.format == "markdown":
        render_markdown(results, headers)
    elif args.format == "csv":
        render_csv(results, headers)
    else:
        render_tsv(results, headers)


if __name__ == "__main__":
    main()