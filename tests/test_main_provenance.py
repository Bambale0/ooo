from ops.release.check_main_provenance import has_merged_main_pr


def test_main_provenance_accepts_merged_pr_to_main():
    assert has_merged_main_pr(
        [
            {
                "merged_at": "2026-09-25T12:00:00Z",
                "base": {"ref": "main"},
            }
        ]
    )


def test_main_provenance_rejects_open_pr():
    assert not has_merged_main_pr(
        [
            {
                "merged_at": None,
                "base": {"ref": "main"},
            }
        ]
    )


def test_main_provenance_rejects_pr_merged_to_other_branch():
    assert not has_merged_main_pr(
        [
            {
                "merged_at": "2026-09-25T12:00:00Z",
                "base": {"ref": "release"},
            }
        ]
    )


def test_main_provenance_rejects_missing_association():
    assert not has_merged_main_pr([])
