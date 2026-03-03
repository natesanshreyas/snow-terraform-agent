"""Azure Service Bus sender — enqueues provisioning requests.

Uses DefaultAzureCredential (managed identity in ACA) against the fully-qualified
hostname rather than a connection string, so no secret is needed on the send path.
"""
from __future__ import annotations

import json
import os

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage


def send_provision_message(
    run_id: str,
    ticket_id: str,
    max_iterations: int = 15,
) -> None:
    """Enqueue a provisioning job on the Azure Service Bus queue."""
    hostname = os.environ["AZURE_SERVICE_BUS_HOSTNAME"]
    queue_name = os.environ["AZURE_SERVICE_BUS_QUEUE_NAME"]

    body = json.dumps({
        "run_id": run_id,
        "ticket_id": ticket_id,
        "max_iterations": max_iterations,
    })

    with ServiceBusClient(hostname, DefaultAzureCredential()) as client:
        with client.get_queue_sender(queue_name) as sender:
            sender.send_messages(ServiceBusMessage(body))
