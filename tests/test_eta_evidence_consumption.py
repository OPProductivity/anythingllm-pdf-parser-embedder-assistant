"""Repeated/overlapping progress callbacks must not compound one forecast."""
from concurrent.futures import ThreadPoolExecutor
import pytest
import rag_pdf_gradio_app as g

pytestmark = pytest.mark.offline_deterministic


def test_duplicate_and_older_samples_cannot_reprice_again():
    gate = g.QueueEtaEvidenceGate()
    assert gate.claim(974.837)
    assert not gate.claim(974.837)
    assert not gate.claim(941.349)
    assert gate.claim(1004.837)
    assert g.QueueEtaEvidenceGate().claim(974.837)  # independent run


def test_concurrent_callbacks_consume_sample_once():
    gate = g.QueueEtaEvidenceGate()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(gate.claim, [974.837] * 100)) == 1


def test_actual_last_run_forecast_takes_only_one_bounded_step():
    samples = [
        {"elapsed_seconds": elapsed, "forecast_seconds": forecast}
        for elapsed, forecast in [(824.172, 1018), (869.248, 1058),
                                  (900.391, 1023), (941.349, 1042), (974.837, 1017)]
    ]
    gate = g.QueueEtaEvidenceGate()
    expected = 828
    for elapsed in (974.837, 974.852):
        decision = g.stable_queue_eta_reprice(
            expected, samples, observed_windows=10, current_elapsed=elapsed,
            confirmed_fraction=.91982, return_decision=True,
        )
        assert decision["status"] == "applied"
        if gate.claim(samples[-1]["elapsed_seconds"]):
            expected = decision["expected_seconds"]
    assert expected == 870  # formerly compounded to 914 on identical evidence


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_invalid_sample_does_not_consume_gate(value):
    gate = g.QueueEtaEvidenceGate()
    assert not gate.claim(value)
    assert gate.claim(70)
