from kiwoom.client import ConditionCandidateEvent, ConditionInfo, MockKiwoomClient


def test_mock_condition_loaded_event():
    client = MockKiwoomClient()
    received = []

    client.condition_loaded.connect(lambda conditions: received.append(conditions))
    client.set_conditions([(12, "주도테마"), (15, "코스피대형주")])
    client.emit_condition_loaded()

    assert received == [[ConditionInfo(12, "주도테마"), ConditionInfo(15, "코스피대형주")]]


def test_mock_condition_include_and_remove_events():
    client = MockKiwoomClient()
    included = []
    removed = []

    client.set_conditions([(3, "코스닥테마주")])
    client.condition_candidate_included.connect(lambda event: included.append(event))
    client.condition_candidate_removed.connect(lambda event: removed.append(event))

    client.emit_condition_include("코스닥테마주", "412350")
    client.emit_condition_remove("코스닥테마주", "412350")

    assert included == [
        ConditionCandidateEvent(
            condition_name="코스닥테마주",
            code="412350",
            condition_index=3,
            event_type="include",
            source="condition",
        )
    ]
    assert removed == [
        ConditionCandidateEvent(
            condition_name="코스닥테마주",
            code="412350",
            condition_index=3,
            event_type="remove",
            source="condition",
        )
    ]
