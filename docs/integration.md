# Using design-ingest from another project

For a project that needs to read PV / BESS documents (O&M assistant, tender tool,
research code). Everything below is stable within a `schema_version` (see the end).

## Install, pinned

```bash
pip install "design-ingest[llm] @ git+https://github.com/owl-solar/design-ingest@v0.6.0"
# DWG files also need the free ODA File Converter (see README); without it save drawings as DXF.
```

Pin a tag, not `master`. Upgrade on purpose: run your own checks, then change the tag.

## The four calls you need

```python
from design_ingest import ingest, ingest_dir
from design_ingest.search import search
from design_ingest.ask import ask

doc  = ingest("DS_inverter_datasheet.pdf")          # one file -> Document (cached by file content)
docs = ingest_dir("equipment_docs/")                # a folder; unreadable files are skipped, reasons in ingest_dir.last_errors

hits = search(docs, "default Modbus baud rate", doc_types=["protocol"], top_k=5)   # offline, nothing is sent
ans  = ask("Which register holds the daily yield?", docs, allow_external=True)     # sends ~8 passages to the API
```

## What a `Document` gives you

| Field | Use |
|---|---|
| `doc.chunks` | text pieces with `page` / `sheet` / `layer` / `section`: feed these to your own retrieval |
| `doc.tables` | columns + rows |
| `doc.warnings` | read them: "no text layer", "units not set", "page(s) with almost no text" |
| `doc.data["doc_info"]` | `doc_type` (datasheet, manual, drawing, protocol, certificate, pvsyst_report, bom, ...), `confidence`, file-name metadata (date, version, revision, language, drawing number) |
| `doc.data["summary"]` | format-specific key results (PAN/OND parameters, drawing title block and totals, PVsyst report results, datasheet fields) |
| `doc.data["specs"]` | datasheet key/value rows as typed quantities (`value`, `unit`, `temperature_c`, ...) |
| `doc.data["registers"]` | register / point lists: `name`, `address`, `n_registers`, `data_type`, `scale`, `unit`, `range`, `access`, `page` |

| `doc.data["fault_codes"]` | PDF manuals and alarm references: one record per fault / alarm code, `{code, name, measures (list of steps), page, page_end}` and, when the document has them, `severity`, `causes` (`[{id, cause, suggestion}]`), `columns` (other table columns by heading, e.g. SMA `S1`/`S2`/`R`), `code_ranges`, `shared_measures`. Layouts: row-ruled tables with a code column (Sungrow, SMA) and one section with tables per alarm (Huawei); found by `search` as one passage per code |

| `doc.data["fault_code_notes"]` | footnotes under a fault code table (a line starting with `*`) and rows whose "code" is not a code: `[{text, page}]`, text only, not interpreted; absent when there are none |

Units are in the key names (`dc_voltage_max_v`, `energy_p50_mwh_year`). Values that are not in the
document are absent, never guessed.

## Questions over documentation (the O&M case)

1. `search` is free and offline. Use it first; for exact values (a register, a limit) it is often enough.
2. `ask` adds a model for the final answer only. It sends the best passages, not the files, and returns
   `answer`, `found` and `citations`. **Show a citation (file and page) with every answer** and treat
   `verified=False` or `found=False` as "not answered". Quotes are checked in code, the answer text is not.
3. Restrict the search when you know the equipment: `ask(..., doc_types=["datasheet"], files=["DS_x.pdf"])`.

### External calls are off by default

Nothing is sent to an external API unless you allow it: `allow_external=True` per call, or the environment
variable `DESIGN_INGEST_ALLOW_EXTERNAL=1`. Set `ANTHROPIC_API_KEY` for the key. Every call is logged without
content (`design-ingest llm-log`); `DESIGN_INGEST_TOKEN_BUDGET` caps tokens per day. Set a spend limit in the
API console too. Do not enable it for documents you may not share with an external service.

### Hybrid search is optional and needs one download

`search(docs, q, mode="hybrid")` (also `build_index(..., mode="hybrid")`, `design-ingest search --mode hybrid`)
merges BM25 with a ranking by embedding similarity (reciprocal rank fusion, k=60, top 50 of each) and adds
`bm25_rank` and `embedding_rank` to every hit. `mode="bm25"` is the default and its results do not change.

- Install: `pip install "design-ingest[embeddings] @ git+...@v0.6.0"` (fastembed, ONNX, no PyTorch).
- The model `BAAI/bge-small-en-v1.5` (English, about 65 MB) is **not** downloaded by `hybrid`. Run
  `design-ingest download-model` (Python: `design_ingest.embeddings.download_model()`) once. Without it `hybrid`
  raises `EmbeddingsUnavailable` that names this command. Folder: `DESIGN_INGEST_MODEL_DIR`, default
  `~/.cache/design_ingest/models`.
- Network: `download-model` is a network request to the model host (Hugging Face). It sends no document.
  Embeddings are computed locally; passage texts and vectors are never sent anywhere, and `hybrid` itself
  makes no network call. This is separate from the External calls block above (that one is the Anthropic API).
- Vectors of passages are cached in `~/.cache/design_ingest/vectors.sqlite` (key: sha256 of the passage text +
  model name); the first index build embeds every passage, later builds only new ones.

## Practical notes

- **Speed.** Most files take under a second. An A1 drawing PDF takes about 20 s, a 100 MB DWG about 80 s, a
  100-page manual a few seconds. Results are cached in `~/.cache/design_ingest` (or `DESIGN_INGEST_CACHE`),
  so the second read is instant. Load a document set once and keep the `Document` list.
- **Scans.** PDFs without a text layer are flagged (`warnings`, `data["empty_pages"]`, `data["low_text_pages"]`)
  and are invisible to search. Do not tell a user "not in the documents" for such a file.
- **Naming conventions.** Layer and title-block names differ between offices: pass a profile
  (`ingest(path, profile="office.json")`, see `examples/profile.example.json`).
- **Errors.** `ParseError` for unreadable files (corrupt, encrypted); `ExternalCallBlocked` and `BudgetExceeded`
  for the model guard. Parsers never raise for odd content, they add warnings.

## Not covered yet

BESS documents (battery and PCS datasheets, EMS point lists, FAT/SAT reports) have no rules until samples exist;
scans need vision; datasheet field rules were written against inverter datasheets. Other equipment keeps its parsed
`specs` but not the fixed-name fields in `summary`.

## When a document reads badly

Do not patch around it in your project. Add the file to a private folder with an `expected.json` of the values you know
(`design-ingest eval FOLDER`, format at the top of `design_ingest/eval.py`), fix it here, release a new tag.

## Versions

`design_ingest.SCHEMA_VERSION` (currently `"1"`) changes only when an existing field or key is removed or changes
meaning; new fields and keys do not change it. `design_ingest.__version__` follows the tags. Parser versions are in
`doc.parser_version`; a cached result is refreshed when a parser changes.
