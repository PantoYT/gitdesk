#!/usr/bin/env python3
"""
gitdesk - panel nad wszystkimi repozytoriami na tej maszynie.

GitHub Desktop pokazuje repo, ktore mu recznie dodasz, po jednym. Tutaj chodzi
o cos innego: zeskanowac skonfigurowane korzenie i odpowiedziec na pytania,
ktorych zaden klient nie zadaje, bo widzi tylko jedno repo naraz:

  - gdzie nie ma commita, a gdzie commit jest, ale nie ma pusha
  - co jest publiczne, a co prywatne
  - ktora kopia robocza jest z przodu, gdy to samo repo lezy na PC i pendrivie
  - gdzie sekret jest o jeden 'git add -A' od wyciekniecia

Skan sekretow nie jest tu pisany od nowa - laduje go z workspace-doctor.

Bez zaleznosci zewnetrznych - sama biblioteka standardowa.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_DIR = HERE / "state"
INDEX_PATH = STATE_DIR / "index.json"

# --------------------------------------------------------------------------
# konfiguracja
# --------------------------------------------------------------------------

# Etykiety intencji. Bez nich narzedzie zglasza jako usterke kazde repo bez
# zdalnego - a wtedy uczy, zeby ignorowac czerwone.
LOCAL_ONLY = "local_only"   # celowo bez remote'a, nie proponuj GitHuba
FOREIGN = "foreign"         # nie moj kod, zero akcji zapisujacych

CONFIG_DEFAULT = {
    "roots": [
        {"path": r"E:\Pliki\Projects", "mode": "rw"},
        {"path": r"G:\ ".strip(), "mode": "rw"},
        {"path": str(Path.home() / "Documents" / "GitHub"), "mode": "rw"},
        {"path": r"E:\Pliki\Backup", "mode": "archive"},
    ],
    "doctor": "../workspace-doctor/doctor.py",
    "owner": "PantoYT",
    "port": 7420,
    "max_depth": 6,
    # Kopie bez .git - dziala, ale zaden klient gita ich nie widzi, wiec cicho
    # sie starzeja. Tu dostaja kolumne "ile plikow rozjechalo sie ze zrodlem".
    "deployments": [
        {
            "name": "dropgate (pendrive)",
            "path": r"G:\Portable\dropgate",
            "source": r"E:\Pliki\Projects\dropgate",
        },
    ],
    "labels": {
        r"E:\Pliki\Projects\MultiMuteUs": LOCAL_ONLY,
        r"E:\Pliki\Projects\Pontifex": LOCAL_ONLY,
        r"E:\Pliki\Projects\SaaS": LOCAL_ONLY,
        r"E:\Pliki\Projects\server": LOCAL_ONLY,
        r"E:\Pliki\Projects\WebLeads": LOCAL_ONLY,
        r"E:\Pliki\Projects\Amberfall": LOCAL_ONLY,
        r"E:\Pliki\Projects\ecc": FOREIGN,
        r"E:\Pliki\Projects\E.E.V.E.E-main": FOREIGN,
    },
}


def config_load() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(CONFIG_DEFAULT, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Utworzylem {CONFIG_PATH.name} z domyslnymi ustawieniami.", file=sys.stderr)
        return json.loads(json.dumps(CONFIG_DEFAULT))
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def config_save(conf: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(conf, indent=2, ensure_ascii=False), encoding="utf-8")


def norm_key(p: str | Path) -> str:
    """Klucz etykiety - sciezka bez wrazliwosci na wielkosc liter i separator."""
    return str(p).replace("/", "\\").rstrip("\\").lower()


# --------------------------------------------------------------------------
# workspace-doctor jako modul
# --------------------------------------------------------------------------


def load_doctor(conf: dict):
    """Laduje doctor.py ze sciezki z configu.

    Brak pliku to twardy blad, nie ciche pominiecie: gitdesk pozwala commitowac
    i pushowac, a jedyne, co stoi miedzy tym a wyciekiem klucza, to skan doktora.
    Narzedzie, ktore po cichu wylacza swoj failsafe, jest gorsze niz jego brak.
    """
    raw = conf.get("doctor", CONFIG_DEFAULT["doctor"])
    path = Path(raw)
    if not path.is_absolute():
        path = (HERE / path).resolve()
    if not path.is_file():
        sys.exit(
            f"BLAD: nie znalazlem workspace-doctor pod {path}\n"
            f"       gitdesk nie uruchomi sie bez skanu sekretow.\n"
            f"       Popraw pole 'doctor' w {CONFIG_PATH.name}."
        )
    spec = importlib.util.spec_from_file_location("wsdoctor", path)
    if spec is None or spec.loader is None:
        sys.exit(f"BLAD: nie moge zaladowac {path} jako modulu.")
    mod = importlib.util.module_from_spec(spec)
    # Rejestracja PRZED exec_module jest obowiazkowa: @dataclass w doktorze
    # siega do sys.modules[cls.__module__], zeby rozstrzygnac adnotacje typow.
    # Bez tej linii wysypuje sie na 'NoneType has no attribute __dict__'.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    for need in ("SECRET_FILE_RE", "SECRET_CONTENT", "looks_synthetic", "git"):
        if not hasattr(mod, need):
            sys.exit(f"BLAD: {path} nie ma '{need}' - niezgodna wersja workspace-doctor.")
    return mod


# --------------------------------------------------------------------------
# odkrywanie repozytoriow
# --------------------------------------------------------------------------

# Katalogi, w ktorych nie ma czego szukac. Czesc to smieci systemowe, czesc to
# miejsca, gdzie repo owszem sa, ale cudze - scoop i wtyczki Claude'a instaluja
# dziesiatki klonow, ktorych nikt nie chce ogladac na liscie swoich projektow.
PRUNE_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    "target", ".next", ".turbo", "site-packages", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "vendor", "$RECYCLE.BIN", "System Volume Information",
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "AppData", "scoop", ".cargo", ".rustup", ".nuget", ".gradle",
    "Emulation", "roms",
}

# Pelne sciezki do pominiecia (dopasowanie po prefiksie, bez wielkosci liter).
PRUNE_PATHS = {
    norm_key(Path.home() / ".claude" / "plugins"),
    norm_key(Path.home() / ".vscode" / "extensions"),
}


def discover(conf: dict) -> list[dict]:
    """Znajduje repozytoria w skonfigurowanych korzeniach.

    Kluczowa decyzja: po trafieniu na .git NIE schodzimy glebiej. Dzieki temu
    MultiMuteUs jest jednym repo, a nie pieciona - jego amonguscapture/,
    automuteus/, galactus/ i deploy/ to zawendorowane upstreamy z wlasnym .git.
    """
    max_depth = int(conf.get("max_depth", 6))
    found: list[dict] = []
    seen: set[str] = set()

    for root in conf.get("roots", []):
        base = Path(root["path"])
        mode = root.get("mode", "rw")
        if not base.is_dir():
            continue

        stack = [(base, 0)]
        while stack:
            cur, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue

            if any(e.name == ".git" for e in entries):
                key = norm_key(cur)
                if key not in seen:
                    seen.add(key)
                    found.append({"path": str(cur), "root": str(base), "mode": mode})
                continue        # nie schodzimy w glab repo

            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name in PRUNE_NAMES or e.name.startswith("$"):
                    continue
                if norm_key(e.path) in PRUNE_PATHS:
                    continue
                stack.append((Path(e.path), depth + 1))

    found.sort(key=lambda r: norm_key(r["path"]))
    return found


# --------------------------------------------------------------------------
# stan pojedynczego repo
# --------------------------------------------------------------------------


@dataclass
class Repo:
    path: str
    root: str
    mode: str = "rw"
    name: str = ""
    label: str = ""             # "", local_only, foreign
    branch: str = "?"
    remote: str = ""
    remote_key: str = ""        # znormalizowane github.com/owner/name
    upstream: str = ""
    dirty: int = 0
    ahead: int = 0
    behind: int = 0
    last_commit: int = 0        # epoch
    fetched_at: int = 0
    visibility: str = ""        # PUBLIC | PRIVATE | obce | (puste)
    twin_of: list[str] = field(default_factory=list)
    verdict: str = ""
    error: str = ""

    @property
    def writable(self) -> bool:
        return self.mode == "rw" and self.label != FOREIGN


REMOTE_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?|git@|ssh://git@)([^/:]+)[/:](.+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def normalize_remote(url: str) -> str:
    """https://github.com/PantoYT/foo.git i git@github.com:PantoYT/foo -> ten sam klucz."""
    url = (url or "").strip()
    if not url:
        return ""
    m = REMOTE_RE.match(url)
    if not m:
        return url.lower()
    return f"{m.group(1).lower()}/{m.group(2).lower()}"


