"""Per-project autonomy dial — the tool-gating allowlist."""
from backend import autonomy


def test_categorisation_ranks():
    assert autonomy.tool_min_rank("read_file") == 0
    assert autonomy.tool_min_rank("write_file") == 1
    assert autonomy.tool_min_rank("run_gated") == 2
    assert autonomy.tool_min_rank("git_commit_request") == 3
    # an unknown tool defaults to full-only, never leaked to a restricted project
    assert autonomy.tool_min_rank("some_new_tool") == 3


def test_normalize_full_is_unrestricted():
    assert autonomy.normalize(None) is None
    assert autonomy.normalize("full") is None
    assert autonomy.normalize("garbage") is None
    assert autonomy.normalize("read_only") == "read_only"


def test_allows_by_level():
    # read_only: only reads
    assert autonomy.allows("read_only", "read_file")
    assert not autonomy.allows("read_only", "write_file")
    assert not autonomy.allows("read_only", "run_gated")
    assert not autonomy.allows("read_only", "git_commit_request")
    # stage: reads + writes, no runs
    assert autonomy.allows("stage", "write_file")
    assert not autonomy.allows("stage", "run_command")
    # gated: + runs, no commit
    assert autonomy.allows("gated", "run_gated")
    assert not autonomy.allows("gated", "git_commit_request")
    # full/None: everything, including unknowns
    assert autonomy.allows(None, "git_commit_request")
    assert autonomy.allows("full", "some_new_tool")


def test_filter_entries():
    entries = [{"name": n} for n in
               ("read_file", "write_file", "run_gated", "git_commit_request")]
    ro = [e["name"] for e in autonomy.filter_entries(entries, "read_only")]
    assert ro == ["read_file"]
    st = [e["name"] for e in autonomy.filter_entries(entries, "stage")]
    assert st == ["read_file", "write_file"]
    # unrestricted keeps everything
    assert len(autonomy.filter_entries(entries, None)) == 4
    assert len(autonomy.filter_entries(entries, "full")) == 4
