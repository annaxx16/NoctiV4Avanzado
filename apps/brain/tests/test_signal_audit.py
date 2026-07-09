from umbra.analytics.signal_audit import classify_rejection


def test_classify_rejection_liquidity_and_risk():
    flags = classify_rejection("spread_too_wide 0.1200")

    assert flags.liquidity_blocked is True
    assert flags.risk_blocked is False
    assert flags.exposure_blocked is False


def test_classify_rejection_exposure():
    flags = classify_rejection("gross_exposure_full 400.00>=400.00")

    assert flags.exposure_blocked is True
    assert flags.liquidity_blocked is False


def test_classify_rejection_edge_as_risk_gate():
    flags = classify_rejection("edge 0.0081 < min_edge 0.03")

    assert flags.risk_blocked is True
    assert flags.liquidity_blocked is False
