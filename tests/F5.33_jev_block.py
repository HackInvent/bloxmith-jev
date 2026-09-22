#!/usr/bin/env python3
"""FB1/FB2/FB3: typed API contracts, secret safety, cancellation and installed runtimes."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import MappingProxyType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from blocs.jev.block import JevBlock
from blocs.jev.runtime import JevError, normalize_config, questions_config, final_response, read_json
from jev_fixtures import KEY, REF, QUESTIONS, answer, context, fake_api, node
from block_test_packages import install_test_package
from ui_smoke_common import (isolated_server, http_json, graph_payload, text_node, display_node, data_edge,
    create_project_api, create_run_api, wait_for_run_predicate, stop_run_api, prepare_run_api, play_run_api, get_run_api)


def rejected(callback):
    """Known invalid values must fail explicitly without echoing private data."""
    try:
        callback()
    except JevError as error:
        assert KEY not in str(error)
    else:
        raise AssertionError("Expected validation failure")


def test_contracts():
    """FB1/FB2: each primitive, structured data, validation and immutable preparation."""
    block = JevBlock()
    assert block.model["version"] == "0.1.0"
    assert questions_config(QUESTIONS) == QUESTIONS
    frozen = MappingProxyType({"questions": MappingProxyType({"s": MappingProxyType({"type": "score",
        "instructions": "Severity?", "criteria": ("Cosmetic", "Blocking")})}), "execution": {"inhibited": False},
        "position": {"x": 1}, "runtime_path": ["composite"], "runtime_path_label": "Composite"})
    assert normalize_config(frozen, block.model)["questions"]["s"]["criteria"] == ["Cosmetic", "Blocking"]
    for bad in ({}, [], {"q": {"type": "wrong", "instructions": "Question"}},
                {"q": {"type": "noul", "instructions": ""}},
                {"q": {"type": "noul", "instructions": "Question", "criteria": {"true": "Yes"}}},
                {"q": {"type": "score", "instructions": "Question", "criteria": ["Only one"]}},
                {"q": {"type": "choice", "instructions": "Question", "criteria": {"single": None}}},
                {"q": {"type": "choice", "instructions": "Question", "criteria": {str(i): None for i in range(256)}}},
                {str(i): QUESTIONS["action"] for i in range(65)}):
        rejected(lambda: questions_config(bad))
    for raw in ('{"q":{},"q":{}}', '{"x":NaN}', '{"x":Infinity}'):
        rejected(lambda: read_json(raw))
    for config in ({"api_key": KEY}, {"api_key_ref": KEY}, {"model": "not-jev"}, {"timeout_sec": False},
                   {"max_retries": -1}, {"max_state_chars": float("nan")}, {"timeout_sec": 999}):
        rejected(lambda: normalize_config(config, block.model))
    response = answer(QUESTIONS)
    assert final_response(response, QUESTIONS) == response
    assert "confidence" not in final_response(response, QUESTIONS)["answers"]["action"]
    for path, value in ((["answers", "action", "noul"], True), (["answers", "team", "choice"], "unknown"),
                        (["answers", "severity", "score"], 2), (["answers", "severity", "legend"], {}),
                        (["answers", "team", "probabilities"], {"code": .1, "other": .1}),
                        (["usage", "input_tokens"], -1), (["model"], KEY)):
        bad = copy.deepcopy(response)
        parent = bad
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = value
        rejected(lambda: final_response(bad, QUESTIONS))


def test_http_and_state():
    """FB1/FB2: real TLS/HTTP to a fake provider, one batched request, typed inputs."""
    block = JevBlock()
    with fake_api() as api:
        for value, content_type, expected in (("{not JSON}", "text/plain", "{not JSON}"),
                ('{"text":"please act","count":0,"enabled":false}', "application/json", {"text": "please act", "count": 0, "enabled": False}),
                (["hello", {"text": "there"}], "application/json", ["hello", {"text": "there"}])):
            result = block.execute_runtime(context(api.directory, value=value, content_type=content_type))
            assert result.status == "success", result
            assert len(result.outputs) == 1 and result.outputs[0].content_type == "application/json"
            assert json.loads(result.outputs[0].value) == answer(QUESTIONS)
            assert api.calls[-1]["body"] == {"model": "jev-latest", "state": expected, "questions": QUESTIONS}
            assert api.calls[-1]["authorization"] == "Bearer " + KEY
            assert KEY not in repr(result)
        assert len(api.calls) == 3
        for value, ctype in ((None, "text/plain"), (False, "application/json"), (42, "application/json"),
                             (b"audio", "application/json"), ("{bad", "application/json"), (" ", "text/plain")):
            assert block.execute_runtime(context(api.directory, value=value, content_type=ctype)).status == "failed"
        assert block.execute_runtime(context(api.directory, config={"max_state_chars": 1})).status == "failed"
        assert len(api.calls) == 3


def test_failures_and_retries():
    """FB2: no credentials/raw errors, no fallback answers, bounded retry policies."""
    block = JevBlock()
    with fake_api() as api:
        for mode in ("401", "422", "429", "529", "redirect", "large", "invalid-json", "malformed"):
            api.calls.clear()
            api.mode = mode
            result = block.execute_runtime(context(api.directory, config={"max_retries": 0}))
            assert result.status == "failed" and not result.outputs, (mode, result)
            assert KEY not in repr(result) and "private request body" not in repr(result)
            assert len(api.calls) == 1
        api.calls.clear()
        api.mode = "retry"
        assert block.execute_runtime(context(api.directory)).status == "success"
        assert len(api.calls) == 2
        api.calls.clear()
        api.mode = "529"
        assert block.execute_runtime(context(api.directory, config={"max_retries": 2})).status == "failed"
        assert len(api.calls) == 3
        count = len(api.calls)
        assert block.execute_runtime(context(api.directory, config={"api_key_ref": ""})).status == "failed"
        def locked(_):
            raise ValueError(KEY)
        result = block.execute_runtime(context(api.directory, services={"resolve_secret": locked}))
        assert result.status == "failed" and KEY not in repr(result)
        assert len(api.calls) == count


def test_cancel_timeout_and_ownership():
    """FB3: timeouts/Stop retire HTTP work and never publish late answers."""
    block = JevBlock()
    with fake_api() as api, ThreadPoolExecutor(max_workers=1) as pool:
        api.mode = "slow"
        stopped = threading.Event()
        future = pool.submit(block.execute_runtime, context(api.directory, services={"cancel_requested": stopped.is_set}))
        assert api.requests.wait(timeout=4)
        stopped.set()
        result = future.result(timeout=3)
        assert result.status == "cancelled" and not result.outputs
        start = time.monotonic()
        result = block.execute_runtime(context(api.directory, config={"timeout_sec": 1}))
        assert result.status == "failed" and not result.outputs
        assert time.monotonic() - start < 3
        api.release.set()
    # Simulate an abruptly killed managed host: its lifetime fd closes, so the
    # HTTP child must exit even if no cancellation callback can run anymore.
    if os.name == "posix":
        import subprocess
        with fake_api() as api:
            api.mode = "slow"
            read_fd, write_fd = os.pipe()
            worker = ROOT / "blocs" / "jev" / "http_worker.py"
            process = subprocess.Popen([sys.executable, "-I", str(worker), str(read_fd)], pass_fds=(read_fd,),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(read_fd)
            try:
                packet = {"key": KEY, "body": {"model": "jev-latest", "state": "hi", "questions": QUESTIONS}, "timeout": 10, "retries": 0}
                process.stdin.write(json.dumps(packet).encode()); process.stdin.close()
                assert api.requests.wait(timeout=4)
                os.close(write_fd); write_fd = None
                process.wait(timeout=2)
                assert not process.stdout.read()
            finally:
                if write_fd is not None:
                    os.close(write_fd)
                if process.poll() is None:
                    process.kill(); process.wait(timeout=2)


def graph():
    """One input, one downstream display; reusable in centralized/active tests."""
    return graph_payload("Jev package test", [text_node("seed", "State", "Please fix the code", 0, 0), node(),
        display_node("sink", "Result", 700, 0)], [data_edge("in", "seed", 1, "jev-test", 1), data_edge("out", "jev-test", 1, "sink", 1)])


def wallet(server):
    """Initialize only the isolated fixture's wallet with a dummy key."""
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "test-only-wallet-password"})
    http_json(server.base_url, "/api/application/secrets", method="POST", payload={"ref": REF, "value": KEY})


