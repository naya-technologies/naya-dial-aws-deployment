from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.signers import RequestSigner


ENVIRONMENT_NAME = os.environ.get("ENVIRONMENT_NAME", "")
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "")
CONFIG_HASH = os.environ.get("CONFIG_HASH", "")
STATE_TABLE_NAME = os.environ.get("STATE_TABLE_NAME", "")
LOCK_LEASE_SECONDS = int(os.environ.get("LOCK_LEASE_SECONDS", "300"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "30"))
CONFIG_REFRESH_PROTOCOL_VERSION = 1
CONFIG_REFRESH_LEASE_SECONDS = 7200
AWAKE_BASELINE_VERSION = 1
CONFIG_REFRESH_FIELDS = (
    "configRefreshId",
    "configRefreshFromHash",
    "configRefreshToHash",
    "configRefreshExpiresAt",
)

TERMINAL_NODEGROUP_STATUSES = {"CREATE_FAILED", "DELETE_FAILED", "DEGRADED"}
TRANSIENT_AWS_ERRORS = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "ServiceUnavailable",
    "InternalFailure",
    "InternalServerError",
    "LimitExceededException",
    "ProvisionedThroughputExceededException",
    "ResourceInUseException",
    "ServerException",
    "SlowDown",
}

_CONFIG_CACHE: dict[str, Any] | None = None
_KUBE_CACHE: "KubeClient | None" = None


class ControllerError(RuntimeError):
    pass


class LostLockError(ControllerError):
    pass


class StaleGenerationError(ControllerError):
    pass


class RetryableControllerError(ControllerError):
    pass


@dataclass
class KubeApiError(ControllerError):
    status: int
    body: str

    def __str__(self) -> str:
        return f"Kubernetes API returned HTTP {self.status}: {self.body[:300]}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _now() -> int:
    return int(time.time())


def _log(level: str, event: str, message: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "level": level,
        "event": event,
        "message": message,
        **fields,
    }
    print(json.dumps(_json_safe(payload), sort_keys=True, default=str))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _invocation_log_context(
    event: Mapping[str, Any], context: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "environment": ENVIRONMENT_NAME or event.get("environment", ""),
        "operation": event.get("operation"),
        "configHash": CONFIG_HASH,
    }
    for name in (
        "executionArn",
        "desiredState",
        "outcome",
        "refreshId",
        "fromConfigHash",
        "toConfigHash",
    ):
        value = event.get(name)
        if value is not None:
            fields[name] = value
    command_input = event.get("input")
    if event.get("operation") == "command" and isinstance(command_input, Mapping):
        fields["source"] = command_input.get("source", "manual")
    request_id = getattr(context, "aws_request_id", None)
    if request_id:
        fields["requestId"] = request_id
    return fields


def _observed_aws_log_summary(observed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rdsStatus": observed["rds"].get("Status"),
        "nodeGroups": {
            name: {
                "status": group.get("status"),
                "scaling": group.get("scaling", {}),
                "autoScalingGroups": {
                    asg_name: observed["asgs"].get(asg_name, {})
                    for asg_name in group.get("autoScalingGroups", [])
                },
            }
            for name, group in observed["groups"].items()
        },
    }


def _aws_error_details(exc: ClientError) -> dict[str, str]:
    response = exc.response or {}
    error = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    return {
        "awsOperation": str(getattr(exc, "operation_name", "unknown")),
        "awsErrorCode": str(error.get("Code", "Unknown")),
        "awsErrorMessage": str(error.get("Message", ""))[:500],
        "awsRequestId": str(metadata.get("RequestId", "")),
    }


