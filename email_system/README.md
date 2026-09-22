# Institutional Email AI (human-in-the-loop)

This system reads, classifies and extracts details from institutional emails, then drafts replies.
**It never sends an email without explicit admin approval.**

Status: **Phase 1 of 13 (setup + config)**. See `docs/DESIGN_REVIEW.md` for the review of error and spam handling.

## Install (clean Mac, Apple Silicon, zsh)

```zsh
# 1. Tools
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # if no Homebrew
brew install python@3.12 tesseract ollama

# 2. Project
cd email_system
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

# 3. LLM (needed from Phase 4)
brew services start ollama
ollama pull qwen3:4b

# 4. Run
pytest
uvicorn app.main:app --reload     # then open http://127.0.0.1:8000/health
```

## Layout (Phase 1)

```
email_system/
├── app/
│   ├── main.py              FastAPI app + /health
│   └── core/
│       ├── config.py        Settings (.env), safety validators, fixed Gmail scopes
│       ├── categories.py    Loads/validates config/categories.yaml
│       ├── errors.py        ErrorCode -> (ERROR|FAILED|NONE, retryable, message)
│       └── logging.py       Event logging with secret/body redaction
├── config/categories.yaml   Categories + spam signals (add categories here, no code)
├── docs/DESIGN_REVIEW.md
├── tests/
├── .env.example
├── requirements.txt / requirements-dev.txt
└── pyproject.toml
```

## Adding a category

Add an entry to `config/categories.yaml` and restart. The file is validated at startup,
and the app will not start if it is invalid.
