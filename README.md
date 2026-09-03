# NOVA

A permission-controlled AI agent operating layer.

NOVA is a personal orchestration system that plans tasks, checks them against
a deterministic permission engine, executes them through a fixed set of tools,
verifies the result, and records everything. The AI chooses *which* tool to
call; it never gets a shell.

## Status

Milestone 1 (foundation) is complete. There is deliberately **no AI, voice,
browser automation, device networking or tool execution yet** - those arrive in
later milestones. What exists today:

| Component | State |
|---|---|
| Configuration (`pydantic-settings`, `.env`) | Done |
| Structured JSON logging with rotation and secret redaction | Done |
| Async SQLite engine (WAL, foreign keys, busy timeout) | Done |
| ORM models: `preferences`, `devices`, `audit_events` | Done |
| Repositories (caller owns the transaction) | Done |
| Alembic migrations | Done |
| FastAPI app, `/health`, `/version` | Done |
| Authenticated WebSocket echo at `/ws` | Done |

## Requirements

- Windows 11 (the first target platform)
- Python 3.12
- Git

## Setup from a clean machine

All commands are PowerShell, run from the repository root.

### 1. Allow scripts to run for your user

Virtual environment activation is a PowerShell script, and the Windows default
policy blocks it.

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`RemoteSigned` still requires a signature on anything downloaded from the
internet. It applies to your account only.

### 2. Install Python 3.12 and Git

```powershell
winget install --id Python.Python.3.12 --scope user
winget install --id Git.Git --scope user
```

Close and reopen PowerShell afterwards, then confirm:

```powershell
py -3.12 --version
git --version
```

### 3. Create the virtual environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. Verify the interpreter is the one
inside the project:

```powershell
where.exe python
```

The first path must be under `.venv\Scripts\`.

### 4. Install the project

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
```

### 5. Configure

```powershell
Copy-Item .env.example .env
```

`.env.example` ships with an empty `NOVA_SECURITY__AUTH_TOKEN`, so copying it
unchanged will fail at startup rather than give you a working install with a
publicly known credential. Generate a real token:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the result into `.env`:

```text
NOVA_SECURITY__AUTH_TOKEN=<the generated value>
```

`.env` is git-ignored and must never be committed.

### 6. Create the database

```powershell
alembic upgrade head
```

This creates `%LOCALAPPDATA%\NOVA\nova.db` with the three tables. Migrations
read the database path from your configuration, so `.env` must exist first.

### 7. Run it

```powershell
python -m nova.main
```

NOVA binds `127.0.0.1:8765` only. From a second window:

```powershell
curl.exe http://127.0.0.1:8765/health
```

Interactive docs are at [http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs) in development. They are
disabled when `NOVA_ENVIRONMENT=production`.

## Quality gates

All four must pass before a commit:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

To run them automatically on every commit:

```powershell
pre-commit install
```

The hooks call the tools from your virtual environment rather than pinned
copies, so a commit and a manual run always agree.

## Layout

```text
core/nova/
  config/       validated settings from environment and .env
  utils/        structured logging
  storage/      engine, models, repositories, Alembic migrations
  api/          FastAPI app, bearer auth, WebSocket
  main.py       console entry point (nova = "nova.main:run")
core/tests/     unit and integration tests
alembic.ini     migration configuration (contains no database URL)
```

## Conventions

- **Python source is ASCII-only.** Multilingual content - Hindi, Hinglish, Devanagari test corpora - belongs in YAML or JSON data files. A single non-ASCII character in a `.py` file has crashed Ruff's diagnostic renderer.
- **Repositories never commit.** The caller owns the transaction boundary.
- **The audit log is append-only.** No update or delete method exists for it.
- **Secrets never reach the logs.** A redaction processor strips any field whose name suggests a credential, before any renderer sees it.
- **Localhost only.** Binding a non-loopback address requires setting `NOVA_SERVER__ALLOW_NON_LOCAL=true` explicitly.

## Security

NOVA does not bypass operating system security, does not install software
silently, does not harvest credentials, and does not give the language model
unrestricted shell access. Every capability is an explicit, audited tool.