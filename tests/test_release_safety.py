import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    # Runtime inputs/outputs are intentionally local and ignored. Only files in
    # the Git index are part of the distributable source repository.
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    )
    tracked = [Path(name) for name in result.stdout.split("\0") if name]
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    for path in tracked:
        assert path.name != "case-set.json"
        assert not (
            path.parts[0] in {"inputs", "outputs", "traces", "dist"} and path.name != ".gitkeep"
        )
        assert not forbidden.intersection(path.parts)
        assert path.name != ".env"


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
