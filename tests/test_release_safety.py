from pathlib import Path


def test_competition_payload_is_gitignored() -> None:
    root = Path(__file__).resolve().parents[1]
    ignores = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"case-set.json", "inputs/*", "outputs/*", "traces/*"} <= set(ignores)
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in root.rglob("*"))


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
