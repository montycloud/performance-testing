"""KB REST API client and environment config."""
import sys
import threading

import requests

ENVIRONMENTS = {
    "dev1": "dev-api.montycloud.com",
    "dev-eu": "dev-eu-api.montycloud.com",
    "eu": "eu-api.montycloud.com",
    "int1": "int1-api.montycloud.com",
    "stg1": "stg1-api.montycloud.com",
    "prd01": "api.montycloud.com",
}


def base_url_for_env(env):
    host = ENVIRONMENTS.get(env)
    if not host:
        sys.exit(f"Unknown --env '{env}'. Known: {', '.join(ENVIRONMENTS)}. "
                 f"Or pass --base-url to override.")
    return f"https://{host}/knowledgebase/api"


class KBClient:
    """Thin wrapper over the KB REST API. One session per thread."""

    def __init__(self, base_url, token, org_id, timeout):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.org_id = org_id
        self.timeout = timeout
        self._local = threading.local()

    @property
    def session(self):
        """Session per thread (reused for keep-alive)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Authorization": self.token})
            if self.org_id:  # d2oid cookie overrides the token user's org
                s.cookies.set("d2oid", self.org_id)
            self._local.session = s
        return s

    def list_labels(self, page=1, page_size=100, search=None):
        params = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        return self.session.get(f"{self.base_url}/labels", params=params,
                                timeout=self.timeout)

    def create_label(self, name):
        return self.session.post(f"{self.base_url}/labels",
                                 json={"name": name}, timeout=self.timeout)

    def generate_upload_url(self, collection_id, body):
        return self.session.post(
            f"{self.base_url}/collections/{collection_id}/documents/upload_url",
            json=body, timeout=self.timeout)

    def list_documents(self, collection_id, label_id, page):
        return self.session.get(
            f"{self.base_url}/collections/{collection_id}/documents",
            params={"label_ids": label_id, "page": page, "page_size": 100},
            timeout=self.timeout)

    def delete_document(self, collection_id, document_id):
        return self.session.delete(
            f"{self.base_url}/collections/{collection_id}/documents/{document_id}",
            timeout=self.timeout)

    def put_to_s3(self, presigned_url, data):
        return requests.put(presigned_url, data=data, timeout=self.timeout)
