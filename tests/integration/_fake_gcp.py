"""In-memory doubles for Firestore and Cloud Storage.

These model the two behaviours the adapters actually depend on for correctness:

* ``DocumentReference.create`` raises ``AlreadyExists`` when the document exists — the
  atomic precondition the idempotency-key and proof stores are built on.
* ``Blob.upload_from_string(..., if_generation_match=0)`` raises ``PreconditionFailed``
  when the object exists — the write-once precondition evidence immutability is built on.

Anything the adapters do not use is deliberately absent, so a fake that drifts out of
step with the real API fails loudly rather than quietly passing a test that the cloud
would reject. The real semantics are confirmed separately by the cloud smoke tests.
"""

from __future__ import annotations

import copy
from typing import Any

from google.api_core import exceptions as gcloud_exceptions

SERVER_TIMESTAMP = object()


# ============================ Firestore ===============================================


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else copy.deepcopy(self._data)


class FakeDocument:
    def __init__(self, store: dict[str, dict[str, Any]], path: str) -> None:
        self._store = store
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._store, f"{self._path}/{name}")

    def get(self, transaction: Any = None) -> FakeSnapshot:
        return FakeSnapshot(self._store.get(self._path))

    def set(self, data: dict[str, Any]) -> None:
        self._store[self._path] = copy.deepcopy(data)

    def create(self, data: dict[str, Any]) -> None:
        if self._path in self._store:
            raise gcloud_exceptions.AlreadyExists(f"document {self._path} already exists")
        self._store[self._path] = copy.deepcopy(data)

    def delete(self) -> None:
        self._store.pop(self._path, None)


class FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]], path: str) -> None:
        self._store = store
        self._path = path
        self._limit: int | None = None

    def document(self, doc_id: str) -> FakeDocument:
        return FakeDocument(self._store, f"{self._path}/{doc_id}")

    def limit(self, count: int) -> FakeCollection:
        clone = FakeCollection(self._store, self._path)
        clone._limit = count
        return clone

    def stream(self) -> list[FakeSnapshot]:
        prefix = f"{self._path}/"
        # Direct children only — a document id may not contain '/', so anything with a
        # further separator belongs to a subcollection, not to this collection.
        matches = [
            FakeSnapshot(data)
            for path, data in sorted(self._store.items())
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]
        return matches if self._limit is None else matches[: self._limit]


class FakeTransaction:
    """A transaction the real ``@firestore.transactional`` decorator can drive.

    The decorator calls ``_clean_up``/``_begin``/``_commit`` around the wrapped function
    and reads ``_read_only``, ``_max_attempts`` and ``_id``. Those are provided as
    no-ops so the adapter's real, undecorated compare-and-set logic runs unchanged.

    Writes apply immediately rather than buffering until commit. That is weaker than
    Firestore, and it is why the atomicity itself is proven against real Firestore in
    the cloud smoke test rather than here — what this double proves is the conflict
    *rule*, not the isolation guarantee.
    """

    _read_only = False
    _max_attempts = 1

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store
        self._id: object | None = None

    def _clean_up(self) -> None:
        self._id = None

    def _begin(self, retry_id: object = None) -> None:
        self._id = retry_id or object()

    def _commit(self) -> list[Any]:
        return []

    def _rollback(self) -> None:
        self._id = None

    def set(self, ref: FakeDocument, data: dict[str, Any]) -> None:
        ref.set(data)


class FakeFirestoreClient:
    """A Firestore client backed by a flat path -> document mapping."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.documents, name)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self.documents)


# ============================ Cloud Storage ===========================================


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name

    @property
    def _record(self) -> dict[str, Any] | None:
        return self._bucket.objects.get(self.name)

    @property
    def generation(self) -> int | None:
        record = self._record
        return None if record is None else record["generation"]

    @property
    def size(self) -> int | None:
        record = self._record
        return None if record is None else len(record["data"])

    @property
    def content_type(self) -> str | None:
        record = self._record
        return None if record is None else record["content_type"]

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_generation_match: int | None = None,
    ) -> None:
        exists = self.name in self._bucket.objects
        if if_generation_match == 0 and exists:
            raise gcloud_exceptions.PreconditionFailed(
                f"object {self.name} already exists (generation precondition failed)"
            )
        self._bucket.generation += 1
        self._bucket.objects[self.name] = {
            "data": bytes(data),
            "content_type": content_type,
            "generation": self._bucket.generation,
        }

    def download_as_bytes(self) -> bytes:
        record = self._record
        if record is None:
            raise gcloud_exceptions.NotFound(f"object {self.name} not found")
        return record["data"]

    def reload(self) -> None:
        if self._record is None:
            raise gcloud_exceptions.NotFound(f"object {self.name} not found")

    def delete(self) -> None:
        if self.name not in self._bucket.objects:
            raise gcloud_exceptions.NotFound(f"object {self.name} not found")
        del self._bucket.objects[self.name]


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, dict[str, Any]] = {}
        self.generation = 0

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def get_blob(self, name: str) -> FakeBlob | None:
        return FakeBlob(self, name) if name in self.objects else None


class FakeStorageClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))
