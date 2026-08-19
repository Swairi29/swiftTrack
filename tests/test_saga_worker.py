# Tests for saga-worker/worker.py: run_saga()'s orchestration and
# compensation ordering, and process_order()'s claim/update/publish
# control flow. call_cms/call_wms/call_ros (the actual HTTP/TCP calls)
# are monkeypatched out, so these exercise real saga logic - including
# the success path and the compensation path required by the roadmap -
# without needing CMS/WMS/ROS or RabbitMQ running.
from unittest.mock import MagicMock

import pytest
import requests

from helpers import import_fresh


@pytest.fixture
def worker_module():
    return import_fresh("saga-worker", "worker")


# run_saga: orchestration + compensation ordering 

def test_run_saga_success_calls_all_three_in_order_and_compensates_nothing(worker_module, monkeypatch):
    calls = []
    monkeypatch.setattr(worker_module, "call_cms", lambda name, addrs: calls.append("cms") or "CMS-1")
    monkeypatch.setattr(worker_module, "call_wms", lambda oid, addrs: calls.append("wms") or {"packageId": "WMS-1", "status": "RECEIVED"})
    monkeypatch.setattr(worker_module, "call_ros", lambda addrs: calls.append("ros") or {"routeId": "ROS-1", "estimatedMinutes": 10})
    compensate_wms = MagicMock()
    compensate_cms = MagicMock()
    monkeypatch.setattr(worker_module, "compensate_wms", compensate_wms)
    monkeypatch.setattr(worker_module, "compensate_cms", compensate_cms)

    result = worker_module.run_saga("ORD-1", "Kandy Traders", ["123 Galle Rd"])

    assert calls == ["cms", "wms", "ros"]
    assert result == {
        "cms": "CMS-1",
        "wms": {"packageId": "WMS-1", "status": "RECEIVED"},
        "ros": {"routeId": "ROS-1", "estimatedMinutes": 10},
    }
    compensate_wms.assert_not_called()
    compensate_cms.assert_not_called()


def test_run_saga_ros_failure_compensates_wms_then_cms(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "call_cms", lambda name, addrs: "CMS-1")
    monkeypatch.setattr(worker_module, "call_wms", lambda oid, addrs: {"packageId": "WMS-1", "status": "RECEIVED"})

    def failing_ros(addrs):
        raise requests.RequestException("ROS down")

    monkeypatch.setattr(worker_module, "call_ros", failing_ros)

    compensation_calls = []
    monkeypatch.setattr(worker_module, "compensate_wms", lambda oid, pid: compensation_calls.append(("wms", pid)))
    monkeypatch.setattr(worker_module, "compensate_cms", lambda oid, cid: compensation_calls.append(("cms", cid)))

    with pytest.raises(worker_module.SagaFailedError) as excinfo:
        worker_module.run_saga("ORD-1", "Kandy Traders", ["123 Galle Rd"])

    assert excinfo.value.step == "ros"
    # Compensation must undo in reverse order: WMS (step 2) before CMS (step 1).
    assert compensation_calls == [("wms", "WMS-1"), ("cms", "CMS-1")]


def test_run_saga_wms_failure_compensates_only_cms(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "call_cms", lambda name, addrs: "CMS-1")

    def failing_wms(oid, addrs):
        raise OSError("WMS unreachable")

    monkeypatch.setattr(worker_module, "call_wms", failing_wms)

    compensation_calls = []
    monkeypatch.setattr(worker_module, "compensate_wms", lambda oid, pid: compensation_calls.append(("wms", pid)))
    monkeypatch.setattr(worker_module, "compensate_cms", lambda oid, cid: compensation_calls.append(("cms", cid)))

    with pytest.raises(worker_module.SagaFailedError) as excinfo:
        worker_module.run_saga("ORD-1", "Kandy Traders", ["123 Galle Rd"])

    assert excinfo.value.step == "wms"
    # WMS itself is what failed - there's nothing to undo there, only CMS.
    assert compensation_calls == [("cms", "CMS-1")]


