#!/usr/bin/env python3
"""
sfx_sort.py - weekly sorter for an SFX library.

Empties the configured inbox folders into the library's category folders:
  * UCS files      -> by CatID prefix in the filename (e.g. AMBForst_..., WHSH_...)
  * BWF metadata   -> by CatID in the file's iXML chunk, if the name has none
  * Soundly style  -> "Explosions, Designed, ..." (first field is the UCS category)
  * everything else-> keyword scoring on the filename + BWF description
  * folders (packs) dropped into an inbox are moved whole, never split up
  * byte-identical copies go to _Duplicates, peak files travel with their audio
  * anything uncertain goes to _Review and is listed in logs/review-latest.txt

Already-organized parts of the library are never touched.
No third-party packages needed (Python 3.11+, or 3.8+ with `tomli`).

Usage:
  sfx_sort.py                 sort now
  sfx_sort.py --dry-run       show what would happen, move nothing
  sfx_sort.py --undo FILE     reverse a previous run using its journal (logs/moves-*.tsv)
  sfx_sort.py --config PATH   use another config file
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import os
import re
import shutil
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        sys.exit("sfx_sort: needs Python 3.11+, or `pip install tomli` on older Pythons (setup.sh handles this).")

HERE = Path(__file__).resolve().parent

# Built-in UCS category prefixes -> full category names. Used when no ucs_csv is configured.
UCS_PREFIXES = {
    "AIR": "AIR", "AERO": "AIRCRAFT", "ALRM": "ALARMS", "AMB": "AMBIENCE", "ANML": "ANIMALS",
    "ARCHV": "ARCHIVED", "BEEP": "BEEPS", "BELL": "BELLS", "BIRD": "BIRDS", "BOAT": "BOATS",
    "BLLT": "BULLETS", "TOON": "CARTOON", "CERM": "CERAMICS", "CHAIN": "CHAINS", "CHEM": "CHEMICALS",
    "CLOCK": "CLOCKS", "CLOTH": "CLOTH", "COMM": "COMMUNICATIONS", "COMP": "COMPUTERS",
    "CRSH": "CRASHES", "CREA": "CREATURES", "CROWD": "CROWDS", "DSGN": "DESIGNED",
    "DEST": "DESTRUCTION", "DIRT": "DIRT & SAND", "DOOR": "DOORS", "DRWR": "DRAWERS",
    "ELEC": "ELECTRICITY", "EQUIP": "EQUIPMENT", "EXPL": "EXPLOSIONS", "FART": "FARTS",
    "FGHT": "FIGHT", "FIRE": "FIRE", "FIREWRK": "FIREWORKS", "FOLY": "FOLEY", "FOOD": "FOOD & DRINK",
    "FEET": "FOOTSTEPS", "FURN": "FURNITURE", "GAME": "GAMES", "GEO": "GEOTHERMAL", "GLAS": "GLASS",
    "GORE": "GORE", "GUN": "GUNS", "HORN": "HORNS", "HUMN": "HUMAN", "ICE": "ICE", "LASR": "LASERS",
    "LETHR": "LEATHER", "LIQ": "LIQUID & MUD", "MACH": "MACHINES", "MAG": "MAGIC",
    "MECH": "MECHANICAL", "METL": "METAL", "MOTR": "MOTORS", "MOVE": "MOVEMENT", "MUSC": "MUSICAL",
    "NATDIS": "NATURAL DISASTER", "OBJ": "OBJECTS", "PAPR": "PAPER", "PLAS": "PLASTIC",
    "RAIN": "RAIN", "ROBO": "ROBOTS", "ROCK": "ROCKS", "ROPE": "ROPE", "RUBR": "RUBBER",
    "SCI": "SCIFI", "SNOW": "SNOW", "SPRT": "SPORTS", "SWSH": "SWOOSHES", "SWCH": "SWITCHES",
    "TOOL": "TOOLS", "TOY": "TOYS", "TRN": "TRAINS", "UI": "USER INTERFACE", "VEG": "VEGETATION",
    "VEH": "VEHICLES", "VOX": "VOICES", "WATR": "WATER", "WEAP": "WEAPONS", "WTHR": "WEATHER",
    "WHSH": "WHOOSHES", "WIND": "WIND", "WNDW": "WINDOWS", "WING": "WINGS", "WOOD": "WOOD",
}

CATID_RE = re.compile(r"^([A-Z][A-Za-z0-9]*?)_")
DUP_SUFFIX_RE = re.compile(r" \d+$")


# --------------------------------------------------------------------------- config
class Config:
    def __init__(self, path: Path):
        with open(path, "rb") as f:
            c = tomllib.load(f)
        base = path.resolve().parent
        rel = lambda p: (base / p).resolve() if not os.path.isabs(p) else Path(p)
        self.library = rel(c.get("library", "../sound_library"))
        self.inboxes = list(c.get("inboxes", ["_Inbox"]))
        self.review_dir = c.get("review_dir", "_Review")
        self.duplicates_dir = c.get("duplicates_dir", "_Duplicates")
        self.log_dir = rel(c.get("log_dir", "logs"))
        self.dest_subfolder = c.get("dest_subfolder", "")
        self.min_age = float(c.get("min_age_minutes", 30)) * 60
        self.dedupe = bool(c.get("dedupe", True))
        self.dedupe_library = bool(c.get("dedupe_library", True))
        self.pack_min = float(c.get("pack_min_agreement", 0.6))
        self.orphan_sidecars = c.get("orphan_sidecars", "leave")
        self.audio_ext = {e.lower() for e in c.get("audio_ext", [".wav"])}
        self.sidecar_ext = {e.lower() for e in c.get("sidecar_ext", [".pkf", ".reapeaks"])}
        self.ignore = {n.lower() for n in c.get("ignore_names", [])}
        self.cat_to_folder = {k.upper(): v for k, v in c.get("ucs_category_to_folder", {}).items()}
        self.catid_overrides = dict(c.get("catid_overrides", {}))
        self.keywords = {folder: [w.lower() for w in words] for folder, words in c.get("keywords", {}).items()}

        # UCS lookup tables
        self.ucs_exact: dict[str, tuple[str, str]] = {}  # CatID -> (Category, SubCategory)
        ucs_csv = c.get("ucs_csv", "")
        if ucs_csv:
            self._load_ucs_csv(rel(ucs_csv))
        self.ucs_prefixes = UCS_PREFIXES
        self.ucs_fullnames = set(UCS_PREFIXES.values()) | {c for c, _ in self.ucs_exact.values()}

        # keyword regexes, longest phrases first
        self.kw_res = {
            folder: [re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])") for w in sorted(words, key=len, reverse=True)]
            for folder, words in self.keywords.items()
        }

    def _load_ucs_csv(self, path: Path):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip().lower().replace(" ", ""): (v or "").strip() for k, v in row.items()}
                cid = row.get("catid")
                if cid:
                    self.ucs_exact[cid] = (row.get("category", "").upper(), row.get("subcategory", ""))


# --------------------------------------------------------------------------- metadata
def read_bwf(path: Path) -> tuple[str, str]:
    """Return (bext description, iXML CatID) from a WAV/BWF/RF64 file, or ('', '')."""
    desc, catid = "", ""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64", b"BW64") or head[8:12] != b"WAVE":
                return desc, catid
            for _ in range(64):  # chunk limit
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                if cid == b"bext":
                    desc = f.read(min(size, 256)).split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
                    f.seek(size - min(size, 256) + (size & 1), 1)
                elif cid == b"iXML" and size < 4_000_000:
                    xml = f.read(size).decode("utf-8", "replace")
                    m = re.search(r"<(?:CATID|catId|CatID)>\s*([^<\s]+)\s*<", xml)
                    if m:
                        catid = m.group(1)
                    if size & 1:
                        f.seek(1, 1)
                elif size == 0xFFFFFFFF:
                    break  # RF64 placeholder size; give up on later chunks
                else:
                    f.seek(size + (size & 1), 1)
    except OSError:
        pass
    return desc, catid


# --------------------------------------------------------------------------- classification
class Decision:
    __slots__ = ("folder", "reason", "ucs_cat", "ucs_sub")

    def __init__(self, folder=None, reason="", ucs_cat="", ucs_sub=""):
        self.folder, self.reason, self.ucs_cat, self.ucs_sub = folder, reason, ucs_cat, ucs_sub


def resolve_catid(cfg: Config, catid: str) -> tuple[str, str] | None:
    """CatID -> (Category, SubCategory) or None if it isn't a plausible UCS CatID."""
    if cfg.ucs_exact:
        return cfg.ucs_exact.get(catid)
    # Longest built-in prefix whose remainder is empty or a Capitalized subcategory
    for n in range(min(len(catid), 7), 1, -1):
        pre, rest = catid[:n], catid[n:]
        if pre in cfg.ucs_prefixes and (rest == "" or rest[0].isupper()):
            return cfg.ucs_prefixes[pre], rest
    return None


