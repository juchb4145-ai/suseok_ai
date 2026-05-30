from apps.kiwoom_gateway import RestCoreClient
from trading.broker.models import GatewayEvent


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, json, headers, timeout):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _Response({"accepted": True})

    def get(self, url, params, headers, timeout):
        self.gets.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return _Response(
            {
                "commands": [
                    {
                        "type": "login",
                        "command_id": "cmd-gw-trace",
                        "payload": {
                            "transport_trace": {
                                "trace_id": "trace-cmd-gw",
                                "core_command_long_poll_response_at_utc": "2026-05-30T09:00:00.000+00:00",
                            }
                        },
                    }
                ]
            }
        )


def test_rest_core_client_post_event_adds_trace_metadata():
    session = _Session()
    client = RestCoreClient("http://127.0.0.1:8000", "token")
    client._session = session

    result = client.post_event(GatewayEvent(type="heartbeat", event_id="evt-post", payload={}))

    assert result["accepted"] is True
    trace = session.posts[0]["json"]["payload"]["transport_trace"]
    assert trace["gateway_event_post_start_at_utc"]
    assert trace["transport_mode"] == "rest_long_poll"
    assert client.last_event_post_ms >= 0


def test_rest_core_client_poll_commands_adds_gateway_receive_trace():
    session = _Session()
    client = RestCoreClient("http://127.0.0.1:8000", "token")
    client._session = session

    commands = client.poll_commands(wait_sec=0.1)

    assert commands[0].command_id == "cmd-gw-trace"
    trace = commands[0].payload["transport_trace"]
    assert trace["gateway_command_polled_at_utc"]
    assert trace["gateway_command_poll_duration_ms"] >= 0
    assert client.last_poll_command_count == 1