def _run(repo: str, *args: str, timeout: int = 60) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def probe(entry: dict, labels: dict) -> Repo:
    """Jedno 'status --porcelain=v2 --branch' daje galaz, brudne pliki i ahead/behind.

    workspace-doctor wolal na to trzy osobne polecenia (remote, status, rev-list)
    - przy 50 repo to widac.
    """
    r = Repo(path=entry["path"], root=entry["root"], mode=entry.get("mode", "rw"))
    r.name = Path(r.path).name
    r.label = labels.get(norm_key(r.path), "")

    st = _run(r.path, "status", "--porcelain=v2", "--branch")
    if st is None:
        r.error = "git status nie przeszedl"
        return r

    for line in st.splitlines():
        if not line.startswith("# "):
            if line.strip():
                r.dirty += 1
            continue
        parts = line[2:].split(" ", 1)
        if len(parts) != 2:
            continue
        key, val = parts[0], parts[1].strip()
        if key == "branch.head":
            r.branch = val
        elif key == "branch.upstream":
            r.upstream = val
        elif key == "branch.ab":
            for tok in val.split():
                if tok.startswith("+"):
                    r.ahead = int(tok[1:])
                elif tok.startswith("-"):
                    r.behind = int(tok[1:])

    url = (_run(r.path, "remote", "get-url", "origin") or "").strip()
    r.remote = url
    r.remote_key = normalize_remote(url)

    ts = (_run(r.path, "log", "-1", "--format=%ct") or "").strip()
    if ts.isdigit():
        r.last_commit = int(ts)

    # czas ostatniego fetcha - git zapisuje go jako mtime FETCH_HEAD
    fh = Path(r.path) / ".git" / "FETCH_HEAD"
    try:
        if fh.exists():
            r.fetched_at = int(fh.stat().st_mtime)
    except OSError:
        pass

    return r


