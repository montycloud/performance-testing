from mcperf.cfn.stack_names import make_stack_name, parse_stack_name


def test_round_trip():
    name = make_stack_name("mc-perftest", "sns", 1, 200)
    assert name == "mc-perftest-sns-000001-000200"
    parsed = parse_stack_name("mc-perftest", name)
    assert parsed["resource"] == "sns_topic"
    assert (parsed["start"], parsed["end"]) == (1, 200)


def test_foreign_and_malformed_names_are_ignored():
    assert parse_stack_name("mc-perftest", "other-sns-000001-000200") is None
    assert parse_stack_name("mc-perftest", "mc-perftest-lambda-roles") is None
    assert parse_stack_name("mc-perftest", "mc-perftest-zzz-000001-000200") is None
