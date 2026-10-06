from scripts.processing_report import reduction_percent


def test_reduction_percent() -> None:
    assert reduction_percent(60, 100) == 40.0
    assert reduction_percent(70, 100) == 30.0
    assert reduction_percent(10, None) is None
