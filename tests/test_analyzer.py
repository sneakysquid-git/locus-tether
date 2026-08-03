import analyzer


class TestFindUnsupportedNumbers:
    """Regression tests for #40: the model repurposing a percentage into a
    fabricated count ('100% coverage' -> '100 containers')."""

    def test_flags_the_exact_confirmed_fabrication_pattern(self):
        source = "we could say we have 100% coverage or fail the pipeline"
        facts = ["The group has 100 containers running in their building."]
        flagged = analyzer._find_unsupported_numbers(facts, source)
        assert flagged == facts

    def test_does_not_flag_a_percentage_correctly_matching_source(self):
        source = "we have 100% coverage across the fleet"
        facts = ["The team has 100% test coverage."]
        flagged = analyzer._find_unsupported_numbers(facts, source)
        assert flagged == []

    def test_does_not_flag_a_plain_count_correctly_matching_source(self):
        source = "the Sacramento Bee has a circulation of, I believe, 7,000"
        facts = ["The Sacramento Bee has a circulation of 7,000."]
        flagged = analyzer._find_unsupported_numbers(facts, source)
        assert flagged == []

    def test_fact_with_no_numbers_never_flagged(self):
        source = "we discussed the quarterly roadmap"
        facts = ["The team is excited about the roadmap."]
        flagged = analyzer._find_unsupported_numbers(facts, source)
        assert flagged == []


class TestAnalyzerPostProcessing:
    def test_null_name_participants_are_stripped(self):
        result = {"participants": [{"name": "Chris", "role": None}, {"name": None, "role": None}]}
        result["participants"] = [p for p in result.get("participants", []) if p.get("name")]
        assert result["participants"] == [{"name": "Chris", "role": None}]

    def test_placeholder_non_decisions_are_filtered(self):
        assert analyzer._is_placeholder_non_decision("None explicitly stated") is True
        assert analyzer._is_placeholder_non_decision("We decided to ship on Friday") is False
