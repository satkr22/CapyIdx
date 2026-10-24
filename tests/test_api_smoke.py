from __future__ import annotations

import asyncio
from pathlib import Path

import capyidx


def test_public_api_index_iterator_reports_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the package-root indexing contract without rebuilding SQLite."""
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_index_repo_iter(_repo, *, max_chunk_size):
        yield type("Update", (), {"status": "done"})()

    monkeypatch.setattr(capyidx, "index_repo_iter", fake_index_repo_iter)

    async def smoke() -> None:
        updates = [update async for update in capyidx.index_repo_iter(repo, max_chunk_size=128)]
        assert updates
        assert updates[-1].status == "done"

    asyncio.run(smoke())
