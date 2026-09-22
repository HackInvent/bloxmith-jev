"""TypeSafe System One adapter, independent of the framework implementation."""

from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from bloxsmith_app.block_api import BlockRuntimeOutput, BlockRuntimeResult

SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})")
MODEL_ID = re.compile(r"jev-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class JevError(ValueError):
    """A known-safe diagnostic that never includes provider bodies or credentials."""


class Cancelled(JevError):
    """The active call was retired; it must not emit an answer."""


def json_text(value):
    """Serialize only finite JSON values, with no implicit conversion of objects."""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise JevError("Expected finite JSON data, not binary or custom objects.") from None


def read_json(text):
    """Reject duplicate keys and non-JSON constants rather than losing questions."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        raise JevError("Invalid JSON: check syntax, duplicate keys and non-finite numbers.") from None


def structured(value):
    """Instructions/descriptions are non-empty text, objects or arrays."""
    return isinstance(value, (str, dict, list)) and bool(value) and (not isinstance(value, str) or bool(value.strip()))


def questions_config(raw):
    """Validate all independent questions before one batched API call (block limit: 64)."""
    if isinstance(raw, str):
        raw = read_json(raw)
    if not isinstance(raw, dict) or not 1 <= len(raw) <= 64:
        raise JevError("Questions must be a JSON object containing 1 to 64 questions.")
    if len(json_text(raw)) > 64000:
        raise JevError("Questions exceed the block's 64,000-character limit.")
    for key, question in raw.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise JevError("Each question needs a non-empty identifier (128 characters maximum).")
        if not isinstance(question, dict) or set(question) - {"type", "instructions", "criteria"}:
            raise JevError("Each question accepts only type, instructions and criteria.")
        if not structured(question.get("instructions")):
            raise JevError("Each question needs complete, non-empty instructions.")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "noul":
            if "criteria" in question and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"}
                    or not all(structured(v) for v in criteria.values())):
                raise JevError("Noul criteria must contain true and false descriptions, or be omitted.")
        elif kind == "choice":
            if (not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255
                    or any(not isinstance(k, str) or not k.strip() for k in criteria)
                    or any(v is not None and not structured(v) for v in criteria.values())):
                raise JevError("Choice needs 2 to 255 named options, with descriptions or null.")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or not all(structured(v) for v in criteria):
                raise JevError("Score needs 2 to 10 ordered, descriptive levels.")
        else:
            raise JevError("Question type must be noul, choice or score.")
    return read_json(json_text(raw))


def normalize_config(raw, model):
    """Ignore framework technical fields, never accept a literal key as a setting."""
    if raw is not None and not isinstance(raw, Mapping):
        raise JevError("Invalid Jev configuration.")
    defaults = model["config"]
    if set(raw or {}) - set(defaults) - {"position", "runtime_path", "runtime_path_label", "execution"}:
        raise JevError("Unsupported Jev setting; store credentials in the wallet only.")
    config = {key: (raw or {}).get(key, value) for key, value in defaults.items()}
    for key in ("model", "api_key_ref"):
        if not isinstance(config[key], str):
            raise JevError(f"Invalid {key}.")
        config[key] = config[key].strip()
    if not MODEL_ID.fullmatch(config["model"]):
        raise JevError("Use a Jev model ID, for example jev-latest or jev-1.13.0.")
    if config["api_key_ref"] and not SECRET_REF.fullmatch(config["api_key_ref"]):
        raise JevError("Use a full wallet reference, for example secret://workspace/typesafe_api.")
    for key, low, high in (("timeout_sec", 1, 120), ("max_state_chars", 1, 250000), ("max_retries", 0, 3)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not low <= value <= high:
            raise JevError(f"{key} must be an integer between {low} and {high}.")
        config[key] = int(value)
    # Preparation contexts recursively freeze authored dictionaries and lists.
    def thaw(value):
        if isinstance(value, Mapping):
            return {k: thaw(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(v) for v in value]
        return value
    config["questions"] = questions_config(thaw(config["questions"]))
    return config


def state_value(context, config):
    """Respect the input content type; JSON strings are parsed only on JSON deliveries."""
    raw = context.input_value(1, "state", default=None)
    content_type = str(context.input_content_type(1, "state", default="") or "").split(";")[0].strip()
    if isinstance(raw, str) and content_type == "application/json":
        raw = read_json(raw)
    if not isinstance(raw, (str, dict, list)) or (isinstance(raw, str) and not raw.strip()):
        raise JevError("state expects non-empty text, a JSON object or a JSON array.")
    size = len(raw) if isinstance(raw, str) else len(json_text(raw))
    if size > config["max_state_chars"]:
        raise JevError("State exceeds max_state_chars; split the document or raise the block limit.")
    json_text(raw)
    return raw


def check_cancel(context):
    """Observe public cancellation throughout the call and before publishing."""
    cancel = context.services.get("cancel_requested")
    if callable(cancel) and cancel():
        raise Cancelled("Jev execution cancelled.")


def resolve_key(context, ref):
    """Resolve a server-side wallet reference without exposing underlying failures."""
    if not ref:
        raise JevError("Set the TypeSafe wallet reference in the block properties.")
    resolver = context.services.get("resolve_secret")
    if not callable(resolver):
        raise JevError("The wallet secret resolver is unavailable.")
    try:
        key = resolver(ref)
    except Exception:
        raise JevError("TypeSafe key unavailable: unlock the wallet and check the reference.") from None
    if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key):
        raise JevError("The TypeSafe wallet secret is empty or invalid.")
    return key


def call_api(context, packet, timeout):
    """Run bounded HTTP off-hook; pipe credentials, reap on cancellation/timeout.

    The POSIX lifetime pipe also stops HTTP if the managed host is force-killed.
    No model input or key appears in command arguments, files or logs.
    """
    read_fd = write_fd = None
    process = None
    try:
        command = [sys.executable, "-I", str(Path(__file__).with_name("http_worker.py"))]
        options = {}
        if os.name == "posix":
            read_fd, write_fd = os.pipe()
            command.append(str(read_fd))
            options["pass_fds"] = (read_fd,)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **options)
        if read_fd is not None:
            os.close(read_fd)
            read_fd = None
        data = json_text(packet).encode("utf-8")
        deadline = time.monotonic() + timeout
        sent = False
        while True:
            check_cancel(context)
            if time.monotonic() >= deadline:
                raise JevError("Jev timed out. No partial answer was published.")
            try:
                stdout, _ = process.communicate(input=None if sent else data, timeout=.1)
                break
            except subprocess.TimeoutExpired:
                sent = True
        if process.returncode or len(stdout) > MAX_RESPONSE_BYTES:
            raise JevError("The Jev HTTP worker failed or returned too much data.")
        reply = read_json(stdout)
        if not isinstance(reply, dict):
            raise JevError("Invalid Jev HTTP response.")
        code = reply.get("error")
        if code:
            messages = {"auth": "TypeSafe rejected the key (HTTP 401/403). Check the wallet entry.",
                        "request": "TypeSafe rejected the request (HTTP 400/422). Check model, context size and questions.",
                        "busy": "TypeSafe is rate-limited or overloaded (HTTP 429/529). Retry later.",
                        "large": "TypeSafe response exceeds the block limit.",
                        "network": "TypeSafe connection failed or timed out.",
                        "redirect": "TypeSafe redirected the request. Credentials were not forwarded.",
                        "http": "TypeSafe returned an unexpected HTTP status."}
            raise JevError(messages.get(code, "Invalid TypeSafe response."))
        return read_json(reply.get("body", ""))
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def final_response(response, questions):
    """Validate typed, finite answers; preserve probabilities without imposing a policy."""
    def number(value, maximum=1):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum

    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not MODEL_ID.fullmatch(response["model"]):
        raise JevError("TypeSafe returned an invalid model identifier.")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("TypeSafe did not return exactly one answer per question.")
    result = {}
    for key, question in questions.items():
        answer = answers[key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise JevError("TypeSafe returned an unexpected answer type.")
        if kind == "noul":
            if not number(answer.get("noul")):
                raise JevError("TypeSafe returned an invalid yes probability.")
            result[key] = {"type": kind, "noul": answer["noul"]}
            continue
        options = set(question["criteria"]) if kind == "choice" else {str(i) for i in range(len(question["criteria"]))}
        probabilities = answer.get("probabilities")
        if (not number(answer.get("confidence")) or not isinstance(probabilities, dict)
                or set(probabilities) != options or not all(number(v) for v in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=.02)):
            raise JevError("TypeSafe returned an invalid probability distribution.")
        clean = {"type": kind, "confidence": answer["confidence"], "probabilities": probabilities}
        if kind == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in options:
                raise JevError("TypeSafe selected an option outside the question criteria.")
            clean["choice"] = choice
        else:
            if not number(answer.get("score"), len(options) - 1):
                raise JevError("TypeSafe returned a score outside the described scale.")
            legend = answer.get("legend")
            if not isinstance(legend, dict) or set(legend) != options or any(not structured(v) for v in legend.values()):
                raise JevError("TypeSafe returned an invalid score legend.")
            clean.update(score=answer["score"], legend=legend)
        result[key] = clean
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
        raise JevError("TypeSafe returned invalid token usage.")
    return {"model": response["model"], "answers": result,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")}}


def execute(context, model):
    """Make one logical batched request and emit only a complete JSON result in either mode."""
    try:
        config = normalize_config(context.config, model)
        check_cancel(context)
        body = {"model": config["model"], "state": state_value(context, config), "questions": config["questions"]}
        key = resolve_key(context, config["api_key_ref"])
        response = call_api(context, {"body": body, "key": key, "timeout": config["timeout_sec"], "retries": config["max_retries"]}, config["timeout_sec"])
        result = final_response(response, config["questions"])
        check_cancel(context)
        return BlockRuntimeResult(status="success", outputs=[BlockRuntimeOutput(port_id=1, value=json_text(result), content_type="application/json")],
                                  metadata={"jev": {"model": result["model"], "question_count": len(config["questions"]), "usage": result["usage"]}},
                                  logs=[f"[jev] Completed {len(config['questions'])} questions."], last_message="Jev evaluation complete.")
    except Cancelled:
        return BlockRuntimeResult(status="cancelled", outputs=[], last_message="Jev execution cancelled.")
    except Exception as error:
        message = str(error) if isinstance(error, JevError) else "Jev execution failed. Check configuration and connectivity."
        return BlockRuntimeResult(status="failed", outputs=[], error=message, last_message=message, logs=[f"[jev-error] {message}"])
