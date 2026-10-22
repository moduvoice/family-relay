"""Key-value document store with the DynamoDB single-table shape (pk, sk -> JSON doc).

SQLite locally, DynamoDB when deployed. The repository (dks.state) only uses
get / put / query(prefix) / delete, so both backends behave the same.
"""
from __future__ import annotations

from typing import Any, Protocol

from .. import config


class Store(Protocol):
    def get(self, pk: str, sk: str) -> dict[str, Any] | None: ...
    def put(self, pk: str, sk: str, doc: dict[str, Any]) -> None: ...
    def query(self, pk: str, sk_prefix: str = "") -> list[dict[str, Any]]: ...
    def delete(self, pk: str, sk: str) -> None: ...
    def delete_partition(self, pk: str) -> None: ...


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        if config.STORE_BACKEND == "dynamodb":
            from .dynamo_store import DynamoStore
            _store = DynamoStore(config.DYNAMODB_TABLE)
        elif config.STORE_BACKEND == "sqlite":
            from .sqlite_store import SqliteStore
            _store = SqliteStore(config.SQLITE_PATH)
        else:
            raise ValueError(f"unknown DKS_STORE={config.STORE_BACKEND!r}")
    return _store
