"""The turn user id has to travel with the Youli session id."""

from octop.infra.gateway.process.harness_request import build_harness_request


def test_harness_request_stamps_the_turn_user() -> None:
    text_request = build_harness_request(
        thread_id="thread-1",
        user_id=18,
        source="dashboard",
        text="报价",
        youli_sid="sid-18",
    )
    assert text_request["configurable"]["octop_user_id"] == 18
    assert text_request["configurable"]["youli_sid"] == "sid-18"

    message_request = build_harness_request(
        thread_id="thread-1",
        user_id=18,
        source="dashboard",
        messages=[{"role": "user", "content": "报价"}],
    )
    assert message_request["configurable"]["octop_user_id"] == 18
    assert "youli_sid" not in message_request["configurable"]