def test_run_saga_cms_failure_triggers_no_compensation(worker_module, monkeypatch):
    def failing_cms(name, addrs):
        raise requests.RequestException("CMS down")

    monkeypatch.setattr(worker_module, "call_cms", failing_cms)

    compensation_calls = []
    monkeypatch.setattr(worker_module, "compensate_wms", lambda oid, pid: compensation_calls.append(("wms", pid)))
    monkeypatch.setattr(worker_module, "compensate_cms", lambda oid, cid: compensation_calls.append(("cms", cid)))

    with pytest.raises(worker_module.SagaFailedError) as excinfo:
        worker_module.run_saga("ORD-1", "Kandy Traders", ["123 Galle Rd"])

    assert excinfo.value.step == "cms"
    assert compensation_calls == []


# process_order: claim / update / publish control flow

def test_process_order_skips_when_claim_fails(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "claim_order", lambda order_id: False)
    run_saga_mock = MagicMock()
    update_order_mock = MagicMock()
    publish_event_mock = MagicMock()
    monkeypatch.setattr(worker_module, "run_saga", run_saga_mock)
    monkeypatch.setattr(worker_module, "update_order", update_order_mock)
    monkeypatch.setattr(worker_module, "publish_event", publish_event_mock)

    worker_module.process_order({"orderId": "ORD-1", "clientName": "X", "addresses": ["y"]})

    run_saga_mock.assert_not_called()
    update_order_mock.assert_not_called()
    publish_event_mock.assert_not_called()


def test_process_order_success_confirms_order_and_publishes_completed(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "claim_order", lambda order_id: True)
    result = {"cms": "CMS-1", "wms": {"packageId": "WMS-1"}, "ros": {"routeId": "ROS-1"}}
    monkeypatch.setattr(worker_module, "run_saga", lambda order_id, name, addrs: result)
    update_order_mock = MagicMock()
    publish_event_mock = MagicMock()
    monkeypatch.setattr(worker_module, "update_order", update_order_mock)
    monkeypatch.setattr(worker_module, "publish_event", publish_event_mock)

    worker_module.process_order({
        "orderId": "ORD-1", "clientName": "Kandy Traders",
        "clientUsername": "alice", "addresses": ["123 Galle Rd"],
    })

    update_order_mock.assert_called_once_with(
        "ORD-1", status="CONFIRMED", cms_order_id="CMS-1",
        wms_package_id="WMS-1", ros_route_id="ROS-1",
    )
    routing_key, payload = publish_event_mock.call_args[0]
    assert routing_key == "order.completed"
    assert payload["orderId"] == "ORD-1"


def test_process_order_failure_marks_failed_with_step_and_publishes_failed(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "claim_order", lambda order_id: True)

    def failing_saga(order_id, name, addrs):
        raise worker_module.SagaFailedError("ros", "circuit breaker open - ROS has failed repeatedly, giving it a cooldown")

    monkeypatch.setattr(worker_module, "run_saga", failing_saga)
    update_order_mock = MagicMock()
    publish_event_mock = MagicMock()
    monkeypatch.setattr(worker_module, "update_order", update_order_mock)
    monkeypatch.setattr(worker_module, "publish_event", publish_event_mock)

    worker_module.process_order({
        "orderId": "ORD-1", "clientName": "Kandy Traders",
        "clientUsername": "alice", "addresses": ["123 Galle Rd"],
    })

    update_order_mock.assert_called_once_with(
        "ORD-1", status="FAILED", failed_step="ros",
        failure_reason="circuit breaker open - ROS has failed repeatedly, giving it a cooldown",
    )
    routing_key, payload = publish_event_mock.call_args[0]
    assert routing_key == "order.failed"
    assert payload["failedStep"] == "ros"
