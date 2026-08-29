#!/opt/miniconda3/envs/ecom-ai/bin/python
from __future__ import annotations

import os
from urllib.parse import urlparse

from minio import Minio


def main() -> None:
    prefix = os.environ.get("ECOM_OBJECT_STORAGE_BUCKET_PREFIX", "")
    if not prefix.startswith("test-"):
        raise RuntimeError("refusing to clean object storage without a test- bucket prefix")

    endpoint = urlparse(os.environ["ECOM_OBJECT_STORAGE_ENDPOINT"])
    client = Minio(
        endpoint.netloc,
        access_key=os.environ["ECOM_OBJECT_STORAGE_ACCESS_KEY"],
        secret_key=os.environ["ECOM_OBJECT_STORAGE_SECRET_KEY"],
        secure=endpoint.scheme == "https",
    )
    for bucket in client.list_buckets():
        if not bucket.name.startswith(prefix):
            continue
        for item in client.list_objects(bucket.name, recursive=True):
            client.remove_object(bucket.name, item.object_name)
        client.remove_bucket(bucket.name)


if __name__ == "__main__":
    main()