def folder_for_ucs(cfg: Config, catid: str, cat: str) -> str | None:
    return cfg.catid_overrides.get(catid) or cfg.cat_to_folder.get(cat.upper())


def classify_file(cfg: Config, path: Path) -> Decision:
    stem = path.stem
    # 1. UCS CatID in filename
    m = CATID_RE.match(stem)
    if m:
        catid = m.group(1)
        hit = resolve_catid(cfg, catid)
        if hit:
            folder = folder_for_ucs(cfg, catid, hit[0])
            if folder:
                return Decision(folder, f"UCS {catid}", hit[0], hit[1])
    # 2. iXML CatID / bext description
    desc, xml_catid = ("", "")
    if path.suffix.lower() in (".wav", ".bwf", ".w64"):
        desc, xml_catid = read_bwf(path)
    if xml_catid:
        hit = resolve_catid(cfg, xml_catid)
        if hit and (folder := folder_for_ucs(cfg, xml_catid, hit[0])):
            return Decision(folder, f"iXML {xml_catid}", hit[0], hit[1])
    # 3. Soundly comma style: "Explosions, Designed, ..."
    if "," in stem:
        first = stem.split(",", 1)[0].strip().upper()
        for cand in (first, first.rstrip("S"), first + "S"):
            if cand in cfg.ucs_fullnames and (folder := cfg.cat_to_folder.get(cand)):
                sub = stem.split(",")[1].strip()
                return Decision(folder, f"UCS name '{cand.title()}'", cand, sub)
    # 4. keywords
    scores = keyword_scores(cfg, stem, desc)
    if scores:
        if top := pick(scores, 2):
            return Decision(top, "keywords " + ", ".join(f"{f}={s}" for f, s in scores.most_common(2)))
        return Decision(None, "ambiguous: " + ", ".join(f"{f}={s}" for f, s in scores.most_common(3)))
    return Decision(None, "no match")


