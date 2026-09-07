"""The batch vocabulary tuples derive from their aliases (#346).

`BATCH_FREQUENCIES` and `BATCH_SELECTOR_MODES` restated their aliases' members
by hand, sixteen lines below the declarations. `Final[tuple[BatchFrequency, ...]]`
looks like it binds them and only half does: a **wrong** member is a type error,
a **missing** one is a shorter tuple that still satisfies the annotation.

`BATCH_FREQUENCIES` is the runtime validator. `materialize_cycle` refuses
anything outside it with `unsupported_batch_frequency`, so a frequency added to
the alias and forgotten in the tuple passes model validation and is then refused
at runtime -- the type system says supported, the validator says not, and the
error blames the caller for a value the contract accepts.

This is the shape #343 removed from the lineage store, where `POSTURE_VALUES`
was a hand-written copy interpolated into a `CHECK` constraint and the model
accepted a posture the column rejected. Same defect, different layer, and the
same pin: `tests/unit/test_stored_value_narrowing.py` asserts
`POSTURE_VALUES == SNAPSHOT_POSTURES`.
"""

from __future__ import annotations

from typing import get_args

import pytest

from app.report_batch_orchestrator.contracts import (
    BATCH_FREQUENCIES,
    BATCH_SELECTOR_MODES,
    BatchFrequency,
    BatchSelectorMode,
)
from app.report_batch_orchestrator.schedule import BatchScheduleValidationError


def test_batch_frequencies_derives_from_its_alias() -> None:
    """A future hand-written copy fails here rather than drifting."""
    assert BATCH_FREQUENCIES == get_args(BatchFrequency)


def test_batch_selector_modes_derives_from_its_alias() -> None:
    assert BATCH_SELECTOR_MODES == get_args(BatchSelectorMode)


def test_declaration_order_is_preserved() -> None:
    """`BATCH_SELECTOR_MODES` carries an ordering contract that consumers read.

    `get_args` preserves declaration order, so this change is invisible to them.
    Asserted rather than assumed, because switching to a `set` or a sorted
    derivation would satisfy the equality tests above only if they were written
    against the same wrong thing -- these compare tuples, so order is included,
    and this test says out loud that the order matters.
    """
    assert BATCH_SELECTOR_MODES[0] == "explicit_portfolio_list"
    assert BATCH_FREQUENCIES[0] == "monthly"
    assert list(BATCH_FREQUENCIES) == sorted(BATCH_FREQUENCIES, key=list(BATCH_FREQUENCIES).index)


@pytest.mark.parametrize("frequency", get_args(BatchFrequency))
def test_every_declared_frequency_is_accepted_by_the_runtime_validator(
    frequency: BatchFrequency,
) -> None:
    """The behavioural half: the contract and the validator agree, member by member.

    The equality tests above prove the tuple matches the alias. This proves the
    thing that actually failed -- that a value the contract declares is not then
    refused by `materialize_cycle` as unsupported. Parameterised over the alias,
    so a frequency added tomorrow is exercised the day it is added.
    """
    assert frequency in BATCH_FREQUENCIES

    try:
        _materialize(frequency)
    except BatchScheduleValidationError as error:
        assert error.args[0] != "unsupported_batch_frequency", (
            f"{frequency!r} is declared by BatchFrequency but refused by the runtime validator"
        )


def _materialize(frequency: str) -> object:
    """Drive the real validator, tolerating the other validation it performs.

    Only the `unsupported_batch_frequency` verdict is under test here; a
    frequency may still be refused for an unrelated reason (an explicit cycle
    needs dates, for instance) and that is not this test's business.
    """
    from app.report_batch_orchestrator.models import BatchCycleRequest
    from app.report_batch_orchestrator.schedule import materialize_cycle

    return materialize_cycle(
        BatchCycleRequest.model_validate(
            {
                "frequency": frequency,
                "as_of_date": "2026-04-22",
            }
        )
    )