def _is_transient_failure(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        response = exc.response or {}
        code = response.get("Error", {}).get("Code")
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in TRANSIENT_AWS_ERRORS or status == 429 or (
            isinstance(status, int) and status >= 500
        )
    return isinstance(
        exc,
        (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
            TimeoutError,
            urllib.error.URLError,
        ),
    )


def _required_environment() -> None:
    required = {
        "ENVIRONMENT_NAME": ENVIRONMENT_NAME,
        "CONFIG_BUCKET": CONFIG_BUCKET,
        "CONFIG_KEY": CONFIG_KEY,
        "CONFIG_HASH": CONFIG_HASH,
        "STATE_TABLE_NAME": STATE_TABLE_NAME,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ControllerError(f"missing Lambda environment: {', '.join(missing)}")


def _load_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    _required_environment()
    response = boto3.client("s3").get_object(Bucket=CONFIG_BUCKET, Key=CONFIG_KEY)
    raw = response["Body"].read()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != CONFIG_HASH:
        raise ControllerError(
            f"configuration digest mismatch: got {actual_hash}, expected {CONFIG_HASH}"
        )
    config = json.loads(raw)
    if config.get("environment") != ENVIRONMENT_NAME:
        raise ControllerError("configuration environment does not match the stack")
    _CONFIG_CACHE = config
    return config


def _table():
    _required_environment()
    return boto3.resource("dynamodb").Table(STATE_TABLE_NAME)


def _get_state() -> dict[str, Any]:
    result = _table().get_item(
        Key={"environment": ENVIRONMENT_NAME}, ConsistentRead=True
    )
    return result.get("Item", {"environment": ENVIRONMENT_NAME})


def _set_fields(
    fields: Mapping[str, Any],
    *,
    owner: str | None = None,
    expected_generation: int | None = None,
    remove: Iterable[str] = (),
) -> None:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    assignments: list[str] = []
    removals: list[str] = []
    for index, (name, value) in enumerate(fields.items()):
        name_key = f"#n{index}"
        value_key = f":v{index}"
        names[name_key] = name
        values[value_key] = value
        assignments.append(f"{name_key} = {value_key}")
    offset = len(names)
    for index, name in enumerate(remove, start=offset):
        name_key = f"#n{index}"
        names[name_key] = name
        removals.append(name_key)
    parts: list[str] = []
    if assignments:
        parts.append("SET " + ", ".join(assignments))
    if removals:
        parts.append("REMOVE " + ", ".join(removals))
    kwargs: dict[str, Any] = {
        "Key": {"environment": ENVIRONMENT_NAME},
        "UpdateExpression": " ".join(parts),
        "ExpressionAttributeNames": names,
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    conditions: list[str] = []
    if owner:
        names["#lockOwner"] = "lockOwner"
        values[":lockOwner"] = owner
        conditions.append("#lockOwner = :lockOwner")
    if expected_generation is not None:
        names["#desiredGeneration"] = "desiredGeneration"
        values[":expectedGeneration"] = expected_generation
        conditions.append("#desiredGeneration = :expectedGeneration")
    if conditions:
        kwargs["ConditionExpression"] = " AND ".join(conditions)
        kwargs["ExpressionAttributeValues"] = values
    try:
        _table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            current = _get_state()
            if owner and current.get("lockOwner") != owner:
                raise LostLockError(
                    "this execution no longer owns the environment lock"
                ) from exc
            if expected_generation is not None and int(
                current.get("desiredGeneration", -1)
            ) != int(expected_generation):
                raise StaleGenerationError(
                    "desired state changed during this reconciliation step"
                ) from exc
            raise ControllerError("conditional state update failed") from exc
        raise


def _result(
    status: str,
    phase: str,
    message: str,
    *,
    wait_seconds: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "phase": phase,
        "message": message,
    }
    if wait_seconds is not None:
        payload["waitSeconds"] = wait_seconds
    if details:
        payload["details"] = dict(details)
    return payload


def _validate_event_environment(event: Mapping[str, Any]) -> None:
    if event.get("environment") != ENVIRONMENT_NAME:
        raise ControllerError("event environment does not match the fixed stack environment")


def _execution_order_key(event: Mapping[str, Any], execution_arn: str) -> tuple[str, int]:
    raw = event.get("executionStartedAt")
    if not isinstance(raw, str) or not raw:
        raise ControllerError("executionStartedAt is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError("executionStartedAt must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ControllerError("executionStartedAt must include a timezone")
    started_micros = int(parsed.timestamp() * 1_000_000)
    return f"{started_micros:020d}:{execution_arn}", started_micros // 1_000_000


def _try_acquire(execution_arn: str) -> dict[str, Any]:
    now = _now()
    try:
        _table().update_item(
            Key={"environment": ENVIRONMENT_NAME},
            UpdateExpression=(
                "SET #owner = :owner, #expires = :expires, "
                "#actual = if_not_exists(#actual, :unknown), #phase = :phase"
            ),
            ConditionExpression=(
                "(attribute_not_exists(#owner) OR #expires < :now OR #owner = :owner)"
            ),
            ExpressionAttributeNames={
                "#owner": "lockOwner",
                "#expires": "lockExpiresAt",
                "#actual": "actualState",
                "#phase": "phase",
            },
            ExpressionAttributeValues={
                ":owner": execution_arn,
                ":expires": now + LOCK_LEASE_SECONDS,
                ":now": now,
                ":unknown": "UNKNOWN",
                ":phase": "COMMAND_ACCEPTED",
            },
        )
        return _result("OWNER", "COMMAND_ACCEPTED", "environment lock acquired")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return _result(
            "JOINED",
            "WAITING_FOR_OWNER",
            "another execution owns the environment lock",
            wait_seconds=RETRY_SECONDS,
        )


def _refresh_owner(refresh_id: str) -> str:
    return f"config-refresh:{refresh_id}"


def _active_config_refresh(state: Mapping[str, Any]) -> bool:
    refresh_id = state.get("configRefreshId")
    expires = state.get("configRefreshExpiresAt")
    return (
        isinstance(refresh_id, str)
        and bool(refresh_id)
        and isinstance(expires, (int, Decimal))
        and int(expires) >= _now()
    )


def _clear_expired_config_refresh(state: Mapping[str, Any]) -> None:
    refresh_id = state.get("configRefreshId")
    if not isinstance(refresh_id, str) or not refresh_id:
        return
    expires = int(state.get("configRefreshExpiresAt", 0))
    if expires >= _now():
        return
    owner = _refresh_owner(refresh_id)
    names = {
        "#refresh": "configRefreshId",
        "#refreshExpires": "configRefreshExpiresAt",
        "#owner": "lockOwner",
        "#lockExpires": "lockExpiresAt",
        "#heartbeat": "lockHeartbeatAt",
        "#from": "configRefreshFromHash",
        "#to": "configRefreshToHash",
    }
    try:
        _table().update_item(
            Key={"environment": ENVIRONMENT_NAME},
            UpdateExpression=(
                "REMOVE #refresh, #refreshExpires, #owner, #lockExpires, "
                "#heartbeat, #from, #to"
            ),
            ConditionExpression=(
                "#refresh = :refresh AND #refreshExpires < :now AND #owner = :owner"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                ":refresh": refresh_id,
                ":now": _now(),
                ":owner": owner,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _config_refresh_prepare_blockers(config: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    observed = _observe_aws(config)
    kube = _kube(config, observed["cluster"])
    blockers.extend(_runtime_sleep_safety_blockers(config, kube))
    if observed["rds"].get("Status") != "available":
        blockers.append(f"aurora:{observed['rds'].get('Status')}")

    nodes_by_group = _nodes_by_group(kube)
    for group in config["eks"]["nodeGroups"]:
        live = observed["groups"][group["name"]]
        scaling = live["scaling"]
        desired_size = scaling.get("desiredSize")
        if live["status"] != "ACTIVE":
            blockers.append(f"nodegroup:{group['name']}:status:{live['status']}")
        if not isinstance(desired_size, int) or desired_size < 1:
            blockers.append(f"nodegroup:{group['name']}:zero")
        nodes = nodes_by_group.get(group["name"], [])
        if not any(_node_ready(node) for node in nodes):
            blockers.append(f"nodegroup:{group['name']}:ready")
        if any(node.get("spec", {}).get("unschedulable", False) for node in nodes):
            blockers.append(f"nodegroup:{group['name']}:cordoned")

    autoscaler_config = config["kubernetes"]["autoscaler"]
    if autoscaler_config["enabled"]:
        autoscaler = _autoscaler_status(config, kube)
        if autoscaler["desired"] < 1:
            blockers.append("autoscaler:desired")
        if autoscaler["available"] < 1:
            blockers.append("autoscaler:available")
    return sorted(set(blockers))


def _config_refresh_acceptance_blockers(config: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    observed = _observe_aws(config)
    kube = _kube(config, observed["cluster"])
    blockers.extend(_runtime_sleep_safety_blockers(config, kube))
    if observed["rds"].get("Status") != "available":
        blockers.append(f"aurora:{observed['rds'].get('Status')}")
    nodes_by_group = _nodes_by_group(kube)
    for group in config["eks"]["nodeGroups"]:
        live = observed["groups"][group["name"]]
        scaling = live["scaling"]
        minimum = scaling.get("minSize")
        desired_size = scaling.get("desiredSize")
        maximum = scaling.get("maxSize")
        if live["status"] != "ACTIVE":
            blockers.append(f"nodegroup:{group['name']}:status:{live['status']}")
        if not isinstance(minimum, int) or minimum < 1:
            blockers.append(f"nodegroup:{group['name']}:min")
        if not isinstance(maximum, int) or not isinstance(minimum, int) or maximum < minimum:
            blockers.append(f"nodegroup:{group['name']}:max")
        if (
            not isinstance(desired_size, int)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not minimum <= desired_size <= maximum
        ):
            blockers.append(f"nodegroup:{group['name']}:desired")
        nodes = nodes_by_group.get(group["name"], [])
        if not isinstance(minimum, int) or sum(
            1 for node in nodes if _node_ready(node)
        ) < minimum:
            blockers.append(f"nodegroup:{group['name']}:ready")
        if any(node.get("spec", {}).get("unschedulable", False) for node in nodes):
            blockers.append(f"nodegroup:{group['name']}:cordoned")
    autoscaler_config = config["kubernetes"]["autoscaler"]
    if autoscaler_config["enabled"]:
        autoscaler = _autoscaler_status(config, kube)
        if autoscaler["desired"] < 1:
            blockers.append("autoscaler:desired")
        if autoscaler["available"] < 1:
            blockers.append("autoscaler:available")
    blockers.extend(_workloads_ready(config, kube))
    blockers.extend(_http_health(config))
    return sorted(set(blockers))


def _validate_refresh_event(event: Mapping[str, Any]) -> tuple[str, str, str]:
    refresh_id = event.get("refreshId")
    from_hash = event.get("fromConfigHash")
    to_hash = event.get("toConfigHash")
    if not isinstance(refresh_id, str) or not refresh_id or len(refresh_id) > 128:
        raise ControllerError("refreshId must be a non-empty string of at most 128 characters")
    for name, value in (("fromConfigHash", from_hash), ("toConfigHash", to_hash)):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ControllerError(f"{name} must be a lowercase SHA-256 digest")
    return refresh_id, from_hash, to_hash


def _handle_prepare_config_refresh(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    config = _load_config()
    refresh_id, from_hash, to_hash = _validate_refresh_event(event)
    if from_hash != CONFIG_HASH:
        raise ControllerError("refresh source hash does not match the active controller config")
    if to_hash == from_hash:
        return _result("SUCCEEDED", "CONFIG_UNCHANGED", "configuration is already active")

    state = _get_state()
    if (
        state.get("configRefreshId") == refresh_id
        and state.get("configRefreshFromHash") == from_hash
        and state.get("configRefreshToHash") == to_hash
        and _active_config_refresh(state)
    ):
        return _result(
            "OWNER",
            "CONFIG_REFRESH_PREPARED",
            "configuration refresh lease is already owned by this request",
            details={"refreshId": refresh_id},
        )
    if _active_config_refresh(state):
        return _result(
            "BLOCKED",
            "CONFIG_REFRESH_BUSY",
            "another configuration refresh is in progress",
            details={"refreshId": state.get("configRefreshId")},
        )
    _clear_expired_config_refresh(state)
    blockers = _config_refresh_prepare_blockers(config)
    if blockers:
        return _result(
            "BLOCKED",
            "CONFIG_REFRESH_REQUIRES_AWAKE",
            "configuration refresh requires a stable awake environment",
            details={"blockers": blockers[:25], "blockerCount": len(blockers)},
        )

    now = _now()
    expires = now + CONFIG_REFRESH_LEASE_SECONDS
    owner = _refresh_owner(refresh_id)
    try:
        _table().update_item(
            Key={"environment": ENVIRONMENT_NAME},
            UpdateExpression=(
                "SET #owner = :owner, #lockExpires = :expires, #heartbeat = :now, "
                "#refresh = :refresh, #from = :from, #to = :to, "
                "#refreshExpires = :expires"
            ),
            ConditionExpression=(
                "(attribute_not_exists(#owner) OR #lockExpires < :now) AND "
                "(attribute_not_exists(#refresh) OR #refreshExpires < :now)"
            ),
            ExpressionAttributeNames={
                "#owner": "lockOwner",
                "#lockExpires": "lockExpiresAt",
                "#heartbeat": "lockHeartbeatAt",
                "#refresh": "configRefreshId",
                "#from": "configRefreshFromHash",
                "#to": "configRefreshToHash",
                "#refreshExpires": "configRefreshExpiresAt",
            },
            ExpressionAttributeValues={
                ":owner": owner,
                ":expires": expires,
                ":now": now,
                ":refresh": refresh_id,
                ":from": from_hash,
                ":to": to_hash,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return _result(
            "BLOCKED",
            "CONFIG_REFRESH_BUSY",
            "an environment operation acquired the lock before refresh",
        )
    return _result(
        "OWNER",
        "CONFIG_REFRESH_PREPARED",
        "configuration refresh lease acquired",
        details={"refreshId": refresh_id, "expiresAt": expires},
    )


def _handle_validate_config_refresh(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    config = _load_config()
    refresh_id, from_hash, to_hash = _validate_refresh_event(event)
    if CONFIG_HASH != to_hash:
        raise ControllerError("refresh target hash is not active in the controller")
    state = _get_state()
    if (
        state.get("configRefreshId") != refresh_id
        or state.get("configRefreshFromHash") != from_hash
        or state.get("configRefreshToHash") != to_hash
        or state.get("lockOwner") != _refresh_owner(refresh_id)
    ):
        raise ControllerError("configuration refresh lease does not match this request")
    blockers = _config_refresh_acceptance_blockers(config)
    if blockers:
        return _result(
            "BLOCKED",
            "CONFIG_REFRESH_VALIDATION_FAILED",
            "new configuration did not pass awake acceptance",
            details={"blockers": blockers[:25], "blockerCount": len(blockers)},
        )
    return _result(
        "SUCCEEDED",
        "CONFIG_REFRESH_VALIDATED",
        "new configuration passed awake acceptance",
        details={"refreshId": refresh_id},
    )


def _handle_complete_config_refresh(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    _load_config()
    refresh_id, from_hash, to_hash = _validate_refresh_event(event)
    if CONFIG_HASH != to_hash:
        raise ControllerError("refresh target hash is not active in the controller")
    state = _get_state()
    if not state.get("configRefreshId") and state.get("configHash") == to_hash:
        return _result("SUCCEEDED", "CONFIG_REFRESH_ALREADY_COMPLETED", "configuration refresh is already complete")
    owner = _refresh_owner(refresh_id)
    if (
        state.get("configRefreshId") != refresh_id
        or state.get("configRefreshFromHash") != from_hash
        or state.get("configRefreshToHash") != to_hash
        or state.get("lockOwner") != owner
    ):
        raise ControllerError("configuration refresh lease does not match this request")
    _set_fields(
        {
            "configHash": to_hash,
            "lastConfigRefreshAt": _now(),
            "lastConfigRefreshOutcome": "SUCCEEDED",
        },
        owner=owner,
        remove=(*CONFIG_REFRESH_FIELDS, "lockOwner", "lockExpiresAt", "lockHeartbeatAt"),
    )
    return _result(
        "SUCCEEDED",
        "CONFIG_REFRESH_COMPLETED",
        "configuration refresh completed and the environment lock was released",
        details={"refreshId": refresh_id},
    )


def _handle_abort_config_refresh(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    _load_config()
    refresh_id, from_hash, to_hash = _validate_refresh_event(event)
    state = _get_state()
    if not state.get("configRefreshId"):
        return _result("SUCCEEDED", "CONFIG_REFRESH_NOT_ACTIVE", "configuration refresh is not active")
    owner = _refresh_owner(refresh_id)
    if (
        state.get("configRefreshId") != refresh_id
        or state.get("configRefreshFromHash") != from_hash
        or state.get("configRefreshToHash") != to_hash
        or state.get("lockOwner") != owner
    ):
        raise ControllerError("configuration refresh lease does not match this request")
    if CONFIG_HASH not in {from_hash, to_hash}:
        raise ControllerError("active controller hash is outside the refresh boundary")
    _set_fields(
        {
            "configHash": CONFIG_HASH,
            "lastConfigRefreshAt": _now(),
            "lastConfigRefreshOutcome": "ABORTED",
        },
        owner=owner,
        remove=(*CONFIG_REFRESH_FIELDS, "lockOwner", "lockExpiresAt", "lockHeartbeatAt"),
    )
    return _result(
        "SUCCEEDED",
        "CONFIG_REFRESH_ABORTED",
        "configuration refresh lease was released without further changes",
        details={"refreshId": refresh_id, "activeConfigHash": CONFIG_HASH},
    )


def _handle_command(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    config = _load_config()
    state = _get_state()
    if _active_config_refresh(state):
        return _result(
            "REFRESHING",
            "CONFIG_REFRESH_IN_PROGRESS",
            "configuration refresh is in progress; command will retry",
            wait_seconds=RETRY_SECONDS,
            details={"refreshId": state.get("configRefreshId")},
        )
    _clear_expired_config_refresh(state)
    desired = event.get("desiredState")
    if desired not in {"AWAKE", "ASLEEP"}:
        raise ControllerError("desiredState must be AWAKE or ASLEEP")
    execution_arn = event.get("executionArn")
    if not isinstance(execution_arn, str) or not execution_arn:
        raise ControllerError("executionArn is required")
    command_order_key, command_started_at = _execution_order_key(
        event, execution_arn
    )
    command_input = event.get("input") or {}
    if not isinstance(command_input, dict):
        raise ControllerError("execution input must be an object")
    allowed_input = {"source", "keepAwakeSeconds"}
    unknown = sorted(set(command_input) - allowed_input)
    if unknown:
        raise ControllerError(
            "unsupported execution input fields: " + ", ".join(unknown)
        )
    source = command_input.get("source", "manual")
    if source not in {"manual", "schedule"}:
        raise ControllerError("source must be manual or schedule")
    keep_awake = command_input.get(
        "keepAwakeSeconds", config["controller"]["manualWakeHoldSeconds"]
    )
    if (
        isinstance(keep_awake, bool)
        or not isinstance(keep_awake, int)
        or keep_awake < 0
        or keep_awake > 604800
    ):
        raise ControllerError("keepAwakeSeconds must be an integer between 0 and 604800")
    now = _now()

    names = {
        "#desired": "desiredState",
        "#generation": "desiredGeneration",
        "#source": "commandSource",
        "#started": "operationStartedAt",
        "#hash": "configHash",
        "#error": "lastError",
        "#details": "lastDetails",
        "#commandArn": "lastCommandExecutionArn",
        "#commandOrder": "lastCommandOrderKey",
    }
    values = {
        ":desired": desired,
        ":source": source,
        ":started": now,
        ":hash": CONFIG_HASH,
        ":empty": "",
        ":commandArn": execution_arn,
        ":commandOrder": command_order_key,
    }
    kwargs: dict[str, Any] = {
        "Key": {"environment": ENVIRONMENT_NAME},
        "UpdateExpression": "",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    assignments = [
        "#desired = :desired",
        "#source = :source",
        "#started = :started",
        "#hash = :hash",
        "#error = :empty",
        "#details = :empty",
        "#commandArn = :commandArn",
        "#commandOrder = :commandOrder",
    ]
    if not (source == "schedule" and desired == "AWAKE"):
        names["#override"] = "overrideUntil"
        values[":override"] = (
            now + keep_awake if desired == "AWAKE" and source == "manual" else 0
        )
        assignments.append("#override = :override")
    conditions = [
        "(attribute_not_exists(#commandOrder) OR #commandOrder < :commandOrder)",
    ]
    if source == "schedule" and desired == "ASLEEP":
        values[":now"] = now
        conditions.append("(attribute_not_exists(#override) OR #override <= :now)")
    kwargs["ConditionExpression"] = " AND ".join(conditions)
    values[":started"] = command_started_at
    table = _table()
    try:
        same_desired_kwargs = dict(kwargs)
        same_desired_kwargs["UpdateExpression"] = "SET " + ", ".join(assignments)
        same_desired_kwargs["ConditionExpression"] = " AND ".join(
            [*conditions, "#desired = :desired", "attribute_exists(#generation)"]
        )
        updated = table.update_item(**same_desired_kwargs)["Attributes"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        state_change_kwargs = dict(kwargs)
        state_change_kwargs["UpdateExpression"] = (
            "SET " + ", ".join(assignments) + " ADD #generation :one"
        )
        state_change_kwargs["ExpressionAttributeValues"] = {
            **values,
            ":one": 1,
        }
        try:
            updated = table.update_item(**state_change_kwargs)["Attributes"]
        except ClientError as state_change_exc:
            if (
                state_change_exc.response.get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise
            current = _get_state()
            if current.get("lastCommandExecutionArn") == execution_arn:
                updated = current
            elif (
                source == "schedule"
                and desired == "ASLEEP"
                and int(current.get("overrideUntil", 0)) > now
            ):
                return _result(
                    "SKIPPED",
                    "KEEP_AWAKE",
                    "scheduled sleep skipped because a manual keep-awake window is active",
                )
            else:
                return _result(
                    "SUPERSEDED",
                    "NEWER_COMMAND_EXISTS",
                    "a newer environment command already exists",
                )
    acquired = _try_acquire(execution_arn)
    acquired["message"] = f"desired state is now {desired}; {acquired['message']}"
    acquired["details"] = {"generation": int(updated.get("desiredGeneration", 0))}
    return acquired


def _handle_acquire(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    owner = event.get("executionArn")
    if not isinstance(owner, str) or not owner:
        raise ControllerError("acquire requires executionArn")
    return _try_acquire(owner)


def _renew_lock(owner: str) -> dict[str, Any]:
    now = _now()
    try:
        response = _table().update_item(
            Key={"environment": ENVIRONMENT_NAME},
            UpdateExpression="SET #expires = :expires, #heartbeat = :heartbeat",
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={
                "#owner": "lockOwner",
                "#expires": "lockExpiresAt",
                "#heartbeat": "lockHeartbeatAt",
            },
            ExpressionAttributeValues={
                ":owner": owner,
                ":expires": now + LOCK_LEASE_SECONDS,
                ":heartbeat": now,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise LostLockError("this execution no longer owns the environment lock") from exc
        raise
    return response["Attributes"]


def _record_result(
    owner: str,
    result: Mapping[str, Any],
    actual_state: str,
    generation: int,
) -> None:
    state = _get_state()
    now = _now()
    fields: dict[str, Any] = {
        "actualState": actual_state,
        "phase": result["phase"],
        "lastMessage": result["message"],
        "observedAt": now,
        "lockExpiresAt": now + LOCK_LEASE_SECONDS,
        "reconciledGeneration": generation,
        "lastWorkerStatus": result["status"],
        "lastDetails": json.dumps(
            _json_safe(result.get("details", {})),
            sort_keys=True,
            separators=(",", ":"),
        )[:4000],
    }
    if state.get("phase") != result["phase"]:
        fields["phaseStartedAt"] = now
    if result["status"] in {"FAILED", "BLOCKED"}:
        fields["lastError"] = result["message"][:1000]
    _set_fields(
        fields,
        owner=owner,
        expected_generation=generation,
    )


def _handle_release(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    owner = event.get("executionArn")
    outcome = event.get("outcome")
    if not isinstance(owner, str) or outcome not in {"SUCCEEDED", "BLOCKED", "FAILED"}:
        raise ControllerError("release requires executionArn and a supported outcome")
    state = _get_state()
    desired_generation = int(state.get("desiredGeneration", -1))
    if int(state.get("reconciledGeneration", -2)) != desired_generation:
        return _result(
            "RETRY",
            "DESIRED_STATE_CHANGED",
            "desired state changed before lock release",
            wait_seconds=1,
        )
    try:
        _set_fields(
            {"lastOutcome": outcome, "operationCompletedAt": _now()},
            owner=owner,
            expected_generation=desired_generation,
            remove=("lockOwner", "lockExpiresAt", "lockHeartbeatAt"),
        )
    except StaleGenerationError:
        return _result(
            "RETRY",
            "DESIRED_STATE_CHANGED",
            "desired state changed during lock release",
            wait_seconds=1,
        )
    except LostLockError:
        return _result("SUCCEEDED", "LOCK_ALREADY_RELEASED", "lock is no longer owned")
    return _result("SUCCEEDED", "LOCK_RELEASED", "environment lock released")


def _handle_finalize_failure(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    owner = event.get("executionArn")
    if not isinstance(owner, str):
        raise ControllerError("finalizeFailure requires executionArn")
    state = _get_state()
    if state.get("lockOwner") != owner:
        return _result(
            "SUCCEEDED",
            "LOCK_NOT_OWNED",
            "failure belongs to an execution that does not own the lock",
        )
    if state.get("lastCommandExecutionArn") != owner:
        return _result(
            "RETRY",
            "DESIRED_STATE_CHANGED",
            "a newer command must be reconciled before releasing the lock",
            wait_seconds=1,
        )
    generation = int(state.get("desiredGeneration", -1))
    desired = state.get("desiredState")
    actual = "ERROR_WAKING" if desired == "AWAKE" else "ERROR_SLEEPING"
    error = json.dumps(event.get("error", {}), sort_keys=True)[:1000]
    terminal_recorded = (
        int(state.get("reconciledGeneration", -2)) == generation
        and state.get("lastWorkerStatus") in {"SUCCEEDED", "BLOCKED", "FAILED"}
    )
    try:
        if terminal_recorded:
            _set_fields(
                {
                    "lastError": error,
                    "lastOutcome": "FAILED",
                    "operationCompletedAt": _now(),
                },
                owner=owner,
                expected_generation=generation,
                remove=("lockOwner", "lockExpiresAt", "lockHeartbeatAt"),
            )
            return _result(
                "SUCCEEDED",
                "TERMINAL_RESULT_PRESERVED",
                "terminal environment state was preserved and the lock was released",
            )
        _set_fields(
            {
                "actualState": actual,
                "phase": "UNHANDLED_FAILURE",
                "lastError": error,
                "lastOutcome": "FAILED",
                "operationCompletedAt": _now(),
                "reconciledGeneration": generation,
            },
            owner=owner,
            expected_generation=generation,
            remove=("lockOwner", "lockExpiresAt", "lockHeartbeatAt"),
        )
    except StaleGenerationError:
        return _result(
            "RETRY",
            "DESIRED_STATE_CHANGED",
            "desired state changed while failure was being recorded",
            wait_seconds=1,
        )
    except LostLockError:
        pass
    return _result("SUCCEEDED", "FAILURE_RECORDED", "failure state recorded")


def _aws_clients(config: Mapping[str, Any]) -> dict[str, Any]:
    region = config["aws"]["region"]
    return {
        "sts": boto3.client("sts", region_name=region),
        "eks": boto3.client("eks", region_name=region),
        "rds": boto3.client("rds", region_name=region),
        "autoscaling": boto3.client("autoscaling", region_name=region),
    }


def _observe_aws(config: Mapping[str, Any]) -> dict[str, Any]:
    clients = _aws_clients(config)
    identity = clients["sts"].get_caller_identity()
    if identity.get("Account") != config["aws"]["accountId"]:
        raise ControllerError("Lambda role resolved to the wrong AWS account")
    cluster = clients["eks"].describe_cluster(name=config["eks"]["clusterName"])[
        "cluster"
    ]
    if cluster.get("arn") != config["eks"]["clusterArn"]:
        raise ControllerError("live EKS cluster ARN does not match configuration")
    if cluster.get("status") != "ACTIVE":
        raise ControllerError(f"EKS cluster is {cluster.get('status')}, expected ACTIVE")
    auth_mode = cluster.get("accessConfig", {}).get("authenticationMode")
    if auth_mode != config["eks"]["authenticationMode"]:
        raise ControllerError("live EKS authentication mode does not match configuration")
    live_names = set(
        clients["eks"].list_nodegroups(clusterName=config["eks"]["clusterName"])[
            "nodegroups"
        ]
    )
    configured_names = {item["name"] for item in config["eks"]["nodeGroups"]}
    if live_names != configured_names:
        raise ControllerError(
            "managed node group set differs from configuration: "
            f"live={sorted(live_names)} configured={sorted(configured_names)}"
        )
    groups: dict[str, Any] = {}
    asg_names: list[str] = []
    for configured in config["eks"]["nodeGroups"]:
        nodegroup = clients["eks"].describe_nodegroup(
            clusterName=config["eks"]["clusterName"],
            nodegroupName=configured["name"],
        )["nodegroup"]
        if nodegroup.get("nodegroupArn") != configured["arn"]:
            raise ControllerError(
                f"node group ARN mismatch for {configured['name']}"
            )
        names = [
            item["name"]
            for item in nodegroup.get("resources", {}).get("autoScalingGroups", [])
        ]
        asg_names.extend(names)
        groups[configured["name"]] = {
            "status": nodegroup.get("status"),
            "scaling": nodegroup.get("scalingConfig", {}),
            "autoScalingGroups": names,
        }
    asgs: dict[str, Any] = {}
    if asg_names:
        for asg in clients["autoscaling"].describe_auto_scaling_groups(
            AutoScalingGroupNames=asg_names
        )["AutoScalingGroups"]:
            asgs[asg["AutoScalingGroupName"]] = {
                "desired": asg["DesiredCapacity"],
                "inService": sum(
                    1
                    for instance in asg.get("Instances", [])
                    if instance.get("LifecycleState") == "InService"
                ),
                "instances": len(asg.get("Instances", [])),
            }
    rds_response = clients["rds"].describe_db_clusters(
        DBClusterIdentifier=config["rds"]["clusterIdentifier"]
    )["DBClusters"]
    if len(rds_response) != 1:
        raise ControllerError("configured Aurora cluster was not resolved uniquely")
    rds = rds_response[0]
    if rds.get("DBClusterArn") != config["rds"]["clusterArn"]:
        raise ControllerError("live Aurora ARN does not match configuration")
    if not str(rds.get("Engine", "")).startswith("aurora"):
        raise ControllerError("configured RDS cluster is not Aurora")
    if rds.get("GlobalClusterIdentifier"):
        raise ControllerError("Aurora Global Database members are not supported")
    return {
        "clients": clients,
        "cluster": cluster,
        "groups": groups,
        "asgs": asgs,
        "rds": rds,
    }


class KubeClient:
    def __init__(self, config: Mapping[str, Any], cluster: Mapping[str, Any]):
        self.config = config
        self.cluster_name = config["eks"]["clusterName"]
        self.region = config["aws"]["region"]
        self.endpoint = str(cluster["endpoint"]).rstrip("/")
        certificate = base64.b64decode(cluster["certificateAuthority"]["data"])
        self.ssl_context = ssl.create_default_context(
            cadata=certificate.decode("utf-8")
        )
        self._token = ""
        self._token_expires = 0

    def _bearer_token(self) -> str:
        now = _now()
        if self._token and now < self._token_expires:
            return self._token
        session = boto3.session.Session(region_name=self.region)
        sts = session.client("sts", region_name=self.region)
        signer = RequestSigner(
            sts.meta.service_model.service_id,
            self.region,
            "sts",
            "v4",
            session.get_credentials(),
            session.events,
        )
        url = f"https://sts.{self.region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
        signed_url = signer.generate_presigned_url(
            {
                "method": "GET",
                "url": url,
                "body": {},
                "headers": {"x-k8s-aws-id": self.cluster_name},
                "context": {},
            },
            region_name=self.region,
            expires_in=60,
            operation_name="",
        )
        encoded = base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode(
            "utf-8"
        )
        self._token = "k8s-aws-v1." + encoded.rstrip("=")
        self._token_expires = now + 600
        return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str | int] | None = None,
        expected: Iterable[int] = (200,),
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        url = self.endpoint + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._bearer_token()}")
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(
                request, context=self.ssl_context, timeout=20
            ) as response:
                raw = response.read()
                if response.status not in set(expected):
                    raise KubeApiError(response.status, raw.decode("utf-8", "replace"))
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code in set(expected):
                return json.loads(raw) if raw else {}
            raise KubeApiError(exc.code, raw) from exc

    def nodes(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v1/nodes").get("items", [])

    def pods(
        self,
        namespace: str,
        *,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, str] = {}
        if label_selector:
            query["labelSelector"] = label_selector
        if field_selector:
            query["fieldSelector"] = field_selector
        return self.request(
            "GET", f"/api/v1/namespaces/{namespace}/pods", query=query
        ).get("items", [])

    def all_pods(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v1/pods").get("items", [])

    def jobs(self, namespace: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"/apis/batch/v1/namespaces/{namespace}/jobs"
        ).get("items", [])

    def volume_attachments(self) -> list[dict[str, Any]]:
        return self.request(
            "GET", "/apis/storage.k8s.io/v1/volumeattachments"
        ).get("items", [])

    def deployment(self, namespace: str, name: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        )

    def deployments(self, namespace: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments"
        ).get("items", [])

    def endpoints(self, namespace: str, name: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/api/v1/namespaces/{namespace}/endpoints/{name}"
        )

    def statefulset(self, namespace: str, name: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}"
        )

    def statefulsets(self, namespace: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"/apis/apps/v1/namespaces/{namespace}/statefulsets"
        ).get("items", [])

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        self.request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
            body={"spec": {"replicas": replicas}},
            content_type="application/merge-patch+json",
        )

    def cordon(self, node_name: str, unschedulable: bool) -> None:
        self.request(
            "PATCH",
            f"/api/v1/nodes/{node_name}",
            body={"spec": {"unschedulable": unschedulable}},
            content_type="application/strategic-merge-patch+json",
        )

    def delete_pod(self, namespace: str, name: str, grace_seconds: int) -> None:
        self.request(
            "DELETE",
            f"/api/v1/namespaces/{namespace}/pods/{name}",
            query={"gracePeriodSeconds": grace_seconds},
            expected=(200, 202, 404),
        )


def _kube(config: Mapping[str, Any], cluster: Mapping[str, Any]) -> KubeClient:
    global _KUBE_CACHE
    if _KUBE_CACHE is None:
        _KUBE_CACHE = KubeClient(config, cluster)
    return _KUBE_CACHE


def _node_ready(node: Mapping[str, Any]) -> bool:
    for condition in node.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _nodes_by_group(kube: KubeClient) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for node in kube.nodes():
        group = node.get("metadata", {}).get("labels", {}).get(
            "eks.amazonaws.com/nodegroup"
        )
        if group:
            result.setdefault(group, []).append(node)
    return result


def _autoscaler_status(config: Mapping[str, Any], kube: KubeClient) -> dict[str, int]:
    autoscaler = config["kubernetes"]["autoscaler"]
    if not autoscaler["enabled"]:
        return {"desired": 0, "available": 0}
    deployment = kube.deployment(autoscaler["namespace"], autoscaler["deployment"])
    return {
        "desired": int(deployment.get("spec", {}).get("replicas", 0) or 0),
        "available": int(
            deployment.get("status", {}).get("availableReplicas", 0) or 0
        ),
    }


def _deferred_deployment_identities(
    config: Mapping[str, Any],
) -> list[tuple[str, str]]:
    return [
        (item["namespace"], item["name"])
        for item in config["kubernetes"]["deferredWake"]["deployments"]
    ]


def _deferred_deployment_statuses(
    config: Mapping[str, Any], kube: KubeClient
) -> dict[str, dict[str, int]]:
    statuses: dict[str, dict[str, int]] = {}
    for namespace, name in _deferred_deployment_identities(config):
        deployment = kube.deployment(namespace, name)
        desired = deployment.get("spec", {}).get("replicas")
        if isinstance(desired, bool) or not isinstance(desired, int) or desired < 0:
            raise ControllerError(
                f"deferred Deployment {namespace}/{name} has invalid replicas"
            )
        available = deployment.get("status", {}).get("availableReplicas", 0) or 0
        if isinstance(available, bool) or not isinstance(available, int) or available < 0:
            raise ControllerError(
                f"deferred Deployment {namespace}/{name} has invalid availability"
            )
        statuses[f"{namespace}/{name}"] = {
            "desired": desired,
            "available": available,
        }
    return statuses


def _capture_deferred_deployment_replicas(
    config: Mapping[str, Any],
    kube: KubeClient,
    previous: Mapping[str, int] | None = None,
) -> dict[str, int]:
    statuses = _deferred_deployment_statuses(config, kube)
    captured: dict[str, int] = {}
    invalid: list[str] = []
    for identity, status in sorted(statuses.items()):
        if status["desired"] >= 1:
            captured[identity] = status["desired"]
        elif previous is not None and identity in previous:
            captured[identity] = previous[identity]
        else:
            invalid.append(identity)
    if invalid:
        raise ControllerError(
            "deferred Deployments need a positive live or persisted replica baseline: "
            + ", ".join(invalid)
        )
    return captured


def _stored_deferred_deployment_replicas(
    state: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, int] | None:
    raw = state.get("deferredDeploymentReplicas")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ControllerError("stored deferred Deployment replicas are not an object")
    expected = {
        f"{namespace}/{name}"
        for namespace, name in _deferred_deployment_identities(config)
    }
    if set(raw) != expected:
        raise ControllerError(
            "stored deferred Deployment set differs from configuration"
        )
    normalized: dict[str, int] = {}
    for identity in sorted(expected):
        replicas = raw[identity]
        if (
            isinstance(replicas, bool)
            or not isinstance(replicas, (int, Decimal))
            or (isinstance(replicas, Decimal) and replicas != replicas.to_integral())
            or int(replicas) < 1
        ):
            raise ControllerError(
                f"stored deferred Deployment replicas for {identity} are invalid"
            )
        normalized[identity] = int(replicas)
    return normalized


def _deferred_prerequisite_status(
    config: Mapping[str, Any], kube: KubeClient
) -> dict[str, Any]:
    prerequisite = config["kubernetes"]["deferredWake"]["prerequisite"]
    deployment = kube.deployment(
        prerequisite["namespace"], prerequisite["deployment"]
    )
    desired = deployment.get("spec", {}).get("replicas")
    available = deployment.get("status", {}).get("availableReplicas", 0) or 0
    if isinstance(desired, bool) or not isinstance(desired, int) or desired < 1:
        raise ControllerError("deferred wake prerequisite has invalid replicas")
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise ControllerError("deferred wake prerequisite has invalid availability")
    endpoints = kube.endpoints(prerequisite["namespace"], prerequisite["service"])
    ready_endpoints = sum(
        len(subset.get("addresses", []))
        for subset in endpoints.get("subsets", [])
    )
    return {
        "deployment": (
            f"{prerequisite['namespace']}/{prerequisite['deployment']}"
        ),
        "service": f"{prerequisite['namespace']}/{prerequisite['service']}",
        "desired": desired,
        "available": available,
        "readyEndpoints": ready_endpoints,
        "ready": available >= desired and ready_endpoints >= 1,
    }


def _stop_deferred_deployments(
    config: Mapping[str, Any], kube: KubeClient
) -> dict[str, Any] | None:
    statuses = _deferred_deployment_statuses(config, kube)
    changed: list[str] = []
    for identity, status in sorted(statuses.items()):
        if status["desired"] == 0:
            continue
        namespace, name = identity.split("/", 1)
        kube.scale_deployment(namespace, name, 0)
        changed.append(identity)
    if changed:
        return _result(
            "RETRY",
            "STOPPING_DEFERRED_DEPLOYMENTS",
            "requested deferred Deployment replicas=0",
            wait_seconds=RETRY_SECONDS,
            details={"deployments": changed},
        )
    terminating = sorted(
        identity
        for identity, status in statuses.items()
        if status["available"] != 0
    )
    if terminating:
        return _result(
            "RETRY",
            "STOPPING_DEFERRED_DEPLOYMENTS",
            "waiting for deferred Deployment pod termination",
            wait_seconds=RETRY_SECONDS,
            details={"deployments": terminating},
        )
    return None


def _restore_deferred_deployments(
    config: Mapping[str, Any],
    kube: KubeClient,
    baseline: Mapping[str, int],
) -> dict[str, Any] | None:
    prerequisite = _deferred_prerequisite_status(config, kube)
    if not prerequisite["ready"]:
        return _result(
            "RETRY",
            "WAITING_DEFERRED_PREREQUISITE",
            "waiting for the deferred wake prerequisite",
            wait_seconds=RETRY_SECONDS,
            details={"prerequisite": prerequisite},
        )
    statuses = _deferred_deployment_statuses(config, kube)
    changed: list[str] = []
    for identity, replicas in sorted(baseline.items()):
        if statuses[identity]["desired"] == replicas:
            continue
        namespace, name = identity.split("/", 1)
        kube.scale_deployment(namespace, name, replicas)
        changed.append(identity)
    if changed:
        return _result(
            "RETRY",
            "RESTORING_DEFERRED_DEPLOYMENTS",
            "restored deferred Deployment replicas",
            wait_seconds=RETRY_SECONDS,
            details={"deployments": changed},
        )
    not_ready = sorted(
        identity
        for identity, replicas in baseline.items()
        if statuses[identity]["available"] < replicas
    )
    if not_ready:
        return _result(
            "RETRY",
            "WAITING_DEFERRED_DEPLOYMENTS",
            "waiting for deferred Deployment readiness",
            wait_seconds=RETRY_SECONDS,
            details={"deployments": not_ready},
        )
    return None


def _runtime_sleep_safety_blockers(
    config: Mapping[str, Any], kube: KubeClient
) -> list[str]:
    blockers: list[str] = []
    configured_groups = {
        group["name"] for group in config["eks"]["nodeGroups"]
    }
    for node in kube.nodes():
        metadata = node.get("metadata", {})
        name = metadata.get("name", "unknown")
        group = metadata.get("labels", {}).get("eks.amazonaws.com/nodegroup")
        if not group:
            blockers.append(f"unidentified-node:{name}")
        elif group not in configured_groups:
            blockers.append(f"unmanaged-node:{group}/{name}")

    managed_namespaces = set(config["kubernetes"]["managedNamespaces"])
    for pod in kube.all_pods():
        metadata = pod.get("metadata", {})
        namespace = metadata.get("namespace", "unknown")
        if namespace in managed_namespaces or _is_daemon_or_static(pod):
            continue
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        blockers.append(
            f"unmanaged-pod:{namespace}/{metadata.get('name', 'unknown')}"
        )
    return sorted(set(blockers))


def _is_daemon_or_static(pod: Mapping[str, Any]) -> bool:
    metadata = pod.get("metadata", {})
    if "kubernetes.io/config.mirror" in metadata.get("annotations", {}):
        return True
    return any(
        owner.get("kind") == "DaemonSet"
        for owner in metadata.get("ownerReferences", [])
    )


def _quiesce_group(
    config: Mapping[str, Any],
    kube: KubeClient,
    group_name: str,
) -> dict[str, int]:
    nodes = {
        node["metadata"]["name"]
        for node in _nodes_by_group(kube).get(group_name, [])
    }
    remaining = 0
    deleted = 0
    for namespace in config["kubernetes"]["managedNamespaces"]:
        for pod in kube.pods(namespace):
            if pod.get("spec", {}).get("nodeName") not in nodes:
                continue
            if _is_daemon_or_static(pod):
                continue
            if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            remaining += 1
            name = pod["metadata"]["name"]
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            try:
                kube.delete_pod(
                    namespace,
                    name,
                    config["kubernetes"]["quiesce"]["gracePeriodSeconds"],
                )
                deleted += 1
            except KubeApiError as exc:
                if exc.status in {404, 409}:
                    continue
                raise
    return {"remaining": remaining, "deleted": deleted}


def _attached_volumes_for_group(kube: KubeClient, group_name: str) -> int:
    node_names = {
        node["metadata"]["name"]
        for node in _nodes_by_group(kube).get(group_name, [])
    }
    return sum(
        1
        for attachment in kube.volume_attachments()
        if attachment.get("spec", {}).get("nodeName") in node_names
        and attachment.get("status", {}).get("attached") is True
    )


def _asg_zero(observed: Mapping[str, Any], group_name: str) -> bool:
    group = observed["groups"][group_name]
    names = group["autoScalingGroups"]
    return bool(names) and all(
        observed["asgs"].get(name, {}).get("desired") == 0
        and observed["asgs"].get(name, {}).get("instances") == 0
        for name in names
    )


def _environment_capacity_is_zero(
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    awake_baseline: Mapping[str, Any],
) -> bool:
    return all(
        observed["groups"][group["name"]]["status"] == "ACTIVE"
        and observed["groups"][group["name"]]["scaling"].get("minSize") == 0
        and observed["groups"][group["name"]]["scaling"].get("desiredSize") == 0
        and observed["groups"][group["name"]]["scaling"].get("maxSize")
        == awake_baseline["nodeGroups"][group["name"]]["max"]
        and _asg_zero(observed, group["name"])
        for group in config["eks"]["nodeGroups"]
    )


def _update_nodegroup(
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    group: Mapping[str, Any],
    scaling: Mapping[str, int],
    generation: int,
    command_key: str,
) -> None:
    request_material = json.dumps(
        {
            "configHash": CONFIG_HASH,
            "environment": ENVIRONMENT_NAME,
            "generation": generation,
            "commandKey": command_key,
            "group": group["name"],
            "scaling": dict(scaling),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request_token = hashlib.sha256(request_material).hexdigest()
    observed["clients"]["eks"].update_nodegroup_config(
        clusterName=config["eks"]["clusterName"],
        nodegroupName=group["name"],
        scalingConfig={
            "minSize": scaling["min"],
            "desiredSize": scaling["desired"],
            "maxSize": scaling["max"],
        },
        clientRequestToken=request_token,
    )


def _configured_awake_baseline(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": AWAKE_BASELINE_VERSION,
        "source": "configured-fallback",
        "configHash": CONFIG_HASH,
        "nodeGroups": {
            group["name"]: {
                "min": int(group["awake"]["min"]),
                "desired": int(group["awake"]["desired"]),
                "max": int(group["awake"]["max"]),
            }
            for group in config["eks"]["nodeGroups"]
        },
        "autoscalerReplicas": int(
            config["kubernetes"]["autoscaler"]["awakeReplicas"]
        ),
    }


def _effective_awake_baseline(
    state: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    raw = state.get("awakeBaseline")
    if raw is None:
        return _configured_awake_baseline(config)
    if not isinstance(raw, Mapping):
        raise ControllerError("stored awake baseline is not an object")
    version = raw.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, (int, Decimal))
        or int(version) != AWAKE_BASELINE_VERSION
    ):
        raise ControllerError("stored awake baseline has an unsupported version")

    raw_groups = raw.get("nodeGroups")
    if not isinstance(raw_groups, Mapping):
        raise ControllerError("stored awake baseline has no nodeGroups object")
    expected_names = {group["name"] for group in config["eks"]["nodeGroups"]}
    if set(raw_groups) != expected_names:
        raise ControllerError("stored awake baseline node-group set differs from config")

    normalized_groups: dict[str, dict[str, int]] = {}
    for name in sorted(expected_names):
        scaling = raw_groups[name]
        if not isinstance(scaling, Mapping):
            raise ControllerError(f"stored awake baseline for {name} is not an object")
        minimum = scaling.get("min")
        desired = scaling.get("desired")
        maximum = scaling.get("max")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, Decimal))
            for value in (minimum, desired, maximum)
        ):
            raise ControllerError(
                f"stored awake baseline for {name} has non-integer scaling"
            )
        minimum, desired, maximum = int(minimum), int(desired), int(maximum)
        if minimum < 1 or not minimum <= desired <= maximum:
            raise ControllerError(
                f"stored awake baseline for {name} must satisfy "
                "1 <= min <= desired <= max"
            )
        normalized_groups[name] = {
            "min": minimum,
            "desired": desired,
            "max": maximum,
        }

    autoscaler_replicas = raw.get("autoscalerReplicas", 0)
    if isinstance(autoscaler_replicas, bool) or not isinstance(
        autoscaler_replicas, (int, Decimal)
    ):
        raise ControllerError("stored awake autoscaler baseline is not an integer")
    autoscaler_replicas = int(autoscaler_replicas)
    autoscaler_enabled = config["kubernetes"]["autoscaler"]["enabled"]
    if autoscaler_enabled and autoscaler_replicas < 1:
        raise ControllerError("stored awake autoscaler baseline must be at least one")
    if not autoscaler_enabled:
        autoscaler_replicas = 0

    return {
        "version": AWAKE_BASELINE_VERSION,
        "source": str(raw.get("source", "live-before-sleep")),
        "configHash": str(raw.get("configHash", "")),
        "nodeGroups": normalized_groups,
        "autoscalerReplicas": autoscaler_replicas,
    }


def _capture_awake_baseline(
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    autoscaler: Mapping[str, int],
) -> dict[str, Any]:
    previous = _effective_awake_baseline(state, config)
    node_groups: dict[str, dict[str, int]] = {}
    for group in config["eks"]["nodeGroups"]:
        name = group["name"]
        live_group = observed["groups"][name]
        if live_group.get("status") != "ACTIVE":
            raise RetryableControllerError(
                f"node group {name} must be ACTIVE before capturing its awake baseline"
            )
        scaling = live_group["scaling"]
        minimum = scaling.get("minSize")
        desired = scaling.get("desiredSize")
        maximum = scaling.get("maxSize")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (minimum, desired, maximum)
        ):
            raise ControllerError(f"live scaling for {name} is incomplete")
        if minimum >= 1 and minimum <= desired <= maximum:
            node_groups[name] = {
                "min": minimum,
                "desired": minimum,
                "max": maximum,
            }
        elif minimum == 0 and desired == 0:
            # A retried/continued sleep may encounter a group already at zero.
            node_groups[name] = previous["nodeGroups"][name]
        else:
            raise ControllerError(
                f"live scaling for {name} is neither a valid awake range nor zero"
            )

    autoscaler_replicas = 0
    if config["kubernetes"]["autoscaler"]["enabled"]:
        live_replicas = autoscaler.get("desired")
        if isinstance(live_replicas, int) and live_replicas >= 1:
            autoscaler_replicas = live_replicas
        elif live_replicas == 0:
            autoscaler_replicas = previous["autoscalerReplicas"]
        else:
            raise ControllerError("live Cluster Autoscaler replicas are invalid")

    return {
        "version": AWAKE_BASELINE_VERSION,
        "source": "live-before-sleep",
        "configHash": CONFIG_HASH,
        "nodeGroups": node_groups,
        "autoscalerReplicas": autoscaler_replicas,
    }


def _save_snapshot(
    owner: str,
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    autoscaler: Mapping[str, int],
    awake_baseline: Mapping[str, Any],
    deferred_deployment_replicas: Mapping[str, int],
) -> None:
    snapshot = {
        "generation": int(state.get("desiredGeneration", 0)),
        "configHash": CONFIG_HASH,
        "rdsStatus": observed["rds"].get("Status"),
        "autoscalerReplicas": autoscaler["desired"],
        "deferredDeploymentReplicas": dict(deferred_deployment_replicas),
        "nodeGroups": {
            name: {
                "min": group["scaling"].get("minSize"),
                "desired": group["scaling"].get("desiredSize"),
                "max": group["scaling"].get("maxSize"),
            }
            for name, group in observed["groups"].items()
        },
    }
    _set_fields(
        {
            "snapshot": snapshot,
            "snapshotGeneration": int(state.get("desiredGeneration", 0)),
            "snapshotAt": _now(),
            "awakeBaseline": dict(awake_baseline),
            "awakeBaselineAt": _now(),
            "awakeBaselineGeneration": int(state.get("desiredGeneration", 0)),
            "deferredDeploymentReplicas": dict(deferred_deployment_replicas),
            "deferredDeploymentBaselineAt": _now(),
            "deferredDeploymentBaselineGeneration": int(
                state.get("desiredGeneration", 0)
            ),
        },
        owner=owner,
    )


def _reconcile_sleep(
    owner: str,
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    kube: KubeClient,
) -> tuple[dict[str, Any], str]:
    autoscaler = _autoscaler_status(config, kube)
    generation = int(state.get("desiredGeneration", 0))
    try:
        awake_baseline = _effective_awake_baseline(state, config)
    except ControllerError as exc:
        return (
            _result(
                "FAILED",
                "AWAKE_BASELINE_INVALID",
                str(exc),
            ),
            "ERROR_SLEEPING",
        )
    safety_blockers = _runtime_sleep_safety_blockers(config, kube)
    if safety_blockers:
        return (
            _result(
                "BLOCKED",
                "RUNTIME_SCOPE_DRIFT",
                "sleep blocked by nodes or workloads outside the configured scope",
                details={
                    "blockers": safety_blockers[:25],
                    "blockerCount": len(safety_blockers),
                },
            ),
            "ERROR_SLEEPING",
        )
    if int(state.get("snapshotGeneration", -1)) != generation:
        try:
            awake_baseline = _capture_awake_baseline(
                state, config, observed, autoscaler
            )
            previous_deferred_replicas = (
                _stored_deferred_deployment_replicas(state, config)
            )
            deferred_deployment_replicas = (
                _capture_deferred_deployment_replicas(
                    config,
                    kube,
                    previous_deferred_replicas,
                )
            )
        except RetryableControllerError as exc:
            return (
                _result(
                    "RETRY",
                    "WAITING_LIVE_BASELINE",
                    str(exc),
                    wait_seconds=RETRY_SECONDS,
                ),
                "SLEEPING",
            )
        except ControllerError as exc:
            return (
                _result(
                    "FAILED",
                    "LIVE_BASELINE_INVALID",
                    str(exc),
                ),
                "ERROR_SLEEPING",
            )
        _save_snapshot(
            owner,
            state,
            config,
            observed,
            autoscaler,
            awake_baseline,
            deferred_deployment_replicas,
        )
        return (
            _result(
                "RETRY",
                "AWAKE_BASELINE_CAPTURED",
                "captured live wake baseline and recovery evidence before shutdown",
                wait_seconds=1,
                details={
                    "nodeGroups": awake_baseline["nodeGroups"],
                    "autoscalerReplicas": awake_baseline["autoscalerReplicas"],
                    "deferredDeploymentReplicas": deferred_deployment_replicas,
                },
            ),
            "SLEEPING",
        )

    try:
        deferred_deployment_replicas = _stored_deferred_deployment_replicas(
            state, config
        )
    except ControllerError as exc:
        return (
            _result("FAILED", "DEFERRED_BASELINE_INVALID", str(exc)),
            "ERROR_SLEEPING",
        )
    if deferred_deployment_replicas is None:
        return (
            _result(
                "FAILED",
                "DEFERRED_BASELINE_INVALID",
                "deferred Deployment baseline was not captured",
            ),
            "ERROR_SLEEPING",
        )

    deferred_action = _stop_deferred_deployments(config, kube)
    if deferred_action:
        return deferred_action, "SLEEPING"

    groups_are_already_zero = _environment_capacity_is_zero(
        config, observed, awake_baseline
    )
    if groups_are_already_zero:
        rds_status = observed["rds"].get("Status")
        autoscaler_config = config["kubernetes"]["autoscaler"]
        if autoscaler_config["enabled"]:
            if autoscaler["desired"] != 0:
                kube.scale_deployment(
                    autoscaler_config["namespace"],
                    autoscaler_config["deployment"],
                    0,
                )
                return (
                    _result(
                        "RETRY",
                        "STOPPING_AUTOSCALER",
                        "restored Cluster Autoscaler replicas=0 for the asleep environment",
                        wait_seconds=RETRY_SECONDS,
                    ),
                    "SLEEPING",
                )
            if autoscaler["available"] != 0:
                return (
                    _result(
                        "RETRY",
                        "STOPPING_AUTOSCALER",
                        "waiting for Cluster Autoscaler pod termination",
                        wait_seconds=RETRY_SECONDS,
                    ),
                    "SLEEPING",
                )
        if rds_status == "available":
            observed["clients"]["rds"].stop_db_cluster(
                DBClusterIdentifier=config["rds"]["clusterIdentifier"]
            )
            return (
                _result(
                    "RETRY",
                    "STOPPING_AURORA",
                    "requested stop of automatically restarted Aurora",
                    wait_seconds=RETRY_SECONDS,
                ),
                "SLEEPING",
            )
        if rds_status == "stopped":
            return (
                _result("SUCCEEDED", "ASLEEP", "environment is fully asleep"),
                "ASLEEP",
            )
        return (
            _result(
                "RETRY",
                "STOPPING_AURORA",
                f"waiting for Aurora; current status is {rds_status}",
                wait_seconds=RETRY_SECONDS,
            ),
            "SLEEPING",
        )

    autoscaler_config = config["kubernetes"]["autoscaler"]
    if autoscaler_config["enabled"]:
        if autoscaler["desired"] != 0:
            kube.scale_deployment(
                autoscaler_config["namespace"], autoscaler_config["deployment"], 0
            )
            return (
                _result(
                    "RETRY",
                    "STOPPING_AUTOSCALER",
                    "requested Cluster Autoscaler replicas=0",
                    wait_seconds=RETRY_SECONDS,
                ),
                "SLEEPING",
            )
        if autoscaler["available"] != 0:
            return (
                _result(
                    "RETRY",
                    "STOPPING_AUTOSCALER",
                    "waiting for Cluster Autoscaler pod termination",
                    wait_seconds=RETRY_SECONDS,
                ),
                "SLEEPING",
            )

    nodes_by_group = _nodes_by_group(kube)
    patched = 0
    for group in config["eks"]["nodeGroups"]:
        for node in nodes_by_group.get(group["name"], []):
            if not node.get("spec", {}).get("unschedulable", False):
                kube.cordon(node["metadata"]["name"], True)
                patched += 1
    if patched:
        return (
            _result(
                "RETRY",
                "CORDONING_NODES",
                f"cordoned {patched} worker nodes",
                wait_seconds=1,
            ),
            "SLEEPING",
        )

    non_system_groups = sorted(
        (group for group in config["eks"]["nodeGroups"] if group["role"] != "system"),
        key=lambda group: group["sleepOrder"],
    )
    system_groups = sorted(
        (group for group in config["eks"]["nodeGroups"] if group["role"] == "system"),
        key=lambda group: group["sleepOrder"],
    )
    for group in non_system_groups:
        action = _sleep_group(
            config,
            observed,
            kube,
            state,
            group,
            awake_baseline["nodeGroups"][group["name"]],
        )
        if action:
            return action, "SLEEPING"

    rds_status = observed["rds"].get("Status")
    if rds_status == "available":
        observed["clients"]["rds"].stop_db_cluster(
            DBClusterIdentifier=config["rds"]["clusterIdentifier"]
        )
        return (
            _result(
                "RETRY",
                "STOPPING_AURORA",
                "requested Aurora cluster stop",
                wait_seconds=RETRY_SECONDS,
            ),
            "SLEEPING",
        )
    if rds_status != "stopped":
        return (
            _result(
                "RETRY",
                "STOPPING_AURORA",
                f"waiting for Aurora; current status is {rds_status}",
                wait_seconds=RETRY_SECONDS,
            ),
            "SLEEPING",
        )

    for group in system_groups:
        action = _sleep_group(
            config,
            observed,
            kube,
            state,
            group,
            awake_baseline["nodeGroups"][group["name"]],
        )
        if action:
            return action, "SLEEPING"

    if all(_asg_zero(observed, group["name"]) for group in config["eks"]["nodeGroups"]):
        return (
            _result("SUCCEEDED", "ASLEEP", "environment is fully asleep"),
            "ASLEEP",
        )
    return (
        _result(
            "RETRY",
            "VERIFYING_ASLEEP",
            "waiting for all worker instances to terminate",
            wait_seconds=RETRY_SECONDS,
        ),
        "SLEEPING",
    )


def _sleep_group(
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    kube: KubeClient,
    state: Mapping[str, Any],
    group: Mapping[str, Any],
    awake: Mapping[str, int],
) -> dict[str, Any] | None:
    live = observed["groups"][group["name"]]
    scaling = live["scaling"]
    if live["status"] in TERMINAL_NODEGROUP_STATUSES:
        return _result(
            "FAILED",
            "NODEGROUP_DEGRADED",
            f"node group {group['name']} is {live['status']}",
        )
    if live["status"] != "ACTIVE":
        return _result(
            "RETRY",
            f"WAITING_{group['name']}_UPDATE",
            f"waiting for {group['name']} update to finish",
            wait_seconds=RETRY_SECONDS,
        )
    if scaling.get("minSize") == 0 and scaling.get("desiredSize") == 0:
        if _asg_zero(observed, group["name"]):
            return None
        return _result(
            "RETRY",
            f"WAITING_{group['name']}_ZERO",
            f"waiting for {group['name']} instances to terminate",
            wait_seconds=RETRY_SECONDS,
        )
    phase = f"QUIESCING_{group['name']}"
    quiesce = _quiesce_group(config, kube, group["name"])
    if quiesce["remaining"]:
        return _result(
            "RETRY",
            phase,
            f"waiting for workloads on {group['name']} to terminate",
            wait_seconds=RETRY_SECONDS,
            details=quiesce,
        )
    attachments = _attached_volumes_for_group(kube, group["name"])
    if attachments:
        return _result(
            "RETRY",
            f"DETACHING_{group['name']}_VOLUMES",
            f"waiting for {attachments} volume attachments on {group['name']}",
            wait_seconds=RETRY_SECONDS,
        )
    _update_nodegroup(
        config,
        observed,
        group,
        {"min": 0, "desired": 0, "max": awake["max"]},
        int(state.get("desiredGeneration", 0)),
        str(
            state.get("lastCommandOrderKey")
            or state.get("lastCommandExecutionArn")
            or f"generation:{int(state.get('desiredGeneration', 0))}"
        ),
    )
    return _result(
        "RETRY",
        f"SCALING_{group['name']}_TO_ZERO",
        f"requested min=0 desired=0 for {group['name']}",
        wait_seconds=RETRY_SECONDS,
    )


def _workloads_ready(config: Mapping[str, Any], kube: KubeClient) -> list[str]:
    not_ready: list[str] = []
    checks = config["healthChecks"]["kubernetes"]
    expected_deployments = {
        (item["namespace"], item["name"]) for item in checks["deployments"]
    }
    expected_statefulsets = {
        (item["namespace"], item["name"]) for item in checks["statefulSets"]
    }
    live_deployments: set[tuple[str, str]] = set()
    live_statefulsets: set[tuple[str, str]] = set()
    for namespace in config["kubernetes"]["managedNamespaces"]:
        for live in kube.deployments(namespace):
            name = live["metadata"]["name"]
            live_deployments.add((namespace, name))
            desired = int(live.get("spec", {}).get("replicas", 0) or 0)
            available = int(
                live.get("status", {}).get("availableReplicas", 0) or 0
            )
            if available < desired:
                not_ready.append(
                    f"Deployment/{namespace}/{name}:{available}/{desired}"
                )
        for live in kube.statefulsets(namespace):
            name = live["metadata"]["name"]
            live_statefulsets.add((namespace, name))
            desired = int(live.get("spec", {}).get("replicas", 0) or 0)
            ready = int(live.get("status", {}).get("readyReplicas", 0) or 0)
            if ready < desired:
                not_ready.append(
                    f"StatefulSet/{namespace}/{name}:{ready}/{desired}"
                )
    for namespace, name in sorted(expected_deployments - live_deployments):
        not_ready.append(f"Deployment/{namespace}/{name}:missing")
    for namespace, name in sorted(expected_statefulsets - live_statefulsets):
        not_ready.append(f"StatefulSet/{namespace}/{name}:missing")
    return sorted(set(not_ready))


def _http_health(config: Mapping[str, Any]) -> list[str]:
    http = config["healthChecks"]["http"]
    timeout = http["requestTimeoutSeconds"]

    def check(endpoint: Mapping[str, Any]) -> str | None:
        request = urllib.request.Request(endpoint["url"], method="GET")
        request.add_header("User-Agent", "dial-nonprod-sleep-wake/1")
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"{endpoint['name']}:network:{type(exc).__name__}"
        if status not in set(endpoint["acceptedStatusCodes"]):
            return f"{endpoint['name']}:http-{status}"
        return None

    required = [item for item in http["endpoints"] if item["required"]]
    if not required:
        return []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(16, len(required))) as executor:
        futures = {executor.submit(check, endpoint): endpoint for endpoint in required}
        for future in as_completed(futures):
            failure = future.result()
            if failure:
                failures.append(failure)
    return sorted(failures)


def _reconcile_wake(
    owner: str,
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    observed: Mapping[str, Any],
    kube: KubeClient,
) -> tuple[dict[str, Any], str]:
    try:
        awake_baseline = _effective_awake_baseline(state, config)
    except ControllerError as exc:
        return (
            _result(
                "FAILED",
                "AWAKE_BASELINE_INVALID",
                str(exc),
            ),
            "ERROR_WAKING",
        )
    try:
        stored_deferred_replicas = _stored_deferred_deployment_replicas(
            state, config
        )
        if state.get("actualState") == "AWAKE":
            deferred_deployment_replicas = _capture_deferred_deployment_replicas(
                config,
                kube,
                stored_deferred_replicas,
            )
        elif stored_deferred_replicas is None:
            deferred_deployment_replicas = (
                _capture_deferred_deployment_replicas(config, kube)
            )
        else:
            deferred_deployment_replicas = stored_deferred_replicas
        if deferred_deployment_replicas != stored_deferred_replicas:
            _set_fields(
                {
                    "deferredDeploymentReplicas": deferred_deployment_replicas,
                    "deferredDeploymentBaselineAt": _now(),
                    "deferredDeploymentBaselineGeneration": int(
                        state.get("desiredGeneration", 0)
                    ),
                },
                owner=owner,
                expected_generation=int(state.get("desiredGeneration", 0)),
            )
            return (
                _result(
                    "RETRY",
                    "DEFERRED_BASELINE_CAPTURED",
                    "captured live deferred Deployment replicas",
                    wait_seconds=1,
                    details={
                        "deferredDeploymentReplicas": deferred_deployment_replicas
                    },
                ),
                "WAKING",
            )
    except ControllerError as exc:
        return (
            _result("FAILED", "DEFERRED_BASELINE_INVALID", str(exc)),
            "ERROR_WAKING",
        )

    prerequisite = _deferred_prerequisite_status(config, kube)
    if not prerequisite["ready"]:
        deferred_action = _stop_deferred_deployments(config, kube)
        if deferred_action:
            return deferred_action, "WAKING"
    rds_status = observed["rds"].get("Status")
    if rds_status == "stopped":
        observed["clients"]["rds"].start_db_cluster(
            DBClusterIdentifier=config["rds"]["clusterIdentifier"]
        )
        return (
            _result(
                "RETRY",
                "STARTING_AURORA",
                "requested Aurora cluster start",
                wait_seconds=RETRY_SECONDS,
            ),
            "WAKING",
        )
    if rds_status == "stopping":
        return (
            _result(
                "RETRY",
                "WAITING_AURORA_STOP",
                "Aurora must finish stopping before it can be started",
                wait_seconds=RETRY_SECONDS,
            ),
            "WAKING",
        )

    nodes_by_group = _nodes_by_group(kube)
    for group in sorted(config["eks"]["nodeGroups"], key=lambda item: item["wakeOrder"]):
        if group["role"] != "system" and rds_status != "available":
            return (
                _result(
                    "RETRY",
                    "WAITING_AURORA_READY",
                    f"waiting for Aurora before application capacity; current status is {rds_status}",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )
        live = observed["groups"][group["name"]]
        if live["status"] in TERMINAL_NODEGROUP_STATUSES:
            return (
                _result(
                    "FAILED",
                    "NODEGROUP_DEGRADED",
                    f"node group {group['name']} is {live['status']}",
                ),
                "ERROR_WAKING",
            )
        if live["status"] != "ACTIVE":
            return (
                _result(
                    "RETRY",
                    f"WAITING_{group['name']}_UPDATE",
                    f"waiting for {group['name']} update to finish",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )
        live_scaling = live["scaling"]
        awake = awake_baseline["nodeGroups"][group["name"]]
        live_desired = live_scaling.get("desiredSize")
        scaling_needs_restore = (
            live_scaling.get("minSize") != awake["min"]
            or live_scaling.get("maxSize") != awake["max"]
            or not isinstance(live_desired, int)
            or live_desired < awake["desired"]
            or live_desired > awake["max"]
        )
        if scaling_needs_restore:
            _update_nodegroup(
                config,
                observed,
                group,
                awake,
                int(state.get("desiredGeneration", 0)),
                str(
                    state.get("lastCommandOrderKey")
                    or state.get("lastCommandExecutionArn")
                    or f"generation:{int(state.get('desiredGeneration', 0))}"
                ),
            )
            return (
                _result(
                    "RETRY",
                    f"RESTORING_{group['name']}",
                    f"requested awake scaling for {group['name']}",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )
        nodes = nodes_by_group.get(group["name"], [])
        ready = sum(1 for node in nodes if _node_ready(node))
        if ready < group["awake"]["desired"]:
            return (
                _result(
                    "RETRY",
                    f"WAITING_{group['name']}_READY",
                    f"{ready}/{group['awake']['desired']} nodes are Ready",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )
        patched = 0
        for node in nodes:
            if node.get("spec", {}).get("unschedulable", False):
                kube.cordon(node["metadata"]["name"], False)
                patched += 1
        if patched:
            return (
                _result(
                    "RETRY",
                    f"UNCORDONING_{group['name']}",
                    f"uncordoned {patched} nodes",
                    wait_seconds=1,
                ),
                "WAKING",
            )
        if group["name"] == config["kubernetes"]["deferredWake"]["afterNodeGroup"]:
            deferred_action = _restore_deferred_deployments(
                config,
                kube,
                deferred_deployment_replicas,
            )
            if deferred_action:
                return deferred_action, "WAKING"

    if rds_status != "available":
        return (
            _result(
                "RETRY",
                "WAITING_AURORA_READY",
                f"waiting for Aurora; current status is {rds_status}",
                wait_seconds=RETRY_SECONDS,
            ),
            "WAKING",
        )

    autoscaler_config = config["kubernetes"]["autoscaler"]
    if autoscaler_config["enabled"]:
        autoscaler = _autoscaler_status(config, kube)
        awake_replicas = awake_baseline["autoscalerReplicas"]
        if autoscaler["desired"] != awake_replicas:
            kube.scale_deployment(
                autoscaler_config["namespace"],
                autoscaler_config["deployment"],
                awake_replicas,
            )
            return (
                _result(
                    "RETRY",
                    "STARTING_AUTOSCALER",
                    "restored Cluster Autoscaler replicas",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )
        if autoscaler["available"] < awake_replicas:
            return (
                _result(
                    "RETRY",
                    "STARTING_AUTOSCALER",
                    "waiting for Cluster Autoscaler readiness",
                    wait_seconds=RETRY_SECONDS,
                ),
                "WAKING",
            )

    not_ready = _workloads_ready(config, kube)
    if not_ready:
        return (
            _result(
                "RETRY",
                "WAITING_WORKLOADS",
                "waiting for configured Kubernetes workloads",
                wait_seconds=RETRY_SECONDS,
                details={"notReady": not_ready[:25], "notReadyCount": len(not_ready)},
            ),
            "WAKING",
        )
    http_failures = _http_health(config)
    if http_failures:
        return (
            _result(
                "RETRY",
                "WAITING_ENDPOINTS",
                "waiting for configured public health endpoints",
                wait_seconds=RETRY_SECONDS,
                details={"failures": http_failures},
            ),
            "WAKING",
        )
    return _result("SUCCEEDED", "AWAKE", "environment is fully awake"), "AWAKE"


def _handle_reconcile(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    owner = event.get("executionArn")
    if not isinstance(owner, str) or not owner:
        raise ControllerError("reconcile requires executionArn")
    state = _renew_lock(owner)
    config = _load_config()
    generation = int(state.get("desiredGeneration", -1))
    if state.get("configHash") != CONFIG_HASH:
        result = _result(
            "FAILED",
            "CONFIG_VERSION_MISMATCH",
            "operation was started with a different configuration version",
        )
        try:
            _record_result(
                owner,
                result,
                "ERROR_WAKING"
                if state.get("desiredState") == "AWAKE"
                else "ERROR_SLEEPING",
                generation,
            )
        except StaleGenerationError:
            return _result(
                "RETRY",
                "DESIRED_STATE_CHANGED",
                "desired state changed during reconciliation; re-reading live state",
                wait_seconds=1,
            )
        return result
    elapsed = _now() - int(state.get("operationStartedAt", _now()))
    if elapsed > config["controller"]["maxTransitionSeconds"]:
        result = _result(
            "FAILED",
            "TRANSITION_TIMEOUT",
            "transition exceeded the configured maximum duration",
        )
        try:
            _record_result(
                owner,
                result,
                "ERROR_WAKING"
                if state.get("desiredState") == "AWAKE"
                else "ERROR_SLEEPING",
                generation,
            )
        except StaleGenerationError:
            return _result(
                "RETRY",
                "DESIRED_STATE_CHANGED",
                "desired state changed during reconciliation; re-reading live state",
                wait_seconds=1,
            )
        return result
    try:
        observed = _observe_aws(config)
        kube = _kube(config, observed["cluster"])
        _log(
            "INFO",
            "reconcile_observed",
            "observed AWS capacity before the reconciliation decision",
            environment=ENVIRONMENT_NAME,
            operation="reconcile",
            executionArn=owner,
            requestId=event.get("_requestId"),
            configHash=CONFIG_HASH,
            desiredState=state.get("desiredState"),
            actualState=state.get("actualState"),
            generation=generation,
            currentPhase=state.get("phase"),
            observed=_observed_aws_log_summary(observed),
        )
        if state.get("desiredState") == "ASLEEP":
            result, actual_state = _reconcile_sleep(
                owner, state, config, observed, kube
            )
        elif state.get("desiredState") == "AWAKE":
            result, actual_state = _reconcile_wake(
                owner, state, config, observed, kube
            )
        else:
            result = _result(
                "FAILED", "INVALID_DESIRED_STATE", "state table has no valid desired state"
            )
            actual_state = "UNKNOWN"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        details = _aws_error_details(exc)
        if _is_transient_failure(exc):
            result = _result(
                "RETRY",
                "TRANSIENT_AWS_ERROR",
                f"transient AWS error {code}",
                wait_seconds=RETRY_SECONDS,
                details=details,
            )
            actual_state = state.get("actualState", "UNKNOWN")
        else:
            result = _result(
                "FAILED",
                "AWS_API_ERROR",
                f"AWS API error {code}",
                details=details,
            )
            actual_state = (
                "ERROR_WAKING"
                if state.get("desiredState") == "AWAKE"
                else "ERROR_SLEEPING"
            )
    except (
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
        TimeoutError,
        urllib.error.URLError,
    ) as exc:
        result = _result(
            "RETRY",
            "TRANSIENT_NETWORK_ERROR",
            f"transient network error {type(exc).__name__}",
            wait_seconds=RETRY_SECONDS,
            details={"errorMessage": str(exc)[:500]},
        )
        actual_state = state.get("actualState", "UNKNOWN")
    except KubeApiError as exc:
        if exc.status in {408, 409, 429} or exc.status >= 500:
            result = _result(
                "RETRY",
                "TRANSIENT_KUBERNETES_ERROR",
                f"transient Kubernetes API status {exc.status}",
                wait_seconds=RETRY_SECONDS,
            )
            actual_state = state.get("actualState", "UNKNOWN")
        else:
            result = _result("FAILED", "CONTROLLER_ERROR", str(exc)[:1000])
            actual_state = (
                "ERROR_WAKING"
                if state.get("desiredState") == "AWAKE"
                else "ERROR_SLEEPING"
            )
    except ControllerError as exc:
        result = _result("FAILED", "CONTROLLER_ERROR", str(exc)[:1000])
        actual_state = (
            "ERROR_WAKING"
            if state.get("desiredState") == "AWAKE"
            else "ERROR_SLEEPING"
        )
    try:
        _record_result(owner, result, actual_state, generation)
    except StaleGenerationError:
        return _result(
            "RETRY",
            "DESIRED_STATE_CHANGED",
            "desired state changed during reconciliation; re-reading live state",
            wait_seconds=1,
        )
    _log(
        "ERROR" if result["status"] == "FAILED" else (
            "WARNING" if result["status"] in {"RETRY", "BLOCKED"} else "INFO"
        ),
        "reconcile_decision",
        result["message"],
        environment=ENVIRONMENT_NAME,
        operation="reconcile",
        executionArn=owner,
        requestId=event.get("_requestId"),
        configHash=CONFIG_HASH,
        desiredState=state.get("desiredState"),
        actualState=actual_state,
        generation=generation,
        status=result["status"],
        phase=result["phase"],
        waitSeconds=result.get("waitSeconds"),
        details=result.get("details", {}),
    )
    return result


def _handle_status(event: Mapping[str, Any]) -> dict[str, Any]:
    _validate_event_environment(event)
    config = _load_config()
    state = _get_state()
    live: dict[str, Any]
    try:
        observed = _observe_aws(config)
        kube = _kube(config, observed["cluster"])
        nodes = _nodes_by_group(kube)
        live = {
            "rdsStatus": observed["rds"].get("Status"),
            "nodeGroups": {
                name: {
                    "status": item["status"],
                    "scaling": item["scaling"],
                    "readyNodes": sum(
                        1 for node in nodes.get(name, []) if _node_ready(node)
                    ),
                    "cordonedNodes": sum(
                        1
                        for node in nodes.get(name, [])
                        if node.get("spec", {}).get("unschedulable", False)
                    ),
                }
                for name, item in observed["groups"].items()
            },
            "autoscaler": _autoscaler_status(config, kube),
        }
    except Exception as exc:  # status must preserve state evidence even if live read fails
        live = {"readError": f"{type(exc).__name__}: {str(exc)[:300]}"}
    public_state = _json_safe({
        key: value
        for key, value in state.items()
        if key
        not in {
            "snapshot",
        }
    })
    return {
        "status": "SUCCEEDED",
        "environment": ENVIRONMENT_NAME,
        "configHash": CONFIG_HASH,
        "configRefreshProtocolVersion": CONFIG_REFRESH_PROTOCOL_VERSION,
        "state": public_state,
        "live": live,
    }


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    safe_event = dict(event) if isinstance(event, Mapping) else {}
    operation = safe_event.get("operation")
    log_context = _invocation_log_context(safe_event, context)
    if log_context.get("requestId"):
        safe_event["_requestId"] = log_context["requestId"]
    started = time.monotonic()
    _log(
        "INFO",
        "operation_started",
        "controller operation started",
        **log_context,
    )
    handlers = {
        "command": _handle_command,
        "acquire": _handle_acquire,
        "reconcile": _handle_reconcile,
        "release": _handle_release,
        "finalizeFailure": _handle_finalize_failure,
        "prepareConfigRefresh": _handle_prepare_config_refresh,
        "validateConfigRefresh": _handle_validate_config_refresh,
        "completeConfigRefresh": _handle_complete_config_refresh,
        "abortConfigRefresh": _handle_abort_config_refresh,
        "status": _handle_status,
    }
    try:
        handler = handlers.get(operation)
        if handler is None:
            raise ControllerError(f"unsupported operation: {operation!r}")
        result = handler(safe_event)
    except Exception as exc:
        if operation != "status" and _is_transient_failure(exc):
            details = (
                _aws_error_details(exc)
                if isinstance(exc, ClientError)
                else {
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc)[:500],
                }
            )
            _log(
                "WARNING",
                "operation_retryable_failure",
                "controller operation hit a transient dependency failure",
                **log_context,
                durationMs=max(0, round((time.monotonic() - started) * 1000)),
                details=details,
            )
            raise RetryableControllerError(
                f"transient {type(exc).__name__} during {operation}"
            ) from exc
        _log(
            "ERROR",
            "operation_failed",
            "controller operation raised an unhandled exception",
            **log_context,
            durationMs=max(0, round((time.monotonic() - started) * 1000)),
            errorType=type(exc).__name__,
            errorMessage=str(exc)[:1000],
            errorTraceback=traceback.format_exc(limit=20)[:8000],
        )
        raise

    status = result.get("status")
    level = "ERROR" if status == "FAILED" else (
        "WARNING" if status in {"RETRY", "BLOCKED"} else "INFO"
    )
    if operation == "status" and result.get("live", {}).get("readError"):
        level = "WARNING"
    completion: dict[str, Any] = {
        **log_context,
        "durationMs": max(0, round((time.monotonic() - started) * 1000)),
        "status": status,
    }
    for name in ("phase", "waitSeconds", "details"):
        if name in result:
            completion[name] = result[name]
    if operation == "status":
        state = result.get("state", {})
        completion.update(
            {
                "desiredState": state.get("desiredState"),
                "actualState": state.get("actualState"),
                "generation": state.get("desiredGeneration"),
                "live": result.get("live", {}),
            }
        )
    _log(
        level,
        "operation_completed",
        str(
            result.get("message")
            or (
                "status returned with a live-read error"
                if operation == "status" and result.get("live", {}).get("readError")
                else "controller operation completed"
            )
        ),
        **completion,
    )
    return result
