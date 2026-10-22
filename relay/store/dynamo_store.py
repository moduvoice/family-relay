"""DynamoDB backend: table with string keys pk (HASH) and sk (RANGE); the document is in attribute 'doc' (JSON string).

Storing the document as a JSON string keeps floats and nested lists identical to the
SQLite backend (DynamoDB would otherwise force Decimal conversions).
"""
from __future__ import annotations

import json
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from .. import config


class DynamoStore:
    def __init__(self, table_name: str):
        session = boto3.Session(profile_name=config.AWS_PROFILE) if config.AWS_PROFILE else boto3.Session()
        self._table = session.resource("dynamodb").Table(table_name)

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self._table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return json.loads(item["doc"]) if item else None

    def put(self, pk: str, sk: str, doc: dict[str, Any]) -> None:
        self._table.put_item(Item={"pk": pk, "sk": sk, "doc": json.dumps(doc, ensure_ascii=False)})

    def query(self, pk: str, sk_prefix: str = "") -> list[dict[str, Any]]:
        cond = Key("pk").eq(pk)
        if sk_prefix:
            cond = cond & Key("sk").begins_with(sk_prefix)
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": cond}
        while True:
            resp = self._table.query(**kwargs)
            out.extend(json.loads(i["doc"]) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def delete(self, pk: str, sk: str) -> None:
        self._table.delete_item(Key={"pk": pk, "sk": sk})

    def delete_partition(self, pk: str) -> None:
        resp = self._table.query(KeyConditionExpression=Key("pk").eq(pk), ProjectionExpression="pk, sk")
        with self._table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
