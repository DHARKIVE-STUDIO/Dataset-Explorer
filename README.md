# Dataset Explorer

Upload a CSV or TSV, then preview and filter rows, check column types and data quality
issues, look at summary statistics, draw basic charts, and get insights about the data.

- **Backend:** FastAPI, pandas, SQLite (metadata), files on disk (the uploads).
- **Frontend:** plain HTML/CSS/JS, Chart.js from a CDN. No build step.
- **AI feature:** a short write-up of the data, notable patterns, and suggested charts.
  Written by Claude when `ANTHROPIC_API_KEY` is set. Otherwise a rule-based version is shown.
  Every result is labeled **AI-generated** or **Rule-based, no AI**.

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

cd backend
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000. Try `sample_data/store_sales.csv`.

Run the tests from the project root:

```bash
pip install pytest
pytest -q tests
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `EXPLORER_DATA_DIR` | `./data` | Where the SQLite DB and uploaded files live. Put this on a persistent volume. |
| `EXPLORER_MAX_UPLOAD_MB` | `50` | Upload size limit. Larger files get a 413. |
| `EXPLORER_MAX_COLUMNS` | `500` | Column limit per file. |
| `EXPLORER_CACHE_SIZE` | `8` | How many parsed datasets stay in memory. |
| `ANTHROPIC_API_KEY` | unset | Enables the AI insights. Without it, rule-based insights are used. |
| `EXPLORER_AI_MODEL` | `claude-haiku-5-5` | Model used for insights. |
| `EXPLORER_AI_TIMEOUT` | `30` | Seconds to wait for the model before falling back. |

## What the AI feature sends

Only a statistical profile goes to the model: column names and types, missing and unique
counts, numeric min/max/mean/median, outlier counts, quality issue codes, and correlation
values. **Raw rows and categorical values are not sent.** Column names can still reveal
things about your data, so think about that before you turn it on for sensitive files.

AI results are cached per dataset in SQLite. Use **Regenerate** to get a fresh run.
Fallbacks are not cached, so the next request retries the model.

## Deployment

### Docker (any host)

```bash
docker build -t dataset-explorer .
docker run -d -p 8000:8000 \
  -v explorer-data:/app/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  --name explorer dataset-explorer
```

The `-v explorer-data:/app/data` volume keeps datasets across container restarts and
upgrades. Without it, uploads are lost when the container is removed.

### A VM or PaaS with a persistent disk

```bash
cd backend
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

Put it behind a reverse proxy (Caddy or nginx) for TLS. Keep `--workers 1`. The parsed-data
cache is per process, and SQLite is fine for this load but not for many writers.
Set `EXPLORER_DATA_DIR` to a disk that survives redeploys.

### Security before you expose it

There is **no authentication**. Anyone who can reach the URL can upload, read, and delete
datasets. Before it's public, put it behind your proxy's basic auth, an SSO gateway, or
a VPN. Don't deploy it open to the internet with sensitive data in it.

## API

All errors come back as `{"error": {"code", "message", "details"}}`. The frontend shows
`message`. Use `code` if you're scripting.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/datasets` | multipart `file`. Returns 201 with metadata. |
| GET | `/api/datasets` | List. |
| GET | `/api/datasets/{id}` | Metadata. |
| DELETE | `/api/datasets/{id}` | Removes the row, the file, and cached insights. |
| GET | `/api/datasets/{id}/profile` | Column types, quality issues, correlations. |
| GET | `/api/datasets/{id}/rows` | `offset`, `limit` (max 500), `filters` (JSON array), `sort_by`, `sort_dir`. |
| GET | `/api/datasets/{id}/stats` | Optional `columns=a,b`. |
| GET | `/api/datasets/{id}/chart` | `kind` (bar, histogram, line, scatter), `x`, `y`, `agg`, `bins`. |
| GET | `/api/datasets/{id}/insights` | `refresh=true` to regenerate. |

Filter operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `is_null`, `not_null`.
Example: `filters=[{"column":"region","op":"eq","value":"North"}]`

## Known limits

- Each dataset is read fully into memory, so files near the upload limit need a few hundred MB of RAM.
- Column types are guessed from a sample of up to 5,000 values. A column that's 94% numeric is typed as text and flagged as mixed.
- Duplicate-row detection and correlations are computed on the whole file, which is slow on very large inputs.
- The AI write-up can be wrong. Check the numbers against the Statistics tab before you act on them.

## Chart assistant

In the Charts tab, type a question like "compare units and unit_price by region". The assistant
picks a chart, draws it, and explains the choice.

- With `ANTHROPIC_API_KEY` set, Claude reads the dataset profile and your question. Answers are
  labeled AI-generated.
- Without a key, or if the AI call fails, a keyword-based guide answers instead, labeled Rule-based.
- Every suggested column and chart type is checked against your data before it's drawn.

Several Y columns can go on one chart. If their sizes differ a lot, the Scale control picks one of:
shared axis (similar sizes), two axes (exactly two metrics), or indexed (line charts only, each
series starts at 100 so you compare growth, not raw values). Auto picks for you and says what it did.
