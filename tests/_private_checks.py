"""Fixed failure output for file-relative private checks."""

from collections.abc import Callable
from functools import wraps

import pytest


def private_test[**P](check: Callable[P, None]) -> Callable[P, None]:
    """Preserve skips and fail outside the handler without source exceptions."""

    @wraps(check)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            check(*args, **kwargs)
        except (Exception, pytest.fail.Exception):
            pass
        else:
            return
        pytest.fail("private file-relative check failed", pytrace=False)

    return run