def probe_all(entries: list[dict], labels: dict, workers: int = 8) -> list[Repo]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda e: probe(e, labels), entries))


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def fetch_all(repos: list[Repo], workers: int = 8) -> tuple[int, int]:
    """Bez tego ahead/behind klamie.

    'git status' liczy roznice wzgledem origin/... zapisanego lokalnie przy
    ostatnim fetchu. Jesli fetcha nie bylo od tygodnia, 'wszystko zsynchronizowane'
    znaczy tylko 'bylo zsynchronizowane tydzien temu'.
    """
    targets = [r for r in repos if r.remote and r.mode == "rw"]
    ok = fail = 0

    def one(r: Repo) -> bool:
        return _run(r.path, "fetch", "--all", "--prune", "--quiet", timeout=90) is not None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r, good in zip(targets, pool.map(one, targets)):
            if good:
                ok += 1
                r.fetched_at = int(time.time())
            else:
                fail += 1
                r.error = "fetch nie przeszedl"
    return ok, fail


# --------------------------------------------------------------------------
# widocznosc public/private
# --------------------------------------------------------------------------


def visibility_map(owner: str) -> dict[str, str]:
    """Jedno wywolanie gh na wszystkie repo, nie jedno na kazde."""
    try:
        out = subprocess.run(
            ["gh", "repo", "list", owner, "--limit", "300",
             "--json", "name,visibility"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    return {f"github.com/{owner.lower()}/{d['name'].lower()}": d["visibility"]
            for d in data}


def apply_visibility(repos: list[Repo], owner: str) -> None:
    vis = visibility_map(owner)
    prefix = f"github.com/{owner.lower()}/"
    for r in repos:
        if not r.remote_key:
            continue
        if r.remote_key in vis:
            r.visibility = vis[r.remote_key]
        elif not r.remote_key.startswith(prefix):
            r.visibility = "obce"


# --------------------------------------------------------------------------
# blizniaki
# --------------------------------------------------------------------------


STALE_AFTER = 86400     # po dobie stan remote'a to juz tylko domysl


def stale(r: Repo) -> bool:
    """Czy wiedza o remote jest przeterminowana.

    'git status' liczy ahead/behind wzgledem origin/... zapisanego lokalnie przy
    ostatnim fetchu. Repo, ktorego nie fetchowano od 111 dni, moze rownie dobrze
    byc 20 commitow z tylu - status pokaze zero i bedzie formalnie poprawny.
    """
    if not r.remote:
        return False
    return not r.fetched_at or (time.time() - r.fetched_at) > STALE_AFTER


def find_twins(repos: list[Repo]) -> dict[str, list[Repo]]:
    """Grupuje kopie robocze wskazujace na ten sam remote.

    Werdykt liczymy bez siegania miedzy repo - kazda kopia ma wlasne origin/<branch>,
    wiec porownanie ahead/behind kazdej z osobna wystarczy. 'git merge-base A B'
    NIE zadziala, bo to osobne bazy obiektow: kopia z PC nie zna commitow z pendrive'a.
    """
    groups: dict[str, list[Repo]] = {}
    for r in repos:
        if r.remote_key:
            groups.setdefault(r.remote_key, []).append(r)
    twins = {k: v for k, v in groups.items() if len(v) > 1}

    for key, members in twins.items():
        live = [m for m in members if m.mode == "rw"]
        for m in members:
            m.twin_of = [o.path for o in members if o.path != m.path]
            if m.mode == "archive":
                m.verdict = "archiwum"
                continue
            if len(live) < 2:
                m.verdict = "jedyna zywa kopia"
                continue
            others = [o for o in live if o.path != m.path]

            # Rozjazd i wyprzedzenie sa prawdziwe niezaleznie od swiezosci -
            # lokalny commit jest faktem. Ale "zgodne" to twierdzenie o remote,
            # a remote znamy tylko z ostatniego fetcha. Bez swiezych danych
            # zamiast klamac mowimy, ze nie wiemy.
            if m.ahead and any(o.ahead for o in others):
                m.verdict = f"ROZJAZD (+{m.ahead})"
            elif m.ahead:
                m.verdict = f"z przodu o {m.ahead}"
            elif any(o.ahead for o in others):
                m.verdict = "z tylu"
            elif m.behind or any(o.behind for o in others):
                m.verdict = "obie za remote"
            elif stale(m) or any(stale(o) for o in others):
                m.verdict = "niezweryfikowane"
            else:
                m.verdict = "zgodne"
    return twins


# --------------------------------------------------------------------------
# wdrozenia - kopie bez .git
# --------------------------------------------------------------------------


@dataclass
class Deployment:
    name: str
    path: str
    source: str
    same: int = 0
    differs: int = 0
    missing: int = 0
    secrets: list[str] = field(default_factory=list)
    error: str = ""


def check_deployments(conf: dict, doctor) -> list[Deployment]:
    """Kopia dziala, ale nie umie powiedziec 'jestem 4 commity za repo'.

    Skoro nie ma .git, jedyne co da sie zrobic, to porownac tresc plik po pliku
    z HEAD repo zrodlowego. Przy okazji zgladamy, co takiego kopia niesie, czego
    w repo nie ma - bo to wlasnie tam laduja klucze i stan, ktory gitignore
    slusznie trzyma poza repo, a ktory razem z nosnikiem wychodzi z domu.
    """
    import hashlib

    out: list[Deployment] = []
    for spec in conf.get("deployments", []):
        d = Deployment(name=spec.get("name") or Path(spec["path"]).name,
                       path=spec["path"], source=spec["source"])
        dep, src = Path(d.path), Path(d.source)
        if not dep.is_dir():
            d.error = "kopia niedostepna (nosnik odlaczony?)"
            out.append(d)
            continue
        if not (src / ".git").exists():
            d.error = "repo zrodlowe nie jest repozytorium"
            out.append(d)
            continue

        tracked = (_run(str(src), "ls-files") or "").splitlines()
        for rel in tracked:
            if not rel:
                continue
            a, b = src / rel, dep / rel
            if not b.is_file():
                d.missing += 1
                continue
            try:
                ha = hashlib.blake2b(a.read_bytes(), digest_size=16).digest()
                hb = hashlib.blake2b(b.read_bytes(), digest_size=16).digest()
            except OSError:
                d.missing += 1
                continue
            if ha == hb:
                d.same += 1
            else:
                d.differs += 1

        # Sama nazwa nie wystarczy. 'backup_key' to klucz prywatny OpenSSH, ale
        # SECRET_FILE_RE wymaga kropki przed rozszerzeniem, wiec plik bez
        # rozszerzenia przelatuje. Dlatego drugi przebieg po TRESCI - te same
        # wzorce, ktorych doktor uzywa w hooku pre-commit.
        for path, size in doctor.walk(dep):
            rel = str(path.relative_to(dep)).replace("\\", "/")
            if doctor.SECRET_FILE_RE.search(rel):
                d.secrets.append(rel)
                continue
            if size > doctor.MAX_SCAN_BYTES:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            for label, pat in doctor.SECRET_CONTENT:
                m = pat.search(blob)
                if m and not doctor.looks_synthetic(m.group(0)):
                    d.secrets.append(f"{rel} ({label})")
                    break
        out.append(d)
    return out


def render_deployments(deps: list[Deployment]) -> None:
    if not deps:
        return
    print(f"{C.BOLD}Wdrozenia{C.OFF} {C.DIM}(kopie bez .git){C.OFF}")
    for d in deps:
        if d.error:
            print(f"  {C.YELLOW}{d.name}{C.OFF}  {C.DIM}{d.error}{C.OFF}")
            continue
        drift = (f"{C.YELLOW}rozjazd: {d.differs} plikow, brak: {d.missing}{C.OFF}"
                 if (d.differs or d.missing) else f"{C.GREEN}zgodne ze zrodlem{C.OFF}")
        print(f"  {d.name}  {C.DIM}{d.same} zgodnych{C.OFF}  {drift}")
        if d.secrets:
            print(f"    {C.RED}niesie sekrety poza repo:{C.OFF} " + ", ".join(d.secrets))
            print(f"    {C.DIM}zgubiony nosnik = te pliki w cudzych rekach{C.OFF}")
    print()


# --------------------------------------------------------------------------
# skan + cache
# --------------------------------------------------------------------------


def scan(conf: dict, do_fetch: bool = False, do_vis: bool = True) -> list[Repo]:
    entries = discover(conf)
    labels = {norm_key(k): v for k, v in conf.get("labels", {}).items()}
    repos = probe_all(entries, labels)
    if do_fetch:
        fetch_all(repos)
        repos = probe_all(entries, labels)      # ahead/behind po swiezym fetchu
    if do_vis:
        apply_visibility(repos, conf.get("owner", ""))
    find_twins(repos)
    save_index(repos)
    return repos


def save_index(repos: list[Repo]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"at": int(time.time()), "repos": [asdict(r) for r in repos]},
        indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INDEX_PATH)


def load_index() -> list[Repo] | None:
    if not INDEX_PATH.exists():
        return None
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [Repo(**d) for d in data.get("repos", [])]


# --------------------------------------------------------------------------
# raport tekstowy
# --------------------------------------------------------------------------


class C:
    RED = "\033[31m"; YELLOW = "\033[33m"; GREEN = "\033[32m"
    CYAN = "\033[36m"; MAG = "\033[35m"; DIM = "\033[2m"; BOLD = "\033[1m"; OFF = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        for n in ("RED", "YELLOW", "GREEN", "CYAN", "MAG", "DIM", "BOLD", "OFF"):
            setattr(cls, n, "")

    @staticmethod
    def enable_windows_ansi() -> None:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)


