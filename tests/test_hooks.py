from hermes_curator_evolver import hooks


def test_hook_forwards_native_identity_and_outcome(monkeypatch):
    received = {}

    class Store:
        def record_tool_call(self, **kwargs):
            received.update(kwargs)

    monkeypatch.setattr(hooks, "_store", lambda: Store())

    hooks.on_post_tool_call(
        tool_name="skill_manage",
        args={"operations": []},
        result={"success": True},
        session_id="session-native",
        task_id="task-native",
        tool_call_id="call-native",
        turn_id="turn-native",
        api_request_id="api-native",
        status="blocked",
        error_type="PolicyError",
        error_message="sensitive message",
    )

    assert received["tool_call_id"] == "call-native"
    assert received["turn_id"] == "turn-native"
    assert received["api_request_id"] == "api-native"
    assert received["status"] == "blocked"
    assert received["error_type"] == "PolicyError"
    assert "error_message" not in received
    assert received["session_id"] == "session-native"
