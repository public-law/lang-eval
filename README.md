# lang-eval

Scores less mainstream programming languages on development activity, community, and editor support, using live public APIs.

Page: https://public-law.github.io/lang-eval/

## Use

Requires [uv](https://docs.astral.sh/uv/). It installs the pinned Python and runs the script:

```sh
GITHUB_TOKEN=$(gh auth token) ./lang_eval.py
```

Output goes to `docs/index.html` and `docs/data.json`. A GitHub Action reruns it weekly and deploys to Pages.

Checks, as CI runs them:

```sh
uvx --exclude-newer 2026-11-01T00:00:00Z --python 3.12.15 mypy==2.4.1 --strict lang_eval.py test_config.py
uv run --python 3.12.15 --no-project python -m unittest
./lang_eval.py --check-config
```

`languages.toml` is validated on every run. Unknown keys, missing or mistyped fields, bad URLs, ratings outside 1 to 5, and duplicate names are all reported at once with their locations.

## Add a language

Copy a `[[language]]` block in `languages.toml`. Fields you leave out show as n/a. Manual judgments (`lsp_rating`, `lsp_notes`, `x_notes`) go there too.

## Notes

- Metrics that fail to fetch show "not measured". If an earlier run succeeded, its value is kept and labeled with its date.
- Reddit blocks many cloud IPs, so the subreddit metric may come from the last local run.
- X activity is not measured (no credentials in CI).
- Scoring thresholds are listed at the bottom of the page.
