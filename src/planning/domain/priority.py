from planning.domain.enums import WorkPriority

WORK_PRIORITY_ORDER = (
    WorkPriority.CRITICAL,
    WorkPriority.HIGH,
    WorkPriority.MEDIUM,
    WorkPriority.LOW,
)

WORK_PRIORITY_BONUSES = {
    WorkPriority.CRITICAL: 3_000_000,
    WorkPriority.HIGH: 2_000_000,
    WorkPriority.MEDIUM: 1_000_000,
    WorkPriority.LOW: 0,
}


def work_priority(value: object) -> WorkPriority:
    try:
        return WorkPriority(str(value))
    except ValueError:
        # Snapshots created before work-type priorities existed intentionally
        # become LOW, matching the database migration for existing data.
        return WorkPriority.LOW


def work_priority_rank(value: object) -> int:
    return WORK_PRIORITY_ORDER.index(work_priority(value))


def work_priority_bonus(value: object) -> int:
    return WORK_PRIORITY_BONUSES[work_priority(value)]
