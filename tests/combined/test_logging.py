"""Logs stay apart per service, and Cheque accounts for its tokens like Leasing does.

- Every Gemini attempt (retries too) is a line in Cheque's hits and token-breakdown logs, with the
  document type and the tokens.
- A request adds its usage line, one line per document, and its timing line; the documents add up to
  the request.
- With two services in one process, a record of one never lands in the other's files, and a library's
  warning is written once, in the common log.
- Everything is also printed on stdout, tagged with its file.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.core.cheque import usage
from app.core.cheque.logging import JSON_LOGS, setup_logging
from app.core.cheque.tracing import RequestTrace
from app.core.leasing.config import settings as leasing_settings
from app.core.leasing.logging import AlertsFileHandler, LibraryConsoleHandler, configure_logging
from app.core.logging import configure_common_logging
from app.schemas.cheque import TokenUsage
from app.services.cheque.gemini.generation import generate_structured

HITS, BREAKDOWN, USAGE, DOCUMENTS, TIMING = (
    "cheque.hits", "cheque.token_breakdown", "cheque.usage", "cheque.document_usage", "cheque.timing",
)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(record.getMessage()))


@pytest.fixture
def logs():
    """Collectors on Cheque's JSON logs; every logger and handler touched here is put back afterwards."""
    names = {"", "uvicorn", "cheque", *JSON_LOGS, "uae_ocr"}
    names |= {n for n in logging.root.manager.loggerDict if n.startswith(("uae_ocr", "cheque"))}
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level,
                 logging.getLogger(n).propagate) for n in names}
    for n in ("", "uvicorn"):  # the session's own alerts / library handlers: replaced by the test's
        for handler in list(logging.getLogger(n).handlers):
            if isinstance(handler, (AlertsFileHandler, LibraryConsoleHandler)):
                logging.getLogger(n).removeHandler(handler)
    collectors: dict[str, _Collect] = {}
    for n in (HITS, BREAKDOWN, USAGE, DOCUMENTS, TIMING):
        logger = logging.getLogger(n)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        collectors[n] = _Collect()
        logger.addHandler(collectors[n])
    yield collectors
    for n in list(logging.root.manager.loggerDict) + [""]:
        logger = logging.getLogger(n)
        if n in saved:
            for handler in list(logger.handlers):
                if handler not in saved[n][0]:
                    logger.removeHandler(handler)
                    handler.close()
            logger.handlers[:] = saved[n][0]
            logger.setLevel(saved[n][1])
            logger.propagate = saved[n][2]
        elif n.startswith(("uae_ocr", "cheque")):
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


class _Answer(BaseModel):
    value: int


def _response(*, parsed=None, text="", prompt=100, answer=10, thinking=5, image=90, text_tokens=10):
    meta = SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=answer, thoughts_token_count=thinking,
        total_token_count=prompt + answer + thinking, cached_content_token_count=None,
        prompt_tokens_details=[
            SimpleNamespace(modality=SimpleNamespace(name="IMAGE"), token_count=image),
            SimpleNamespace(modality=SimpleNamespace(name="TEXT"), token_count=text_tokens),
        ],
    )
    return SimpleNamespace(parsed=parsed, text=text, usage_metadata=meta, candidates=[])


def _client(*responses):
    queue = list(responses)

    async def generate_content(**_):
        return queue.pop(0)

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


