# Zurich finance RSS aggregator

Reads Swiss newspaper RSS feeds, keeps only articles about Zurich-based
financial-services companies and the Zurich financial centre, and republishes
the matches as a single RSS 2.0 feed (`feed.xml`). A scheduled GitHub Actions
workflow regenerates and commits that file hourly.

Only publishers' feed metadata is used: titles, summaries, links and dates.
Article bodies and paywalled content are never fetched.

## Local usage

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen        # install the locked dependencies
uv run pytest           # offline behavior tests, no network access
uv run python aggregator.py
```

The last command fetches every configured source and writes `feed.xml` next to
`feeds.toml`. It prints one line per source with fetched and matched entry
counts, then `Wrote N articles to .../feed.xml`.

Options:

- `--config PATH` — configuration file, default `feeds.toml`.
- `--feed-link URL` — public URL of the generated feed. When absent,
  `FEED_LINK` is used, then `feeds.toml`'s `link`.

## Sources and filtering

Configured sources:

| Source | Feed |
| --- | --- |
| NZZ Wirtschaft | `https://www.nzz.ch/wirtschaft.rss` |
| NZZ Finanzen | `https://www.nzz.ch/finanzen.rss` |
| NZZ Zürich | `https://www.nzz.ch/zuerich.rss` |
| Tages-Anzeiger Wirtschaft | `https://www.tagesanzeiger.ch/wirtschaft/rss.html` |
| 20 Minuten Wirtschaft | `https://partner-feeds.20min.ch/rss/20minuten/wirtschaft` |

Behavior:

- An article matches when **any** pattern in `feeds.toml` matches its title or
  summary (OR semantics, case-insensitive).
- Links are canonicalized (lowercased scheme/host, fragment and `utm_*`
  parameters removed, trailing slash trimmed) and deduplicated; the canonical
  URL is the output `<guid>`.
- The feed is a **current snapshot**: it is rebuilt from the current source
  feeds every run. Entries that disappear upstream disappear here too; the
  previous `feed.xml` is never merged.
- At most `max_items` (default 100) articles are kept, newest first; undated
  articles come last.
- Either **all** sources succeed or nothing is written. A fetch or parse
  failure aborts the run with an error naming the source, and the previous
  `feed.xml` is left untouched.

## Editing the configuration

`feeds.toml` is the only routine configuration. Add a publisher:

```toml
[[sources]]
name = "Publisher Wirtschaft"
url = "https://example.ch/wirtschaft.rss"
```

Add a company or topic with a TOML literal string, so backslashes are not
escaped twice:

```toml
patterns = [
  '\bUBS\b',
  '\bMeine[ -]Bank\b',
]
```

`patterns` must stay **above** the `[[sources]]` tables; otherwise TOML treats
it as a key of the last source.

The program validates everything before fetching: empty source list, empty
pattern list, non-positive `max_items`, blank names/URLs and invalid regexes all
raise an error naming the offending setting. Configuration errors and any
failed source leave the previous `feed.xml` in place.

## GitHub setup

1. Push this project to a repository (the workflow expects the default branch
   to allow direct pushes by the Actions token).
2. Enable **Actions** for the repository.
3. Under **Settings → Actions → General → Workflow permissions**, allow read
   and write access to repository contents.
4. Run **Update RSS feed** once manually (`workflow_dispatch`). It runs the
   tests, regenerates `feed.xml` with the public link, and commits the result
   as `Update filtered RSS feed`; with no changes it logs `No feed changes`.

The workflow then runs hourly (`cron: "17 * * * *"`). Scheduled workflows run
from the default branch, and GitHub does not guarantee exact cron timing. A
push rejected because new commits arrived fails that run; the next scheduled
run checks out the branch again.

Subscribe to:

```text
https://raw.githubusercontent.com/OWNER/REPO/BRANCH/feed.xml
```

Normally `BRANCH` is `main`. The raw URL follows the branch the workflow ran
on. Unauthenticated feed readers need a **public** repository; for a private
repository, readers must support GitHub-authenticated raw URLs.
