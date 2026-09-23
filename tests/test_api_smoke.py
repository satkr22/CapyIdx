from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import capyidx


def test_public_api_indexes_and_retrieves_a_symbol(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the documented package-root workflow against a real index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "greeter.py").write_text(
        "class Greeter:\n"
        "    def greet(self) -> str:\n"
        "        return \"hello\"\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "add", "greeter.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CapyIdx Smoke Test",
            "-c",
            "user.email=capyidx-smoke@example.invalid",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "Initial fixture",
        ],
        cwd=repo,
        check=True,
    )

    # The public API derives the current branch from the process directory and
    # writes indexes under CAPYIDX_HOME, so keep both local to this test.
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CAPYIDX_HOME", str(tmp_path / "capyidx-home"))

    async def smoke() -> None:
        updates = [
            update
            async for update in capyidx.index_repo_iter(
                repo,
                max_chunk_size=128,
            )
        ]
        assert updates
        assert updates[-1].status == "done"

        lookup = await capyidx.lookup_symbol(
            repo,
            "Greeter",
            detail="body",
        )
        assert len(lookup.matches) == 1
        assert lookup.selected is not None
        assert lookup.selected.name == "Greeter"
        assert "class Greeter" in lookup.selected.reconstructed_text

        reconstructed = await capyidx.reconstruct_symbol(
            repo,
            lookup.matches[0].id,
        )
        assert reconstructed.id == lookup.matches[0].id
        assert reconstructed.name == "Greeter"

        resolved = await capyidx.resolve_lookup(repo, "Greeter")
        assert [code.id for code in resolved.codes] == [lookup.matches[0].id]

        async with capyidx.open_lookup(repo) as session:
            session_lookup = session.lookup("Greeter", detail="signature")
        assert session_lookup.selected is not None
        assert session_lookup.selected.name == "Greeter"

    asyncio.run(smoke())