async def test_every_gemini_attempt_is_logged_with_tokens_and_document_type(logs):
    client = _client(_response(text="not json"), _response(parsed=_Answer(value=1)))

    answer, tokens = await generate_structured(
        client, "test-model", [], None, _Answer, label="a.jpg", content_type="image/jpeg",
    )

    assert answer.value == 1 and tokens.total_tokens == 115
    first, second = logs[HITS].lines
    assert (first["call"], first["attempt"], first["status"]) == ("extraction", 1, "invalid_output")
    assert (second["call"], second["attempt"], second["status"]) == ("model_retry", 2, "success")
    assert first["document_type"] == "cheque" and first["filename"] == "a.jpg"
    assert first["content_type"] == "image/jpeg" and first["model"] == "test-model"
    # The unusable answer is billed too: its tokens are logged, output = answer + thinking
    assert (first["input_tokens"], first["output_tokens"], first["total_tokens"]) == (100, 15, 115)
    assert first["input_token_breakdown"] == {
        "system_instruction_tokens": None, "prompt_tokens": 10, "image_tokens": 90, "pdf_tokens": None,
    }
    assert first["estimated_cost_usd"] is None
    split = logs[BREAKDOWN].lines[0]
    assert (split["text_output_tokens"], split["thinking_output_tokens"]) == (10, 5)
    assert (split["input_tokens_total"], split["output_tokens_total"]) == (100, 15)
    assert split["hit_id"] == first["hit_id"]


async def test_a_call_that_fails_before_any_answer_is_logged_without_tokens(logs):
    async def refuse(**_):
        raise ValueError("boom")

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=refuse)))
    with pytest.raises(ValueError):
        await generate_structured(client, "m", [], None, _Answer, max_retries=0, label="x.pdf")

    [line] = logs[HITS].lines
    assert line["status"] == "invalid_output" and line["input_tokens"] is None and line["filename"] == "x.pdf"


def test_a_request_writes_usage_per_document_and_timing_lines(logs):
    trace = RequestTrace("POST", "/api/v1/cheques/extract", None, None, {})
    trace_token, usage_token = trace.activate(), usage.start_request()
    try:
        trace.set(type="extract")
        trace.count(files=2, cheques_found=3)
        trace.new_file(name="a.jpg", status="success", cheques=[{}, {}], load_ms=0, queue_wait_ms=0,
                       model_ms=2000, quality_check_ms=50, extraction_ms=2100)
        trace.new_file(name="b.pdf", status="success", cheques=[{}], model_ms=1000, extraction_ms=1100)
        with trace.stage("extraction"):
            pass
        started = 0.0
        usage.record_call(file="a.jpg", content_type="image/jpeg", model="m", call="extraction", attempt=1,
                          status="invalid_output", started_at=started,
                          usage=TokenUsage(prompt_tokens=100, output_tokens=10, thinking_tokens=0, total_tokens=110))
        usage.record_call(file="a.jpg", content_type="image/jpeg", model="m", call="model_retry", attempt=2,
                          status="success", started_at=started,
                          usage=TokenUsage(prompt_tokens=100, output_tokens=20, thinking_tokens=5, total_tokens=125))
        usage.record_call(file="b.pdf", content_type="application/pdf", model="m", call="extraction",
                          attempt=1, status="success", started_at=started,
                          usage=TokenUsage(prompt_tokens=200, output_tokens=30, thinking_tokens=0, total_tokens=230))
        usage.log_request_usage(trace.finish(200))
    finally:
        usage.end_request(usage_token)
        trace.deactivate(trace_token)

    [request] = logs[USAGE].lines
    assert request["request_id"] == trace.request_id and request["document_type"] == "cheque"
    assert request["extraction"]["calls"] == 2 and request["model_retry"]["calls"] == 1
    assert request["request_total"] == {"calls": 3, "input_tokens": 400, "output_tokens": 65, "total_tokens": 465}

    a, b = logs[DOCUMENTS].lines
    assert (a["document"], a["document_type"], a["cheques_found"]) == ("a.jpg", "cheque", 2)
    assert (b["document"], b["document_type"], b["cheques_found"]) == ("b.pdf", "cheque", 1)
    assert a["document_total"]["total_tokens"] + b["document_total"]["total_tokens"] == 465  # adds up
    assert a["model_retry"]["calls"] == 1 and b["model_retry"]["calls"] == 0

    [timing] = logs[TIMING].lines
    assert timing["document_count"] == 2 and "extraction" in timing["stages"]
    assert timing["documents"][0]["steps"]["model"] == {"seconds": 2.0}
    # The model call is part of the extraction step: a document's own time counts it once
    assert [d["document_seconds"] for d in timing["documents"]] == [2.1, 1.1]