def test_installed_runtimes():
    """FB1/FB2/FB3: unmodified managed/linked packages, real wallet RPC and both engines."""
    for origin in ("managed", "linked"):
        with fake_api() as api, isolated_server() as server:
            install_test_package(server, "jev", origin=origin)
            wallet(server)
            for mode in ("centralized", "zeromq_active"):
                document = graph()
                project = create_project_api(server, document=document)["project"]
                previous = len(api.calls)
                created = create_run_api(server, document, project_id=project["project_id"], runtime_mode=mode)
                try:
                    run = wait_for_run_predicate(server, created["run_id"],
                        lambda r: r.get("node_statuses", {}).get("sink") == "success" or r.get("status") == "failed",
                        "Jev did not deliver a JSON result", timeout_sec=25)
                    assert run.get("node_statuses", {}).get("sink") == "success", run.get("logs")
                    assert "jev-1.13.0" in str(run.get("output_values"))
                    assert KEY not in json.dumps(run)
                    assert len(api.calls) == previous + 1
                finally:
                    stop_run_api(server, created["run_id"])
                print(f"[ok] Jev {origin} {mode}", flush=True)
            # A real editor-style Run does not contact the API until Play.
            api.mode = "slow"
            api.requests.clear()
            api.release.clear()
            previous = len(api.calls)
            prepared = prepare_run_api(server, graph(), runtime_mode="zeromq_active")
            assert len(api.calls) == previous
            play_run_api(server, prepared["run_id"])
            try:
                assert api.requests.wait(timeout=5)
            finally:
                stop_run_api(server, prepared["run_id"])
            api.release.set()
            run = get_run_api(server, prepared["run_id"])
            assert not run.get("output_values", {}).get("jev-test:1"), run.get("logs")
            assert KEY not in json.dumps(run)
            print(f"[ok] Jev {origin} live Stop", flush=True)


def main():
    for test in (test_contracts, test_http_and_state, test_failures_and_retries, test_cancel_timeout_and_ownership, test_installed_runtimes):
        test()
        print(f"[ok] {test.__name__}", flush=True)


if __name__ == "__main__":
    main()
