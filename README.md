# design-ingest

Read the inputs of a PV / BESS design project — datasheets, specifications,
tender documents, drawings, site files, PVsyst component files — into **one
structured format** that scripts, dashboards and LLM/RAG pipelines can use.

Built as a reusable module, so that PV / BESS design and analysis tools do not
each re-implement file parsing.

| Format | What you get |
|---|---|
| **PDF** | text per page, tables (section rows folded into keys, no duplicate text), pages without text layer flagged, sheet sizes in mm; **CAD sheets (A3+)**: title block, module table counts and total, coordinates, transformer stations in `summary`, rotated labels kept; **PVsyst reports**: system size, models, P50/P90, PR, loss diagram, monthly energy in `summary` |
| **DOCX** | paragraphs with their heading path (`4 Technical > 4.2 BESS`), tables |
| **XLSX / CSV** | every sheet as a table (header row found below titles, merged cells repeated); CSV encoding, delimiter, metadata, units row, time range and column stats (PVsyst/SCADA/TMY exports) |
| **DXF / DWG** | block counts incl. nested blocks (tables × modules), block attributes as tables, title block, text per layer, line length and closed areas per layer in m / m², dimensions, layers |
| **KML / KMZ / GeoJSON / Shapefile** | features with folder, description, properties; polygon area (ha), line length (m), points; CRS noted (planar areas for projected shapefiles) |
| **PVsyst .PAN / .OND** | full parameter tree + clean summary (Pnom, Voc, Isc, temperature coefficients, MPPT window, efficiency, Gamma, LID, low-irradiance and IAM / efficiency profiles) |

On top of every file the package adds, without an LLM:

| Added to `doc.data` | What |
|---|---|
| `doc_info` | document type (datasheet, manual, drawing, protocol, certificate, PVsyst report, BOM ...), confidence, and file-name metadata (date, version, revision, language, drawing number) |
| `specs`, `summary` | datasheet key/value tables as typed quantities (`{"value": 352, "unit": "kVA", "temperature_c": 30}`) and fixed-name fields (`dc_voltage_max_v`, `mppt_voltage_range_v`, `ac_power`, `efficiency_max_pct`, `ip_rating`, ...) |
| `registers` | register / point lists (Modbus, SCADA, EMS) with address, type, scale, unit |

`design_ingest.quantities.parse_quantities("500 V – 1500 V")` is usable on its own.

Optional: **LLM extraction** into typed schemas (module, inverter, battery
datasheets; tender requirements) with Claude, validated with pydantic.

## Install

```bash
# while developing: editable, changes are picked up immediately
pip install -e path/to/design-ingest

# in another project: pinned version from GitHub
pip install "design-ingest @ git+https://github.com/owl-solar/design-ingest@v0.2.0"

# extras
pip install -e "path/to/design-ingest[llm]"     # LLM extraction (anthropic)
pip install -e "path/to/design-ingest[dev]"     # tests
```

Core dependencies are small: `pydantic`, `pdfplumber`, `python-docx`,
`openpyxl`. DXF, KML and PVsyst parsers are pure Python.

**DWG** needs the free [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter);
it is found automatically in `C:\Program Files\ODA\...`, or set
`ODA_FILE_CONVERTER` to the exe path. Without it, save drawings as DXF.

## Use

```python
from design_ingest import ingest, ingest_dir

doc = ingest("layout.dxf")
doc.data["blocks"]["MODULE"]["inserts_model_total"]   # module count incl. nested blocks
doc.data["lengths"]["CABLE_MV"]["m"]                   # MV cable length on that layer
doc.data["title_block"]                                # DWG_NO, REV, ...

site = ingest("site.kmz")
[(p["name"], p["area_ha"]) for p in site.data["placemarks"] if p["area_ha"]]

pan = ingest("module.pan").data["summary"]             # pnom_w, voc_v, mu_pmp_pct_per_c, ...

spec = ingest("technical_specification.docx")
for c in spec.chunks:
    print(c.section, "|", c.text[:80])

docs = ingest_dir("tender_package/")                   # everything supported in a folder
prompt_text = "\n\n".join(d.to_markdown() for d in docs)
```