def _read(folder: Path, pattern: str = "*") -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(folder.glob(pattern)))


def _flush() -> None:
    for name in list(logging.root.manager.loggerDict) + ["", "uvicorn"]:
        for handler in logging.getLogger(name).handlers:
            handler.flush()


def test_two_services_in_one_process_keep_their_logs_apart(logs, tmp_path):
    setup_logging("INFO", str(tmp_path / "cheque"), shared=True, console="off")
    configure_logging(leasing_settings, tmp_path / "leasing", shared=True)
    configure_common_logging(tmp_path)

    logging.getLogger("cheque.match").warning("CHEQUE-LINE")
    logging.getLogger("uae_ocr.leasing").warning("LEASING-LINE")
    logging.getLogger("google_genai.models").warning("LIBRARY-LINE")
    logging.getLogger("uvicorn.error").error("SERVER-LINE")
    _flush()

    cheque = _read(tmp_path / "cheque" / "app") + _read(tmp_path / "cheque" / "errors")
    assert "CHEQUE-LINE" in cheque and "LEASING-LINE" not in cheque and "LIBRARY-LINE" not in cheque
    leasing = _read(tmp_path / "leasing", "*.log")
    assert "LEASING-LINE" in leasing and "CHEQUE-LINE" not in leasing
    assert "LIBRARY-LINE" not in leasing and "SERVER-LINE" not in leasing
    alerts = (tmp_path / "leasing" / "alerts.log").read_text(encoding="utf-8")
    assert "LEASING-LINE" in alerts and "CHEQUE-LINE" not in alerts
    common = _read(tmp_path / "common" / "alerts")
    assert "LIBRARY-LINE" in common and "SERVER-LINE" in common
    assert "CHEQUE-LINE" not in common and "LEASING-LINE" not in common
    assert common.count("LIBRARY-LINE") == 1  # written once, not once per service


def test_cheque_alone_keeps_everything_in_its_own_log_as_before(logs, tmp_path):
    setup_logging("INFO", str(tmp_path / "cheque"), shared=False, console="off")

    logging.getLogger("cheque.match").info("CHEQUE-LINE")
    logging.getLogger("google_genai.models").warning("LIBRARY-LINE")
    _flush()

    text = _read(tmp_path / "cheque" / "app")
    assert "CHEQUE-LINE" in text and "LIBRARY-LINE" in text
    assert "LIBRARY-LINE" in _read(tmp_path / "cheque" / "errors")


def test_cheque_logs_also_go_to_stdout_tagged_with_their_file(logs, tmp_path, capsys):
    setup_logging("INFO", str(tmp_path / "cheque"), shared=True, console="all")

    logging.getLogger("cheque.match").info("a flow step")
    logging.getLogger(HITS).info('{"hit_id": "abc"}')
    logging.getLogger("cheque.match").warning("a problem")
    out = capsys.readouterr().out

    assert "[cheque-api] " in out and "a flow step" in out
    assert '[cheque-hits] {"hit_id": "abc"}' in out
    assert "!! [cheque-api] " in out and "a problem" in out


def test_console_off_prints_nothing_but_still_writes_the_files(logs, tmp_path, capsys):
    setup_logging("INFO", str(tmp_path / "cheque"), shared=True, console="off")

    logging.getLogger("cheque.match").info("quiet step")
    logging.getLogger(HITS).info('{"hit_id": "abc"}')
    _flush()

    assert capsys.readouterr().out == ""
    assert "quiet step" in _read(tmp_path / "cheque" / "app")
    assert "abc" in _read(tmp_path / "cheque" / "hits")