def age(ts: int) -> str:
    if not ts:
        return "-"
    d = int(time.time()) - ts
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def render_list(repos: list[Repo], conf: dict) -> None:
    if not repos:
        print("Nie znalazlem zadnych repozytoriow. Sprawdz 'roots' w config.json.")
        return

    w = max(len(r.name) for r in repos)
    print(f"\n{C.BOLD}gitdesk{C.OFF}  {C.DIM}{len(repos)} repozytoriow{C.OFF}\n")

    last_root = None
    for r in sorted(repos, key=lambda x: (norm_key(x.root), norm_key(x.path))):
        if r.root != last_root:
            tag = f" {C.DIM}[archiwum]{C.OFF}" if r.mode == "archive" else ""
            print(f"{C.DIM}{r.root}{C.OFF}{tag}")
            last_root = r.root

        bits = []
        if r.error:
            bits.append(f"{C.RED}{r.error}{C.OFF}")
        if r.dirty:
            col = C.DIM if r.label == LOCAL_ONLY else C.YELLOW
            bits.append(f"{col}brudne:{r.dirty}{C.OFF}")
        if r.ahead:
            bits.append(f"{C.RED}niewypchniete:{r.ahead}{C.OFF}")
        if r.behind:
            bits.append(f"{C.CYAN}z tylu:{r.behind}{C.OFF}")
        if not r.remote:
            if r.label == LOCAL_ONLY:
                bits.append(f"{C.DIM}lokalne z wyboru{C.OFF}")
            elif r.label == FOREIGN:
                bits.append(f"{C.DIM}obce{C.OFF}")
            else:
                bits.append(f"{C.RED}BEZ ZDALNEGO{C.OFF}")
        if r.visibility == "PUBLIC":
            bits.append(f"{C.MAG}publiczne{C.OFF}")
        elif r.visibility == "PRIVATE":
            bits.append(f"{C.DIM}prywatne{C.OFF}")
        elif r.visibility == "obce":
            bits.append(f"{C.DIM}obce{C.OFF}")
        if r.verdict:
            col = C.RED if "ROZJAZD" in r.verdict else C.DIM
            bits.append(f"{col}blizniak: {r.verdict}{C.OFF}")
        if not bits:
            # "czysto" bez swiezego fetcha to zdanie o przeszlosci, nie o stanie.
            bits.append(f"{C.DIM}niezweryfikowane{C.OFF}" if stale(r)
                        else f"{C.GREEN}czysto{C.OFF}")

        fa = age(r.fetched_at)
        fcol = C.YELLOW if stale(r) else C.DIM
        print(f"  {r.name:<{w}}  {C.DIM}{r.branch:<12}{C.OFF} "
              f"{fcol}fetch {fa:>4}{C.OFF}  " + "  ".join(bits))

    # podsumowanie
    real_noremote = [r for r in repos
                     if not r.remote and not r.label and r.mode == "rw"]
    dirty = [r for r in repos if r.dirty and r.mode == "rw"]
    unpushed = [r for r in repos if r.ahead and r.mode == "rw"]
    twins = find_twins(repos)
    stale_repos = [r for r in repos if r.mode == "rw" and stale(r)]

    print()
    print(f"{C.DIM}brudne     :{C.OFF} {len(dirty)}    "
          f"{C.DIM}niewypchniete:{C.OFF} {len(unpushed)}    "
          f"{C.DIM}bez zdalnego (do decyzji):{C.OFF} {len(real_noremote)}")
    print(f"{C.DIM}blizniaki  :{C.OFF} {len(twins)} grup    "
          f"{C.DIM}nieswiezy fetch (>24h):{C.OFF} {len(stale_repos)}")
    if real_noremote:
        print(f"{C.RED}Do decyzji:{C.OFF} " + ", ".join(r.name for r in real_noremote))
    print()


