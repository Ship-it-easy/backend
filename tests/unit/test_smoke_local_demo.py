from scripts.smoke_local_demo import board_query_params


def test_verify_only_board_follows_current_project_date() -> None:
    manifest = {"planning_date": "2026-09-26", "comparison_date": "2026-09-27"}

    assert board_query_params(manifest, verify_only=True) is None


def test_initial_planning_board_uses_preparation_date() -> None:
    manifest = {"planning_date": "2026-09-26"}

    assert board_query_params(manifest, verify_only=False) == {
        "from": "2026-09-26",
        "days": 7,
    }
