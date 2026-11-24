"""Synthetic checks for private failure redaction and absent-data skips."""

import pytest

from tests._private_checks import private_test


@pytest.mark.parametrize("failure", [AssertionError, ValueError, pytest.fail.Exception])
def test_private_check_redacts_values_and_exception_chains(failure) -> None:
    @private_test
    def check() -> None:
        if failure is AssertionError:
            assert {"SAMPLE": 1} == {"SAMPLE": 2}
        try:
            raise RuntimeError("SAMPLE CONTEXT")  # noqa: TRY301
        except RuntimeError as exc:
            raise failure("SAMPLE DETAILS") from exc

    with pytest.raises(pytest.fail.Exception) as caught:
        check()
    assert str(caught.value) == "private file-relative check failed"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.pytrace is False


def test_private_check_preserves_absent_dataset_skips() -> None:
    @private_test
    def check() -> None:
        pytest.skip("synthetic dataset absent")

    with pytest.raises(pytest.skip.Exception, match="synthetic dataset absent"):
        check()
