# Contributing

Thanks for your interest. A few rules keep this repository safe to use.

## Do NOT commit

- Real API keys, `.env`, tokens, cookies or Chrome login profiles
- Candidate resumes, PDFs, chat logs, screenshots of real accounts
- SQLite databases, search snapshots, or anything under `data/`
- Customer names, quotes, pricing or other commercial documents

All of the above are covered by `.gitignore`, but please double-check `git status`
before committing. If you accidentally push sensitive data, open a private
security advisory immediately rather than a public issue.

## Development

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-real.txt
python -m unittest discover -s tests -p "test_*.py"
python -m scripts.smoke_test
```

Tests must not require a real Liepin account or send any message. Use fake pages
or offline HTML snapshots (see `tests/` for the pattern).

## Pull requests

1. Keep changes focused; avoid unrelated refactors.
2. Add or update tests for behaviour changes and report the pass/fail count.
3. Chinese or English descriptions are both fine.
4. Browser selectors change with the platform — describe how you verified them
   (read-only DOM probe is preferred over real sends).
