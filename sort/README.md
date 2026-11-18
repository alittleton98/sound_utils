# sfx_sort

Weekly sorter for `../sound_library`. It empties the inbox folders (`_Inbox`, `SoundlyAudio`)
into the library's category folders and never touches anything that's already filed.

## Setup (on the Linux server)

```bash
cd /path/to/sound_utils
chmod +x setup.sh
./setup.sh            # checks Python, creates _Inbox/_Review/_Duplicates, shows a dry run, offers to install the weekly job
```

The default schedule is Sunday at 03:30. To change it: `SFX_CRON="15 4 * * 1" ./setup.sh`.
To remove the job: `./setup.sh --uninstall`.

Requires Python 3.11+, or 3.8+ with `tomli` (setup installs it if needed). Nothing else.

## Everyday use

| | |
|---|---|
| Drop new sounds in | `sound_library/_Inbox/` (loose files, or whole pack folders) |
| Preview | `./sfx_sort.py --dry-run` |
| Sort now | `./sfx_sort.py` |
| Undo a run | `./sfx_sort.py --undo logs/moves-YYYYMMDD-HHMMSS.tsv` |
| Couldn't place | `sound_library/_Review/` + list in `logs/review-latest.txt` |
| Exact copies | `sound_library/_Duplicates/` (nothing is ever deleted) |

## How it decides

1. **UCS CatID in the filename**: `AMBForst_…` goes to Ambience, `WHSH_…` to Production Elements
2. **CatID in the file's iXML metadata**: for files with plain names like `ZOOM0042.wav`
3. **Soundly comma style**: in `Explosions, Designed, …`, the first field is the UCS category
4. **Keywords**: first word counts most (`Foley …`, `Crash …`, `Weapon …`), plus the BWF description
5. **Packs** (folders dropped in an inbox) move whole: by their folder name, or by majority vote of their files
6. Peak files (`.pkf`, `.reapeaks`, …) move with their audio file. Files modified in the last 30 minutes are skipped.

Everything is configurable in `sfx_sort.toml`: inbox list, the UCS-category → folder map, keywords,
and an optional `ucs_csv` path to the official UCS spreadsheet for exact CatID matching.

## Using Claude for the leftovers

`logs/review-latest.txt` is a plain list of what landed in `_Review`. Paste it into Claude and ask
where each item belongs (a few thousand tokens). Then add any new keywords to `sfx_sort.toml`
so next week's run handles them automatically.
