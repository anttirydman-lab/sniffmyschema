# SniffMySchema — Schema Audit API

FastAPI + Playwright service that crawls a sitemap, extracts JSON-LD schema from every page, and returns a structured audit report.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --reload --port 8000
```

Test:

```bash
curl -s -X POST http://localhost:8000/audit \
  -H "Content-Type: application/json" \
  -d '{"sitemap_url":"https://example.com/sitemap.xml","business_type":"local_business","max_pages":5}' \
  | python3 -m json.tool
```

## Deploy to Railway

1. Push to GitHub (see below).
2. In Railway, create a new project → "Deploy from GitHub repo" → select `sniffmyschema`.
3. Railway auto-detects the Dockerfile. No config file needed.
4. The service starts on the `$PORT` Railway provides.

## Git setup

```bash
git init
git remote add origin git@github.com:<your-user>/sniffmyschema.git
git add -A
git commit -m "Initial deploy: FastAPI schema audit API"
git push -u origin main
```

## Endpoints

- `GET /health` — `{"status": "ok"}`
- `POST /audit` — accepts `{"sitemap_url", "business_type", "max_pages"}`, returns full audit JSON

## Business types

`ecommerce`, `local_business`, `blog_media`, `professional_services`, `other` (default)
