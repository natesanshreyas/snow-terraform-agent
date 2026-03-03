"""Azure Blob Storage helper for run state persistence.

Reads / writes JSON blobs at runs/{run_id}.json.
Credentials come from DefaultAzureCredential — in ACA the user-assigned
managed identity is selected via the AZURE_CLIENT_ID env var.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient


def _client() -> BlobServiceClient:
    account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    client_id = os.environ.get("AZURE_CLIENT_ID")
    credential = (
        ManagedIdentityCredential(client_id=client_id)
        if client_id
        else DefaultAzureCredential()
    )
    return BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=credential,
    )


def _container() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER_NAME", "runs")


def write_run(run_id: str, data: Dict[str, Any]) -> None:
    """Write (or overwrite) run state blob at runs/{run_id}.json."""
    blob = _client().get_blob_client(container=_container(), blob=f"{run_id}.json")
    blob.upload_blob(json.dumps(data, indent=2, default=str), overwrite=True)


def read_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Read run state blob. Returns None if the blob does not exist."""
    blob = _client().get_blob_client(container=_container(), blob=f"{run_id}.json")
    try:
        stream = blob.download_blob()
        return json.loads(stream.readall())
    except ResourceNotFoundError:
        return None