def render_twins(repos: list[Repo]) -> None:
    twins = find_twins(repos)
    if not twins:
        print("Brak blizniakow - kazde repo ma jedna kopie robocza.")
        return
    print(f"\n{C.BOLD}Blizniaki{C.OFF}  {C.DIM}{len(twins)} grup{C.OFF}\n")
    for key, members in sorted(twins.items()):
        print(f"{C.BOLD}{key}{C.OFF}")
        for m in sorted(members, key=lambda x: x.mode != "rw"):
            col = C.RED if "ROZJAZD" in m.verdict else (
                C.DIM if m.mode == "archive" else C.GREEN)
            print(f"   {col}{m.verdict:<18}{C.OFF} {C.DIM}+{m.ahead}/-{m.behind} "
                  f"brudne:{m.dirty}{C.OFF}  {m.path}")
        print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="gitdesk",
        description="Panel nad wszystkimi repozytoriami: commity, pushe, widocznosc, blizniaki.")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "twins", "scan", "serve"],
                    help="list (domyslne) | twins | scan | serve")
    ap.add_argument("--fetch", action="store_true",
                    help="odswiez remote'y przed raportem (wolniejsze, ale prawdziwe)")
    ap.add_argument("--cached", action="store_true",
                    help="uzyj ostatniego skanu zamiast skanowac od nowa")
    ap.add_argument("--no-gh", action="store_true", help="pomin odpytanie gh o widocznosc")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        C.disable()
    elif os.name == "nt":
        try:
            C.enable_windows_ansi()
        except (OSError, AttributeError):
            C.disable()

    conf = config_load()
    doctor = load_doctor(conf)      # twardy blad, jesli brak - zanim cokolwiek zrobimy

    if args.cached:
        repos = load_index()
        if repos is None:
            print("Brak cache - skanuje od nowa.", file=sys.stderr)
            repos = scan(conf, do_fetch=args.fetch, do_vis=not args.no_gh)
        else:
            find_twins(repos)
    else:
        repos = scan(conf, do_fetch=args.fetch, do_vis=not args.no_gh)

    if args.json:
        print(json.dumps([asdict(r) for r in repos], indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "twins":
        render_twins(repos)
    elif args.cmd == "scan":
        print(f"Znalazlem {len(repos)} repozytoriow, zapisalem {INDEX_PATH}")
    elif args.cmd == "serve":
        print("Panel jeszcze niegotowy - faza 3.", file=sys.stderr)
        return 2
    else:
        render_list(repos, conf)
        render_deployments(check_deployments(conf, doctor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
