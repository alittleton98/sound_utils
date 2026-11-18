# SFX Sort

Weekly sorter for an SFX library. It empties the inbox folders (`_Inbox`, `SoundlyAudio` by default)
into the library's category folders and never touches anything that's already filed.

It works anywhere: the library can be any folder you point it at, it needs nothing but Python 3.8+
(3.11+, or `tomli` on older versions), and it runs on Linux, macOS and Windows.

## Setup (Linux / macOS)

```bash
./setup.sh --library /path/to/sound_library
```

This saves the library path, checks Python, creates `_Inbox`, `_Review` and `_Duplicates`,
shows a dry run, and offers to install a weekly job (default Sunday 03:30).
If you leave out `--library`, it asks for the path.

| | |
|---|---|
| Different schedule | `SFX_CRON="15 4 * * 1" ./setup.sh` |
| Install without asking | `./setup.sh --yes` |
| Reset config to defaults | `./setup.sh --reset-config` (keeps the library path, backs up the old file) |
| Remove the job | `./setup.sh --uninstall` |

On Windows, skip setup and use Task Scheduler to run `python sfx_sort.py --library D:\sound_library`.

## Choosing the library

The first of these that is set wins:

1. `--library PATH` on the command line
2. the `SFX_LIBRARY` environment variable
3. `library` in `sfx_sort.toml` (save it with `./sfx_sort.py --set-library PATH`)

No config file? The script uses its built-in defaults. Run `./sfx_sort.py --init` to write
them out as `sfx_sort.toml` so you can edit them. You can keep the config elsewhere with
`--config PATH` or `SFX_SORT_CONFIG`.

## Everyday use

| | |
|---|---|
| Drop new sounds in | `<library>/_Inbox/` (loose files, or whole pack folders) |
| Preview | `./sfx_sort.py --dry-run` |
| Sort now | `./sfx_sort.py` |
| Sort another library once | `./sfx_sort.py --library /other/library` |
| Undo a run | `./sfx_sort.py --undo logs/moves-YYYYMMDD-HHMMSS.tsv` |
| Couldn't place | `<library>/_Review/` + list in `logs/review-latest.txt` |
| Exact copies | `<library>/_Duplicates/` (nothing is ever deleted) |

## How it decides

1. **UCS CatID in the filename**: `AMBForst_…` goes to Ambience, `WHSH_…` to Production Elements
2. **CatID in the file's iXML metadata**: for files with plain names like `ZOOM0042.wav`
3. **Soundly comma style**: in `Explosions, Designed, …`, the first field is the UCS category
4. **Keywords**: first word counts most (`Foley …`, `Crash …`, `Weapon …`), plus the BWF description
5. **Packs** (folders dropped in an inbox) move whole: by their folder name, or by majority vote of their files
6. Peak files (`.pkf`, `.reapeaks`, …) move with their audio file. Files modified in the last 30 minutes are skipped.

Everything is configurable in `sfx_sort.toml`: inbox list, the UCS-category → folder map, keywords,
and an optional `ucs_csv` path to the official UCS spreadsheet for exact CatID matching.
Folder names in the map that don't exist in your library are created when first needed.

## Using Claude for the leftovers

`logs/review-latest.txt` is a plain list of what landed in `_Review`. Paste it into Claude and ask
where each item belongs. Then add any new keywords to `sfx_sort.toml` so the next run
handles them automatically.
