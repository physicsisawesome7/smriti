"""The production retrieval path (Qdrant) must give the same access guarantees
as the local index. Runs against Qdrant's in-process engine."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")

from smriti import evals
from smriti.api import Platform
from smriti.ask import ask
from smriti.cli import seed
from smriti.config import Settings


@pytest.fixture(scope="module")
def qtenant(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    tmp: Path = tmp_path_factory.mktemp("smriti-qdrant")
    s = Settings.from_env(
        data_dir=tmp,
        database_url=f"sqlite:///{(tmp / 'smriti.db').as_posix()}",
        scheduler=False,
        vector_backend="qdrant",
        qdrant_url=":memory:",
    )
    plat = Platform(s)
    seed(plat)
    return plat.tenant(None)


def test_qdrant_backend_is_used(qtenant):
    from smriti.index import QdrantIndex

    assert isinstance(qtenant.index, QdrantIndex) and len(qtenant.index) > 30


def test_qdrant_zero_leakage(qtenant):
    r = evals.leakage_suite(qtenant)
    assert r["violations"] == [] and r["probes"] > 50


def test_qdrant_answers_and_versions(qtenant):
    asha = qtenant.principal("asha")
    q = "What is the period for submitting the performance guarantee under clause 16.1?"
    assert "30 days" in ask(qtenant, asha, q, use_cache=False)["answer"]
    assert "21 days" in ask(qtenant, asha, q, as_of="2024-06-01", use_cache=False)["answer"]


def test_qdrant_golden(qtenant):
    r = evals.run_golden(qtenant)
    assert [row["id"] for row in r["rows"] if not row["pass"]] == []
