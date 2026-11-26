"""Scaffold tools: Flask, FastAPI, Node Express, static site, Dockerfile, docker-compose.yml,
GitHub Actions CI, Terraform main.tf and README.md. Existing files are never overwritten unless asked."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from tools._core import STATE, audit, resolve_path, result
from tools.registry import tool

_FLASK = {
    "app.py": '''"""Minimal Flask app."""
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/")
def index():
    return jsonify(message="Hello from {name}")


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
''',
    "requirements.txt": "flask>=3.0\npytest>=8.0\n",
    "test_app.py": '''from app import app


def test_health():
    r = app.test_client().get("/health")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}
''',
}
_FASTAPI = {
    "main.py": '''"""Minimal FastAPI app. Run: uvicorn main:app --reload"""
from fastapi import FastAPI

app = FastAPI(title="{name}")


@app.get("/")
def index() -> dict:
    return {"message": "Hello from {name}"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
''',
    "requirements.txt": "fastapi>=0.110\nuvicorn[standard]>=0.29\nhttpx>=0.27\npytest>=8.0\n",
    "test_main.py": '''from fastapi.testclient import TestClient

from main import app


def test_health():
    r = TestClient(app).get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
''',
}
_EXPRESS = {
    "package.json": json.dumps({"name": "{name}", "version": "1.0.0", "main": "index.js",
                                "scripts": {"start": "node index.js", "test": "node --test"},
                                "dependencies": {"express": "^4.19.2"}}, indent=2) + "\n",
    "index.js": '''const express = require("express");
const app = express();

app.get("/", (_req, res) => res.json({ message: "Hello from {name}" }));
app.get("/health", (_req, res) => res.json({ status: "ok" }));

module.exports = app;

if (require.main === module) {
  const port = process.env.PORT || 3000;
  app.listen(port, () => console.log(`listening on ${port}`));
}
''',
    "app.test.js": '''const test = require("node:test");
const assert = require("node:assert");
const app = require("./index");

test("GET /health", async () => {
  const server = app.listen(0);
  const { port } = server.address();
  const res = await fetch(`http://127.0.0.1:${port}/health`);
  assert.strictEqual(res.status, 200);
  assert.deepStrictEqual(await res.json(), { status: "ok" });
  server.close();
});
''',
}
_STATIC = {
    "index.html": '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main><h1>{name}</h1><p>Your static site is live.</p></main>
</body>
</html>
''',
    "style.css": "body{font-family:system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh}\nmain{max-width:40rem;padding:2rem}\n",
}
_APPS = {"flask": _FLASK, "fastapi": _FASTAPI, "express": _EXPRESS, "static": _STATIC}


def _detect(d: Path) -> str:
    if (d / "package.json").exists():
        return "node"
    if (d / "go.mod").exists():
        return "go"
    return "python"


def _dockerfile(d: Path) -> str:
    kind = _detect(d)
    if kind == "node":
        return ("FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --omit=dev || npm install --omit=dev\n"
                "COPY . .\nEXPOSE 3000\nHEALTHCHECK CMD wget -qO- http://localhost:3000/health || exit 1\nCMD [\"npm\", \"start\"]\n")
    if kind == "go":
        return ("FROM golang:1.22 AS build\nWORKDIR /src\nCOPY . .\nRUN CGO_ENABLED=0 go build -o /app ./...\n\n"
                "FROM gcr.io/distroless/static\nCOPY --from=build /app /app\nEXPOSE 8080\nENTRYPOINT [\"/app\"]\n")
    entry = "main:app" if (d / "main.py").exists() else "app:app"
    cmd = ('["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]' if entry == "main:app"
           else '["python", "app.py"]')
    return ("FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt* ./\n"
            "RUN pip install --no-cache-dir -r requirements.txt || true\nCOPY . .\nEXPOSE 8000\n"
            f"HEALTHCHECK CMD python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/health')\" || exit 1\nCMD {cmd}\n")


def _compose(d: Path) -> str:
    port = "3000" if _detect(d) == "node" else ("8080" if _detect(d) == "go" else "8000")
    return (f"services:\n  app:\n    build: .\n    ports:\n      - \"{port}:{port}\"\n    env_file:\n      - .env\n"
            "    restart: unless-stopped\n")


def _ci(d: Path) -> str:
    kind = _detect(d)
    head = "name: CI\non:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
    if kind == "node":
        return head + "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n      - run: npm ci || npm install\n      - run: npm test\n"
    if kind == "go":
        return head + "      - uses: actions/setup-go@v5\n        with:\n          go-version: '1.22'\n      - run: go test ./...\n"
    return head + ("      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
                   "      - run: pip install -r requirements.txt\n      - run: pytest -q\n")


_TERRAFORM = '''terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name"
}

provider "aws" {
  region = var.region
}

resource "aws_s3_bucket" "main" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "main" {
  bucket                  = aws_s3_bucket.main.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "bucket_arn" {
  value = aws_s3_bucket.main.arn
}
'''


def _readme(d: Path, name: str) -> str:
    kind = _detect(d)
    run = {"node": "npm install\nnpm start", "go": "go run ./...", "python": "pip install -r requirements.txt\npython app.py"}[kind]
    return f"# {name}\n\nShort description of {name}.\n\n## Run\n\n```bash\n{run}\n```\n\n## Test\n\n```bash\n{'npm test' if kind == 'node' else ('go test ./...' if kind == 'go' else 'pytest')}\n```\n\n## Deploy\n\n```bash\ndocker compose up -d --build\n```\n"


def _write(files: dict[str, str], base: Path, overwrite: bool, name: str) -> dict[str, Any]:
    created, skipped = [], []
    for rel, content in files.items():
        p = base / rel
        if p.exists() and not overwrite:
            skipped.append(rel)
            continue
        if STATE.dry_run:
            created.append(rel + " (dry-run)")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.replace("{name}", name), encoding="utf-8")
        created.append(rel)
    if created and not STATE.dry_run:
        audit("scaffold", f"write {len(created)} file(s) in {base}", 0)
    out = f"Directory: {base}\nCreated: {', '.join(created) or 'nothing'}"
    if skipped:
        out += f"\nSkipped (already exist; use overwrite=true): {', '.join(skipped)}"
    return result(True, output=out, created=created, skipped=skipped, path=str(base))


@tool("scaffold", "Scaffold a project or config file: flask, fastapi, express, static (new directory) or "
      "dockerfile, compose, ci (.github/workflows/ci.yml), terraform (main.tf), readme (in the target dir).",
      {"kind": ("string", "flask|fastapi|express|static|dockerfile|compose|ci|terraform|readme",
                ["flask", "fastapi", "express", "static", "dockerfile", "compose", "ci", "terraform", "readme"]),
       "name": ("string", "Project name (for app kinds the directory to create)"),
       "path": ("string", "Target directory (for dockerfile/compose/ci/terraform/readme; default cwd)"),
       "overwrite": ("boolean", "Overwrite existing files")}, required=["kind"], category="scaffold")
def scaffold(kind: str, name: str = "my-app", path: Optional[str] = None, overwrite: bool = False) -> dict[str, Any]:
    """Generate files from built-in templates."""
    if kind in _APPS:
        return _write(_APPS[kind], resolve_path(path) / name if path else resolve_path(name), overwrite, name)
    base = resolve_path(path)
    if kind == "dockerfile":
        return _write({"Dockerfile": _dockerfile(base), ".dockerignore": ".git\nnode_modules\n__pycache__\n.env\nlogs\n"}, base, overwrite, name)
    if kind == "compose":
        return _write({"docker-compose.yml": _compose(base)}, base, overwrite, name)
    if kind == "ci":
        return _write({".github/workflows/ci.yml": _ci(base)}, base, overwrite, name)
    if kind == "terraform":
        return _write({"main.tf": _TERRAFORM}, base, overwrite, name)
    if kind == "readme":
        return _write({"README.md": _readme(base, name if name != "my-app" else base.name)}, base, overwrite, name)
    return result(False, error=f"Unknown scaffold kind '{kind}'", exit_code=2)