Every parser returns a `Document`:

```
Document
├── chunks   text pieces with location: page / sheet / layer / section
├── tables   columns + rows  (.to_markdown(), .to_dataframe())
├── data     format-specific results (documented at the top of each parser)
├── metadata author, dates, AutoCAD version, encoding ...
└── warnings things to check (no text layer, units not set, hidden sheets ...)
```

Results are cached by file content (`~/.cache/design_ingest`, or
`DESIGN_INGEST_CACHE`), so re-reading an unchanged file is instant.

### LLM extraction (optional)

```python
from design_ingest import extract
from design_ingest.schemas import ModuleDatasheet, TenderRequirements

ds = extract("module_datasheet.pdf", ModuleDatasheet)
ds.variants[0].pmax_w, ds.temp_coeff_pmax_pct_per_c

reqs = extract("tender.pdf", TenderRequirements, instructions="Focus on BESS technical requirements")
[r for r in reqs.requirements if r.mandatory]

extract("drawing.pdf", MySchema, vision=True)   # send the PDF itself: sees drawings and charts
```

Needs `ANTHROPIC_API_KEY` (API billing is separate from a Claude.ai
subscription). Default model is small and cheap; override with `model=` or
`DESIGN_INGEST_MODEL`. Any pydantic model works as a schema. Extraction results
are cached too. **Confidential documents** are sent to the API — check with the
document owner first.

### Command line

```bash
design-ingest formats
design-ingest parse layout.dxf            # summary + warnings
design-ingest parse spec.docx --md        # readable Markdown
design-ingest parse site.kmz --json site.json
design-ingest dir tender_package/ --out parsed/ --md
design-ingest extract datasheet.pdf --schema module
design-ingest parse layout.dxf --profile office.json      # naming conventions of one design office
design-ingest eval my_files/                              # check results against my_files/expected.json
```

### Profiles: naming conventions outside the code

Layer names, block names and title-block labels differ between design offices.
`profile=` (a JSON file or dict, or the `DESIGN_INGEST_PROFILE` variable) replaces
the built-in defaults without touching code: see `design_ingest/profiles.py` and
`examples/profile.example.json`.

```python
ingest("layout.dwg", profile="office.json").data["summary"]["length_m_by_group"]
```

### Checking against files you know

Put real files in a folder (git-ignored: `samples/private/`) with an `expected.json`
that lists values you know (module count, voltage, register address ...).
`design-ingest eval FOLDER` prints PASS / FAIL per file and exits with 1 on any
failure: a regression test for the real documents that cannot live in the repository.
`--snapshot out.json` writes the current values as a starting point. Format: see the
top of `design_ingest/eval.py`.

## Limits

- Scanned PDFs (no text layer) are flagged, not OCR'd; use `extract(..., vision=True)`.
  Pages with a big image and a thin text layer are listed in `data["low_text_pages"]`.
- The datasheet field rules (`specs.py`) were written against inverter datasheets;
  other equipment keeps its parsed `specs` but may need extra synonym rules.
- Layer groups and block patterns for drawings are defaults from the development
  drawings: use a profile for other offices.
- DXF: ASCII only (binary DXF is rare); dynamic blocks show as anonymous `*U..`
  names; geometry inside blocks is not included in per-layer lengths.
- KML areas use a spherical earth (error < 0.5 %) — fine for site screening, not for cadastral work.
- Encrypted PVsyst files from manufacturers cannot be read.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests build small sample files of every format on the fly — no real project
data in the repo. Adding a format = one file in `src/design_ingest/parsers/`
with a function decorated by `@register([".ext"], name=..., version=...)`.
