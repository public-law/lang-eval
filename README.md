# lang-eval

Scores less mainstream programming languages on development activity, community, and editor support, using live public APIs.

Page: https://public-law.github.io/lang-eval/

## Use

Requires [uv](https://docs.astral.sh/uv/). It installs the pinned Python and runs the script:

```sh
GITHUB_TOKEN=$(gh auth token) ./lang_eval.py
```

Output goes to `docs/index.html` and `docs/data.json`. A GitHub Action reruns it weekly and deploys to Pages.

## Add a language

Copy a `[[language]]` block in `languages.toml`. Fields you leave out show as n/a. Manual judgments (`lsp_rating`, `lsp_notes`, `x_notes`) go there too.

## Notes

- Metrics that fail to fetch show "not measured". If an earlier run succeeded, its value is kept and labeled with its date.
- Reddit blocks many cloud IPs, so the subreddit metric may come from the last local run.
- X activity is not measured (no credentials in CI).
- Scoring thresholds are listed at the bottom of the page.
