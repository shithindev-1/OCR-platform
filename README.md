# OCR Platform

One project, two services - **Leasing OCR** (identity / company documents) and **Cheque OCR** (cheque
extraction and matching to rent payments) - built from the former `OCR_MULTI_FILES_API` and
`Paperless_cheque` projects. Every URL, request and response is unchanged.

```
app/                       every layer has a leasing/ and a cheque/ folder; what both use sits at its root
  api/
    v1/endpoints/leasing/  leasing, leasing_detect, ocr, admin_logs
    v1/endpoints/cheque/   extraction, matching
    v1/routers/            leasing.py, cheque.py (each service's router)
    leasing/  cheque/      request forms, API-key / error helpers of that service
  services/                shared: Azure Blob core, Gemini client, PDF render/write
    leasing/  cheque/      the OCR pipelines of each service
  schemas/leasing|cheque/  request and response models
  core/                    shared: config, X-API-Key auth, health, error dispatch, logging, timezones, Swagger fix
    leasing/  cheque/      each service's settings, errors, logging, usage logs; service.py mounts it into the app
  prompts/cheque/  utils/cheque/  data/leasing/
  factory.py               builds the one FastAPI app from whichever services are switched on
  main.py                  `app` for uvicorn / `python -m app`
config/       settings.json (shared defaults), settings.example.json
postman/      OCR_Platform.postman_collection.json (+ local environment)
scripts/      build_postman.py - regenerates the collection
tests/        leasing/ cheque/ (the original suites) and combined/ (the merged app + shared code)
```

## Two endpoints groups, one project, one or two processes

| | URL |
|---|---|
| Leasing | `POST /api/v1/ocr`, `/api/v1/ocr/leasing[/upload]`, `/api/v1/ocr/leasing/jobs[/upload]`, `GET /api/v1/ocr/leasing/jobs/{id}`, `/api/v1/ocr/leasing/detect[/upload]`, `/detect/jobs[/upload]`, `GET /api/v1/ocr/leasing/files` |
| Cheque | `POST /api/v1/cheques/extract`, `/api/v1/cheques/match`, `/api/v1/cheques/match/upload` |
| Both | `GET /health` (open, `{"status":"ok"}`), `GET /api/v1/health` (open, version + running services), Swagger `/docs` |

`SERVICES` (in `.env`, or on the command line) chooses what a process runs:

```powershell
python -m app                    # both services, one app, one port   (SERVICES=leasing,cheque)
python -m app leasing            # Leasing alone
python -m app cheque --port 8001 # Cheque alone, on its own port
```

Same code either way. To keep a Cheque crash or memory spike away from Leasing, run it as **two
processes** (two Windows services, two ports) behind one reverse-proxy URL; to try it all in one place,
run it as one. `uvicorn app.main:app` also works (it follows `SERVICES`).

