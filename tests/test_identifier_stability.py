import os
import subprocess
import sys
from datetime import UTC, datetime

from pydantic import BaseModel

from pcfs.identifiers import canonical_json


def test_unordered_values_are_canonical_without_reordering_sequences():
    assert canonical_json({"ordered": [2, 1], "scopes": frozenset({"b", "a"})}) == (
        '{"ordered":[2,1],"scopes":["a","b"]}'
    )


def test_policy_and_event_identity_survive_different_process_hash_seeds():
    code = """
from pcfs.identifiers import content_digest
from pcfs.policy import PolicyRegistry
from pcfs.subscriptions.fixtures import demo_scenario
event = demo_scenario().events[0]
snapshot = event.new.model_copy(update={
    'fact': event.new.fact.model_copy(update={'required_scopes': frozenset({'read:a', 'read:b'})})
})
event = event.model_copy(update={'new': snapshot})
print(content_digest(PolicyRegistry.defaults().all()[0]))
print(event.key)
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code], text=True, env={**os.environ, "PYTHONHASHSEED": str(seed)}
        ).splitlines()
        for seed in (0, 1, 2, 3)
    ]
    assert all(len(digest) == 64 for output in outputs for digest in output)
    assert outputs[0] == outputs[1] == outputs[2] == outputs[3]


def test_existing_model_and_mapping_timestamp_encoding_is_preserved():
    class Timestamp(BaseModel):
        at: datetime

    instant = datetime(2026, 1, 1, tzinfo=UTC)
    assert canonical_json(Timestamp(at=instant)) == '{"at":"2026-01-01T00:00:00Z"}'
    assert canonical_json({"at": instant}) == '{"at":"2026-01-01T00:00:00+00:00"}'
