# revision-finder

`revision_finder.py` compares two `.sql` backups of a Drupal 10 database and
reports the **node IDs of a given content type that were created, modified, or
deleted within a specified UTC datetime window**.

The script is pure Python (3.10+) standard library — no third-party runtime
dependencies. It does not execute SQL or invoke any shell; both backups are
parsed by a self-contained tokenizer, which makes the tool safe to point at
untrusted input files.

## Usage

```bash
python3 revision_finder.py BACKUP_A.sql BACKUP_B.sql \
    --type article \
    --start 2025-01-01 \
    --end   2025-01-31
```

### Common options

| Flag | Purpose |
| --- | --- |
| `-t / --type MACHINE_NAME` | Drupal content type machine name (`[a-z][a-z0-9_]{0,31}`). |
| `-s / --start ISO`         | Window start (UTC). Accepts `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, etc. |
| `-e / --end ISO`           | Window end (UTC, inclusive). |
| `-l / --limit N`           | Restrict the printed list of node ids to `N`. |
| `--diff NID`               | Show a colorful unified diff for one specific node id. |
| `--diff-all`               | Show colorful diffs for every identified node id. |
| `--no-color`               | Disable ANSI color (auto-disabled when stdout is not a tty). |
| `-h / --help`              | Full argparse help. |

### Validation behavior

Before any work is done the script enforces:

1. Both inputs end in `.sql`, exist, and are non-empty.
2. They are not the same path, and their SHA-256 contents are not identical.
3. They look like Drupal dumps — at least two known Drupal tables are present
   (`node_field_data`, `users_field_data`, `key_value`, `config`, …).
4. They differ by at least one signal: `mysqldump` completion timestamp, file
   mtime, or file content.
5. At least one of them contains a node of the requested content type.
6. The content-type machine name matches `^[a-z][a-z0-9_]{0,31}$` (this rejects
   shell- or SQL-injection-style strings).

If any check fails the script prints an `error: …` line to `stderr` and exits
with status `2`.

## How a node qualifies as “differing”

A node id is reported when **all** of the following hold:

* It carries `type = <machine_name>` in at least one of the two backups.
* Its `changed` timestamp in at least one backup falls within `[start, end]`.
* Its row data in `node_field_data` differs between the two backups (added,
  removed, or modified).

## Tests

The repository ships with a tiny mock test suite using two minimal fixture
backups under `tests/fixtures/`. No real database is needed.

### Native (macOS / Linux)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -v tests/
```

### Containerized (recommended for CI parity)

```bash
./bin/run-tests.sh         # builds the image and runs pytest inside it
# or, equivalently:
docker-compose run --rm tests
```

GitHub Actions (`.github/workflows/ci.yml`) runs both the native matrix
(Python 3.10/3.11/3.12) and the Dockerized job on every push / PR.

## License

See `LICENSE.md`.