## Run locally

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env        # fill GEMINI_API_KEY / GEMINI_MODEL; set REQUIRE_HTTPS=false for plain http
.venv\Scripts\python.exe -m app
```

Swagger: http://127.0.0.1:8000/docs - every upload endpoint has a **Choose file** button (the fix Cheque
had is now applied to Leasing's endpoints too). Postman: import `postman/OCR_Platform.postman_collection.json`
and the environment file; every direct-upload request is form-data with file rows - pick your files in the
Body tab. Regenerate after adding an endpoint with `python scripts/build_postman.py` (a test fails if the
collection and the API drift apart).

## Configuration

One `.env` (secrets + per-machine switches), `config/settings.json` (shared defaults, sections `common`,
`leasing`, `cheque`) and `config/settings.<APP_ENV>.json` (per environment). Priority: environment
variable > `.env` > settings file > default.

**A prefix gives a value to one service only:** `LEASING_GEMINI_API_KEY`, `CHEQUE_GEMINI_MODEL`,
`LEASING_AZURE_STORAGE_CONNECTION_STRING`, `CHEQUE_MAX_FILE_SIZE_MB`, ... beats the plain `GEMINI_API_KEY`
etc. that both read. That is how Leasing and Cheque get separate Gemini quotas and Blob accounts.

## Logs: separate per service, nothing mixed

Every record belongs to one bucket, by its logger name (`app/core/logging.py`):

| Bucket | Logger names | Files |
|---|---|---|
| Leasing | `uae_ocr`, `uae_ocr.*` | `logs/leasing/`: `ocr-api`, `errors`, `alerts`, `hits`, `results`, `token-breakdown`, `usage`, `document-usage`, `timing`, `document-results`, `rotation` |
| Cheque | `cheque`, `cheque.*` | `logs/cheque/<name>/<date>`: `app`, `errors`, `results`, `hits`, `token-breakdown`, `usage`, `document-usage`, `timing` |
| Common | everything else (Uvicorn, the Gemini SDK, httpx, shared helpers) | `logs/common/alerts/<date>.log`, WARNING and above |

- With two services in one process the buckets stay apart: a Cheque line never lands in a Leasing file or the
  other way round, and a library warning is written once, in `common`. A service that runs alone keeps
  everything in its own log (the common bucket only exists when two share a process).
- **Cheque token and cost logs**, like Leasing's: `hits` (every Gemini call: tokens, time, status, document type),
  `token-breakdown` (input split text / image / pdf, output split answer / thinking), `usage` (one line per request:
  each kind of call and the total), `document-usage` (one line per document with its type and tokens; they add up
  to the request's `usage` line), `timing` (request, stage and per-document times). A retry is logged as its own
  call: its tokens are billed. A cheque file's document type is `cheque`. `estimated_cost_usd` is empty: multiply
  the tokens by the model's current price.
- **Every flow logs each step** with its request id: `START`, each file accepted or rejected, validation,
  extraction (with the quality check), matching, cropping, case study, `SUMMARY`; Leasing logs `Request started` /
  `Request finished` around its existing stage lines.
- **stdout:** every line of every file is also printed, tagged with its file (`[cheque-api]`, `[cheque-hits]`,
  `[ocr-api]`, `[usage]` ...). `LOG_TO_CONSOLE` (or `LEASING_LOG_TO_CONSOLE` / `CHEQUE_LOG_TO_CONSOLE`) =
  `all` | `warnings` | `off`.

## What is shared now (one implementation instead of two)

| Concept | Before | Now |
|---|---|---|
| X-API-Key check + Swagger scheme | 2 copies, 2 schemes | `core/security.py`, one scheme |
| Health | 2 (`/health`, `/api/v1/health`) | `core/health.py`, both URLs kept |
| Settings base, `.env`/JSON sources, key rules | 2 systems | `core/config.py` |
| Gemini client construction | 2 | `services/gemini_client.py` |
| Azure Blob connect / download / upload / warm-up | 2 | `services/blob.py` (each service keeps its own URL rules) |
| PDF render and image-to-PDF | PDFium+Pillow vs PyMuPDF | `services/pdf.py` (PDFium + Pillow) |
| UAE / IST timezones | 2 | `core/timezones.py` |
| Swagger file-picker patch | Cheque only | `core/swagger.py`, whole app |
| Exception handling | 2 apps | `core/errors.py` picks the handler by URL; each service keeps its own error format (`{"detail"}` / `errorInfo` vs `{"error"}`) |

## Tests

```powershell
.venv\Scripts\python.exe -m pytest        # 670 tests: Leasing 447, Cheque 178, merged app + shared code 45
.venv\Scripts\python.exe -m ruff check app tests scripts
```

No test calls Gemini or Azure.

## Differences from the two original projects (review before UAT)

1. **Cheque no longer uses PyMuPDF.** Rendering a PDF page and writing the one-page processed-cheque PDF
   now go through PDFium and Pillow, like Leasing. Tests pass, but the processed PDFs differ byte-for-byte:
   compare a few real cheques in UAT. (PyMuPDF's native library is also what Windows Application Control
   blocked on the development machine.)
2. **Entry point:** `python -m app [leasing|cheque]` serves both services, or one. Leasing's own
   `python -m app` still starts Leasing's routes, but now also Cheque unless `SERVICES=leasing`. The
   Windows service (NSSM) must set `SERVICES` or pass the service name; the deploy scripts have not been
   adapted or run yet.
3. **Log folders:** `logs/leasing/...` and `logs/cheque/{app,results}/...` (Leasing's files used to be
   directly under `logs/`).
4. **`API_KEYS` is now required outside `APP_ENV=local` for Leasing too** (Cheque already required it).
5. **Cheque's `cors_origins`, `docs_enabled`, `log_level` are process-wide** (`common` section) and CORS is
   off unless origins are listed (Cheque's code default was `*`; its shipped settings file already set `[]`).
6. `GET /health` now exists in a Cheque-only process, and `/api/v1/health` in a Leasing-only one.
7. Library pins: Leasing's exact versions for the shared packages; `pymupdf` removed.
