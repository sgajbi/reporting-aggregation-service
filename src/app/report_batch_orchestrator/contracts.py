"""RFC-0104 batch reporting vocabulary and support posture.

This module centralizes batch-orchestration vocabulary without claiming full
product runtime support before gateway and Workbench surfaces exist.
"""

from typing import Final, Literal, get_args

BatchSelectorMode = Literal[
    "explicit_portfolio_list",
    "selected_subset",
    "all_active_portfolios",
    "batch_manifest",
]

BatchFrequency = Literal[
    "monthly",
    "quarterly",
    "semi_annual",
    "yearly",
    "explicit",
]

BATCH_CAPABILITY_KEY: Final = "lotus-report.reporting.batch_orchestration.v1"
BATCH_MATERIALIZATION_API_CAPABILITY_KEY: Final = (
    "lotus-report.reporting.batch_materialization_api.v1"
)
BATCH_CONTROL_API_CAPABILITY_KEY: Final = "lotus-report.reporting.batch_control_api.v1"
BATCH_SCHEDULER_ADMIN_API_CAPABILITY_KEY: Final = (
    "lotus-report.reporting.batch_scheduler_admin_api.v1"
)

#: Derived, not restated. `Final[tuple[BatchFrequency, ...]]` looks like it ties
#: the tuple to the alias, and it only half does: a *wrong* member is a type
#: error, a *missing* one is simply a shorter valid tuple. The failure is
#: one-directional and silent.
#:
#: It is not decorative either. `materialize_cycle` refuses anything outside
#: `BATCH_FREQUENCIES` with `unsupported_batch_frequency`, so a member added to
#: the alias and forgotten here passes model validation and is then refused at
#: runtime -- the type system says supported, the validator says not, and the
#: message blames the caller for a value the contract accepts. Same defect #343
#: removed from the lineage store, one layer along.
#:
#: `get_args` preserves declaration order, so `BATCH_SELECTOR_MODES`'s ordering
#: contract is unchanged and no consumer needs to know this moved.
BATCH_SELECTOR_MODES: Final[tuple[BatchSelectorMode, ...]] = get_args(BatchSelectorMode)

BATCH_FREQUENCIES: Final[tuple[BatchFrequency, ...]] = get_args(BatchFrequency)

BATCH_RUNTIME_SUPPORTED: Final = False