def keyword_scores(cfg: Config, name: str, extra: str = "") -> Counter:
    """Score folders by keyword hits. First word = 3, second word = 2 (packs often prefix a
    code like 'NEON' or 'SFDS'), first word of the BWF description = 2, anywhere else = 1."""
    text = re.sub(r"_+", " ", name).lower()
    words = [w for w in re.split(r"[\s,]+", text) if w]
    lead1 = " ".join(words[:3])
    lead2 = " ".join(words[1:4])
    extra = extra.lower().strip()
    scores: Counter = Counter()
    for folder, res in cfg.kw_res.items():
        best = 0
        for rx in res:
            if rx.match(lead1):
                best = 3
                break
            if lead2 and rx.match(lead2):
                best = max(best, 2)
            elif extra and rx.match(extra):
                best = max(best, 2)
            elif rx.search(text) or (extra and rx.search(extra)):
                best = max(best, 1)
        if best:
            scores[folder] = best
    return scores


def pick(scores: Counter, minimum: int) -> str | None:
    if not scores:
        return None
    ranked = scores.most_common()
    top, s = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0
    return top if s >= minimum and s - runner >= 1 else None


# --------------------------------------------------------------------------- helpers
def sha1(path: Path, _cache: dict = {}) -> str:
    key = str(path)
    if key not in _cache:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _cache[key] = h.hexdigest()
    return _cache[key]


