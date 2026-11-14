"""Dry runs must work offline and must never construct a cloud client."""

import pytest

from scripts import index_policy_chunks, print_cloud_setup, seed_firestore, upload_policy_docs


@pytest.fixture
def no_cloud(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("cloud client constructed during dry run")

    for mod, name in [
        (upload_policy_docs, "storage_client"),
        (seed_firestore, "firestore_client"),
        (index_policy_chunks, "firestore_client"),
    ]:
        monkeypatch.setattr(mod, name, boom)


def test_upload_dry_run(valid_env, no_cloud, capsys):
    assert upload_policy_docs.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "gs://test-bucket/policy_docs/DOC-CG-HVAC.md" in out
    assert out.count("-> gs://") == 6


def test_seed_dry_run(valid_env, no_cloud, capsys):
    assert seed_firestore.main([]) == 0
    out = capsys.readouterr().out
    for line in ("properties     2", "units          5", "residents      5", "work_orders   10"):
        assert line in out
    assert "demo_auth" not in out and "evaluation_cases" not in out


def test_index_dry_run(valid_env, no_cloud, capsys):
    assert index_policy_chunks.main([]) == 0
    out = capsys.readouterr().out
    assert "Chunks:     18 from 6 documents" in out
    assert "embed-test  dims=8" in out


def test_setup_commands_reflect_config(valid_env, capsys):
    assert print_cloud_setup.main() == 0
    out = capsys.readouterr().out
    assert "--location=us-central1" in out
    assert '"dimension": "8"' in out
    assert "--collection-group=policy_chunks" in out


def test_config_error_exits_2(valid_env, no_cloud, monkeypatch):
    monkeypatch.delenv("GCS_BUCKET")
    assert upload_policy_docs.main([]) == 2
