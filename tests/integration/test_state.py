from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from budget_bot.ports import MessageInProgress
from budget_bot.state import LEASE_SECONDS, RETENTION_SECONDS, DynamoMessagingState
from tests.helpers import NOW

pytestmark = pytest.mark.integration


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-2")
        yield resource.create_table(
            TableName="messaging-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def test_crashed_worker_can_be_reclaimed_without_waiting_for_ttl(table: Any) -> None:
    state = DynamoMessagingState(table, clock=lambda: NOW)
    first = state.claim("crashed")
    assert first
    with pytest.raises(MessageInProgress):
        state.claim("crashed")
    later = DynamoMessagingState(table, clock=lambda: NOW + LEASE_SECONDS)
    second = later.claim("crashed")
    assert second and second != first
    # The first worker cannot delete or complete the new owner's claim.
    state.release("crashed", first)
    with pytest.raises(ClientError):
        state.complete("crashed", first)
    later.complete("crashed", second)
    assert later.claim("crashed") is None


def test_completed_message_and_expiry(table: Any) -> None:
    state = DynamoMessagingState(table, clock=lambda: NOW)
    token = state.claim("done")
    assert token
    state.complete("done", token)
    state.release("done", token)  # An uncertain completion must not delete a completed record.
    assert state.claim("done") is None
    assert DynamoMessagingState(table, clock=lambda: NOW + RETENTION_SECONDS).claim("done")


def test_release_allows_immediate_retry(table: Any) -> None:
    state = DynamoMessagingState(table, clock=lambda: NOW)
    token = state.claim("retry")
    assert token
    state.release("retry", token)
    assert state.claim("retry")


def test_legacy_completed_record_is_respected(table: Any) -> None:
    table.put_item(Item={"pk": "MSG#legacy", "ttl": NOW + RETENTION_SECONDS})
    assert DynamoMessagingState(table, clock=lambda: NOW).claim("legacy") is None


def test_daily_report_key_is_claimed_once(table: Any) -> None:
    state = DynamoMessagingState(table, clock=lambda: NOW)
    token = state.claim("report-2026-10-14")
    assert token
    state.complete("report-2026-10-14", token)
    assert state.claim("report-2026-10-14") is None
    assert state.claim("report-2026-10-15")