def find_dir_ci(parent: Path, name: str) -> Path:
    """Case-insensitive lookup of an existing folder; falls back to the given name."""
    p = parent / name
    if p.exists():
        return p
    try:
        for e in parent.iterdir():
            if e.is_dir() and e.name.lower() == name.lower():
                return e
    except FileNotFoundError:
        pass
    return p


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}") if p.suffix and not p.is_dir() else p.with_name(f"{p.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def sidecars_for(audio: Path, siblings: dict[str, Path], cfg: Config) -> list[Path]:
    out = []
    for ext in cfg.sidecar_ext:
        for cand in (audio.stem + ext, audio.name + ext):
            p = siblings.get(cand.lower())
            if p:
                out.append(p)
    return out


def preference(p: Path) -> tuple:
    """Lower is better: keep the UCS-named, un-suffixed, shortest copy."""
    return (0 if CATID_RE.match(p.stem) else 1, 1 if DUP_SUFFIX_RE.search(p.stem) else 0, len(p.name), p.name)


# --------------------------------------------------------------------------- the run
class Sorter:
    def __init__(self, cfg: Config, dry: bool):
        self.cfg, self.dry = cfg, dry
        self.lib = cfg.library
        self.moves: list[tuple[Path, Path, str]] = []
        self.review: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self.now = time.time()
        self.lib_sizes: dict[int, list[Path]] | None = None

    # ---- library size index for dedupe against existing files
    def library_index(self) -> dict[int, list[Path]]:
        if self.lib_sizes is None:
            self.lib_sizes = defaultdict(list)
            skip = {i.lower() for i in self.cfg.inboxes} | {self.cfg.review_dir.lower(), self.cfg.duplicates_dir.lower()}
            for top in self.lib.iterdir():
                if not top.is_dir() or top.name.lower() in skip:
                    continue
                for root, dirs, files in os.walk(top):
                    dirs[:] = [d for d in dirs if d.lower() not in self.cfg.ignore]
                    for fn in files:
                        if os.path.splitext(fn)[1].lower() in self.cfg.audio_ext:
                            p = Path(root, fn)
                            try:
                                self.lib_sizes[p.stat().st_size].append(p)
                            except OSError:
                                pass
        return self.lib_sizes

    def existing_copy(self, f: Path) -> Path | None:
        size = f.stat().st_size
        for other in self.library_index().get(size, []):
            if other != f and sha1(other) == sha1(f):
                return other
        return None

    # ---- moving
    def move(self, src: Path, dst: Path, reason: str):
        dst = unique_path(dst)
        self.moves.append((src, dst, reason))
        if self.dry:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst

    def dest_dir(self, folder: str, d: Decision, inbox: str) -> Path:
        base = find_dir_ci(self.lib, folder)
        if self.cfg.dest_subfolder:
            sub = self.cfg.dest_subfolder.format(
                ucs_category=(d.ucs_cat or "Other").title(), ucs_subcategory=d.ucs_sub or "Misc", inbox=inbox)
            base = base / sub
        return base

    def recent(self, p: Path) -> bool:
        try:
            return self.now - p.stat().st_mtime < self.cfg.min_age
        except OSError:
            return True

    # ---- loose files in an inbox
    def sort_files(self, inbox: Path, files: list[Path]):
        cfg = self.cfg
        siblings = {f.name.lower(): f for f in files}
        audio = [f for f in files if f.suffix.lower() in cfg.audio_ext]
        claimed: set[Path] = set()
        groups = {a: sidecars_for(a, siblings, cfg) for a in audio}
        for s in groups.values():
            claimed.update(s)

        dup_of: dict[Path, Path] = {}
        if cfg.dedupe:
            by_size = defaultdict(list)
            for a in audio:
                by_size[a.stat().st_size].append(a)
            for same in by_size.values():
                if len(same) < 2:
                    continue
                by_hash = defaultdict(list)
                for a in same:
                    by_hash[sha1(a)].append(a)
                for copies in by_hash.values():
                    copies.sort(key=preference)
                    for extra in copies[1:]:
                        dup_of[extra] = copies[0]

        for a in sorted(audio):
            if self.recent(a):
                self.skipped.append((str(a.relative_to(self.lib)), "modified recently"))
                continue
            group = [a] + groups[a]
            if a in dup_of:
                dest = self.lib / cfg.duplicates_dir / inbox.name
                for f in group:
                    self.move(f, dest / f.name, f"duplicate of {dup_of[a].name}")
                continue
            if cfg.dedupe and cfg.dedupe_library and (orig := self.existing_copy(a)):
                dest = self.lib / cfg.duplicates_dir / inbox.name
                for f in group:
                    self.move(f, dest / f.name, f"already in library: {orig.relative_to(self.lib)}")
                continue
            d = classify_file(cfg, a)
            if d.folder and d.folder != cfg.review_dir:
                dest = self.dest_dir(d.folder, d, inbox.name)
                for f in group:
                    self.move(f, dest / f.name, d.reason)
            else:
                dest = self.lib / cfg.review_dir / inbox.name
                for f in group:
                    self.move(f, dest / f.name, d.reason or "review")
                self.review.append((a.name, d.reason))

        for f in files:
            if f in claimed or f.suffix.lower() in cfg.audio_ext:
                continue
            if f.suffix.lower() in cfg.sidecar_ext:
                if cfg.orphan_sidecars == "review" and not self.recent(f):
                    self.move(f, self.lib / cfg.review_dir / inbox.name / f.name, "orphan peak file")
                else:
                    self.skipped.append((str(f.relative_to(self.lib)), "orphan peak file"))
            else:
                self.skipped.append((str(f.relative_to(self.lib)), "not audio"))

    # ---- whole packs (subfolders) in an inbox
    def sort_pack(self, inbox: Path, pack: Path):
        cfg = self.cfg
        audio, newest = [], 0.0
        for root, dirs, fnames in os.walk(pack):
            dirs[:] = [d for d in dirs if d.lower() not in cfg.ignore]
            for fn in fnames:
                p = Path(root, fn)
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    pass
                if p.suffix.lower() in cfg.audio_ext:
                    audio.append(p)
        if self.now - newest < cfg.min_age:
            self.skipped.append((str(pack.relative_to(self.lib)) + "/", "pack modified recently"))
            return
        # 1) a pack's own name is usually the best label ("Gun Handling", "NEON - SCI-FI Elements")
        winner, reason = pick(keyword_scores(cfg, pack.name), 1), ""
        if winner:
            why = f"pack name -> {winner}"
        else:
            # 2) otherwise let the files vote
            votes: Counter = Counter(classify_file(cfg, a).folder for a in audio)
            votes.pop(None, None)
            if votes:
                top, n = votes.most_common(1)[0]
                share = n / max(1, len(audio))
                if share >= cfg.pack_min:
                    winner, why = top, f"pack: {share:.0%} of files agree on {top}"
                else:
                    reason = f"pack: only {share:.0%} of files agree on {top}"
            else:
                reason = "pack: nothing recognizable"
        if winner and winner != cfg.review_dir:
            target = find_dir_ci(self.lib, winner) / pack.name
            if not target.exists():
                self.move(pack, target, why)
                return
            reason = f"pack: {winner}/{pack.name} already exists"
        self.move(pack, self.lib / cfg.review_dir / pack.name, reason)
        self.review.append((pack.name + "/", reason))

    # ---- entry point
    def run(self):
        cfg = self.cfg
        if not self.lib.is_dir():
            sys.exit(f"sfx_sort: library not found: {self.lib}")
        for name in cfg.inboxes:
            inbox = find_dir_ci(self.lib, name)
            if not inbox.is_dir():
                continue
            entries = [e for e in inbox.iterdir() if e.name.lower() not in cfg.ignore and not e.name.startswith(".")]
            self.sort_files(inbox, [e for e in entries if e.is_file()])
            for pack in sorted(e for e in entries if e.is_dir()):
                self.sort_pack(inbox, pack)
        self.report()

    def report(self):
        cfg = self.cfg
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        rel = lambda p: os.path.relpath(p, self.lib)
        summary = Counter()
        for _, dst, _ in self.moves:
            summary[Path(rel(dst)).parts[0]] += 1
        tag = "DRY RUN - nothing moved" if self.dry else "moved"
        print(f"sfx_sort {stamp}: {len(self.moves)} items {tag}, {len(self.review)} for review, {len(self.skipped)} skipped")
        for folder, n in summary.most_common():
            print(f"  {n:5d}  -> {folder}")
        if self.dry:
            for src, dst, why in self.moves:
                print(f"  {rel(src)}\n      -> {rel(dst)}   [{why}]")
        for item, why in self.skipped:
            print(f"  skip {item}  [{why}]")
        if self.dry or not self.moves:
            return
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        journal = cfg.log_dir / f"moves-{stamp}.tsv"
        with open(journal, "w", encoding="utf-8") as f:
            f.write(f"# library\t{self.lib}\n")
            for src, dst, why in self.moves:
                f.write(f"{rel(src)}\t{rel(dst)}\t{why}\n")
        with open(cfg.log_dir / "review-latest.txt", "w", encoding="utf-8") as f:
            f.write(f"# {len(self.review)} items moved to {cfg.review_dir}/ on {stamp}\n")
            f.write("# Paste this list into Claude and ask which library folder each belongs in.\n")
            for item, why in self.review:
                f.write(f"{item}\t# {why}\n")
        print(f"  journal: {journal}")


def undo(journal: Path, dry: bool):
    lines = journal.read_text(encoding="utf-8").splitlines()
    lib = Path(lines[0].split("\t", 1)[1]) if lines and lines[0].startswith("# library") else None
    if not lib:
        sys.exit("sfx_sort: journal has no library header")
    done = 0
    for line in reversed(lines[1:]):
        src, dst, *_ = line.split("\t")
        s, d = lib / src, lib / dst
        if d.exists() and not s.exists():
            print(f"  {dst} -> {src}")
            if not dry:
                s.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(s))
            done += 1
        else:
            print(f"  skip {dst} (missing, or original path is taken)")
    print(f"undo: {done} restored{' (dry run)' if dry else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=HERE / "sfx_sort.toml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", type=Path, metavar="JOURNAL")
    args = ap.parse_args()

    lock = open(HERE / ".sfx_sort.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("sfx_sort: another run is in progress")

    if args.undo:
        return undo(args.undo, args.dry_run)
    Sorter(Config(args.config), args.dry_run).run()


if __name__ == "__main__":
    main()
