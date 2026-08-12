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
import html
import importlib.util
import json
import os
import re
import secrets as _secrets
import socket
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
    # `medium` to nazwa NOSNIKA, nie ocena. Repo na pendrivie nie jest kopia
    # zapasowa PC-ta - bywa zrodlem: commit powstaje tam, a PC go zaciaga.
    # Bez tej etykiety widok grupowy kaze sie domyslac z sciezki, gdzie co lezy.
    "roots": [
        {"path": r"E:\Pliki\Projects", "mode": "rw", "medium": "PC"},
        {"path": r"G:\ ".strip(), "mode": "rw", "medium": "pendrive"},
        {"path": str(Path.home() / "Documents" / "GitHub"), "mode": "rw",
         "medium": "PC/Dokumenty"},
        {"path": r"E:\Pliki\Backup", "mode": "archive", "medium": "archiwum"},
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


def guess_medium(path: str, mode: str) -> str:
    """Nazwa nosnika z systemu, nie z zaszytej litery dysku.

    Pendrive potrafi wjechac jako G:, H: albo E: - zaleznie od tego, co juz
    jest wpiete. Zaszyta litera znaczylaby, ze po zmianie portu narzedzie
    nagle uznaje ten sam nosnik za cos innego.
    """
    if mode == "archive":
        return "archiwum"
    if os.name != "nt":
        return "dysk"
    try:
        import ctypes
        drive = str(Path(path).anchor)          # "G:\\"
        if not drive:
            return "dysk"
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
    except (OSError, AttributeError, ValueError):
        return "dysk"
    return {2: "pendrive", 3: "PC", 4: "dysk sieciowy", 5: "plyta",
            6: "ramdysk"}.get(kind, "dysk")


def config_load() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(CONFIG_DEFAULT, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Utworzylem {CONFIG_PATH.name} z domyslnymi ustawieniami.", file=sys.stderr)
        conf = json.loads(json.dumps(CONFIG_DEFAULT))
    else:
        conf = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    # Uzupelnienie starszych configow: korzen bez `medium` dostaje nazwe
    # nosnika wykryta z systemu. Zapisane recznie wartosci zostaja nietkniete.
    changed = False
    for root in conf.get("roots", []):
        if not root.get("medium"):
            root["medium"] = guess_medium(root["path"], root.get("mode", "rw"))
            changed = True
    if changed and CONFIG_PATH.exists():
        config_save(conf)
    return conf


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
                    found.append({"path": str(cur), "root": str(base), "mode": mode,
                                  "medium": root.get("medium", "")})
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
    medium: str = ""            # PC | pendrive | archiwum - nosnik, nie ocena
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
    r = Repo(path=entry["path"], root=entry["root"], mode=entry.get("mode", "rw"),
             medium=entry.get("medium", ""))
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

    # Czas ostatniego kontaktu ze zdalnym. FETCH_HEAD jest najpewniejszy, ale
    # SWIEZY KLON go nie ma - a swiezy klon ma z definicji aktualne refy zdalne.
    # Bez tego zapasowego tropu kazde nowo sklonowane repo klamaloby, ze jego
    # stan jest nieznany.
    gitdir = Path(r.path) / ".git"
    newest = 0
    for cand in (gitdir / "FETCH_HEAD", gitdir / "packed-refs",
                 gitdir / "refs" / "remotes"):
        try:
            if cand.exists():
                newest = max(newest, int(cand.stat().st_mtime))
        except OSError:
            continue
    r.fetched_at = newest

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
                # Zaden werdykt. Nosnik jest juz osobnym tagiem, a wpisywanie
                # tu drugi raz "archiwum" sprawialo, ze kolumna STANU krzyczala
                # "archiwum" przy grupie, w ktorej zywe kopie sa na PC i pendrivie
                # - i cala grupa czytala sie jako martwa.
                m.verdict = ""
                continue
            if len(live) < 2:
                m.verdict = "jedyna zywa kopia"
                continue
            others = [o for o in live if o.path != m.path]

            # Rozjazd i wyprzedzenie sa prawdziwe niezaleznie od swiezosci -
            # lokalny commit jest faktem. Ale "zgodne" to twierdzenie o remote,
            # a remote znamy tylko z ostatniego fetcha. Bez swiezych danych
            # zamiast klamac mowimy, ze nie wiemy.
            def where(seq):
                names = [o.medium or Path(o.path).parent.name for o in seq]
                return ", ".join(dict.fromkeys(names))

            newer = [o for o in others if o.ahead]
            if m.ahead and newer:
                m.verdict = f"ROZJAZD (+{m.ahead}, tez na: {where(newer)})"
            elif m.ahead:
                m.verdict = f"z przodu o {m.ahead} — push, potem pull na: {where(others)}"
            elif newer:
                m.verdict = f"z tylu — pull (nowsze na: {where(newer)})"
            elif m.behind or any(o.behind for o in others):
                m.verdict = "obie za zdalnym — pull"
            elif stale(m) or any(stale(o) for o in others):
                # Nie "martwe" - tylko dawno nieodpytywane. Roznica jest istotna:
                # te kopie bywaja zrodlem commitow, nie sa kopia zapasowa PC-ta.
                m.verdict = f"bez fetcha od {age(m.fetched_at)} — stan nieznany"
            else:
                m.verdict = "zsynchronizowane"
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
# skan sekretow dla POJEDYNCZEGO repo
# --------------------------------------------------------------------------


def secret_crits(repo: str, doctor) -> list[str]:
    """Sekrety w JEDNYM repo. doctor.check_secrets() chodzi po calym korzeniu,
    a przed pushem interesuje nas dokladnie to repo i nic wiecej."""
    out: list[str] = []
    listing = _run(repo, "ls-files")
    if listing is None:
        return out
    for rel in listing.splitlines():
        if rel and doctor.SECRET_FILE_RE.search(rel):
            out.append(f"sekret w repo: {rel}")
    st = _run(repo, "status", "--porcelain", "--untracked-files=all") or ""
    for line in st.splitlines():
        if line.startswith("??"):
            rel = line[3:].strip().strip('"')
            if doctor.SECRET_FILE_RE.search(rel):
                out.append(f"nieignorowany sekret: {rel}")
    return out


def staged_secrets(repo: str, doctor) -> list[str]:
    """To samo co hook pre-commit doktora, ale dla wskazanego repo: skanuje
    WYLACZNIE indeks, po nazwie i po tresci."""
    bad: list[str] = []
    staged = _run(repo, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    if not staged:
        return bad
    for rel in staged.splitlines():
        if not rel:
            continue
        if doctor.SECRET_FILE_RE.search(rel):
            bad.append(f"{rel} - nazwa wskazuje na plik z sekretem")
            continue
        try:
            blob = subprocess.run(["git", "-C", repo, "show", f":{rel}"],
                                  capture_output=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for label, pat in doctor.SECRET_CONTENT:
            m = pat.search(blob)
            if m and not doctor.looks_synthetic(m.group(0)):
                bad.append(f"{rel} - {label}")
                break
    return bad


# --------------------------------------------------------------------------
# akcje
# --------------------------------------------------------------------------


def act(rt, action: str, path: str, msg: str = "") -> tuple[bool, str]:
    """Jedno miejsce, w ktorym gitdesk pisze po repozytoriach.

    Zabezpieczenia sa tutaj, a nie w UI - przycisk mozna ominac, tej funkcji nie.
    """
    r = next((x for x in rt.repos if x.path == path), None)
    if r is None:
        return False, "nie znam takiego repo"
    if r.mode == "archive":
        return False, "korzen archiwalny - tylko do odczytu"
    if r.label == FOREIGN and action not in ("fetch", "pull"):
        return False, "repo oznaczone jako obce - bez akcji zapisujacych"

    if action == "fetch":
        ok = _run(r.path, "fetch", "--all", "--prune", "--quiet", timeout=90) is not None
        return ok, "odswiezone" if ok else "fetch nie przeszedl"

    if action == "pull":
        # --ff-only celowo: zbiorczy merge w 40 repo to nie jest cos,
        # co chce sie potem odkrecac.
        out = _run(r.path, "pull", "--ff-only", timeout=120)
        return (out is not None,
                "zaktualizowane" if out is not None
                else "pull odrzucony - historia sie rozjechala, potrzebny recznie")

    if action == "push":
        crits = secret_crits(r.path, rt.doctor)
        if crits and r.visibility == "PUBLIC":
            return False, "PUSH ZABLOKOWANY (repo publiczne): " + "; ".join(crits)
        out = _run(r.path, "push", timeout=180)
        return out is not None, "wypchniete" if out is not None else "push nie przeszedl"

    if action == "commit":
        if not msg.strip():
            return False, "pusty opis commita"
        if _run(r.path, "add", "-A") is None:
            return False, "git add nie przeszedl"
        bad = staged_secrets(r.path, rt.doctor)
        if bad:
            _run(r.path, "reset")
            return False, "COMMIT ODRZUCONY - sekret w indeksie: " + "; ".join(bad)
        out = _run(r.path, "commit", "-m", msg, timeout=60)
        return out is not None, "zacommitowane" if out is not None else "commit nie przeszedl"

    if action == "mark_local":
        rt.conf.setdefault("labels", {})[r.path] = LOCAL_ONLY
        config_save(rt.conf)
        r.label = LOCAL_ONLY
        return True, "oznaczone jako lokalne z wyboru"

    return False, f"nieznana akcja: {action}"


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------

PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d0d10;color:#d8d8dd;
 font:14px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace}
a{color:#f0b45e;text-decoration:none}
header{padding:18px 22px;border-bottom:1px solid #22222a;
 display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
h1{margin:0;font-size:16px;letter-spacing:.14em;text-transform:uppercase;color:#f0b45e}
.sum{color:#6a6a76;font-size:12px}
nav{padding:10px 22px;border-bottom:1px solid #22222a;display:flex;gap:6px;flex-wrap:wrap}
nav a{padding:5px 11px;border:1px solid #2a2a34;border-radius:2px;font-size:12px;color:#9a9aa6}
nav a.on{background:#f0b45e;color:#0d0d10;border-color:#f0b45e;font-weight:600}
main{padding:16px 22px 60px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
 color:#5a5a66;padding:8px 10px;border-bottom:1px solid #22222a;font-weight:600}
td{padding:8px 10px;border-bottom:1px solid #17171d;vertical-align:middle}
tr:hover td{background:#131318}
.nm{font-weight:600;color:#e8e8ee}
.pth{color:#4a4a56;font-size:11px}
.tag{display:inline-block;padding:1px 7px;border-radius:2px;font-size:11px;margin-right:5px}
.crit{background:#5a1f22;color:#ffb4b4}
.warn{background:#54401a;color:#ffd79a}
.info{background:#1c3a44;color:#9fdcea}
.ok{background:#1e3a26;color:#a5e0b5}
.mut{background:#232330;color:#7a7a88}
.pub{background:#43204a;color:#e2b0f0}
form{display:inline}
button{font:600 11px ui-monospace,monospace;color:#0d0d10;background:#8a8a99;
 border:0;border-radius:2px;padding:4px 9px;cursor:pointer;margin-right:4px}
button:hover{background:#f0b45e}
button.d{background:#2a2a34;color:#9a9aa6}
button.d:hover{background:#3a3a46;color:#d8d8dd}
input[type=text]{background:#17171d;border:1px solid #2a2a34;color:#d8d8dd;
 padding:4px 7px;font:12px ui-monospace,monospace;border-radius:2px;width:230px}
.bar{margin:0 0 16px;padding:11px 14px;background:#131318;border-left:2px solid #f0b45e}
.err{border-left-color:#c0484e;color:#ffb4b4}
.done{border-left-color:#4e9e63;color:#a5e0b5}
.hint{color:#5a5a66;font-size:11px;margin-top:3px}

/* widok kafelkowy */
.grid{display:grid;gap:12px;
 grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}
.card{background:#131318;border:1px solid #22222a;border-radius:4px;padding:13px 14px;
 border-left:3px solid #2a2a34}
.card:hover{border-color:#33333f;border-left-color:#f0b45e}
.card.a{border-left-color:#c0484e}
.card.w{border-left-color:#d0a050}
.card.g{border-left-color:#4e9e63}
.card.m{border-left-color:#3a3a46;opacity:.62}
.card.s{border-left-color:#4a7c8c}
.card h3{margin:0 0 2px;font-size:14px;color:#e8e8ee}
.card .br{color:#6a6a76;font-size:11px;margin-bottom:8px}
.card .tg{margin-bottom:10px;line-height:2}
.card .ac{border-top:1px solid #1f1f27;padding-top:9px}
.card input[type=text]{width:100%;margin-bottom:5px}

/* widok grupowy */
.grp{margin-bottom:20px;border:1px solid #22222a;border-radius:4px;overflow:hidden}
.grp>h2{margin:0;padding:9px 13px;background:#17171d;font-size:12px;font-weight:600;
 letter-spacing:.05em;color:#f0b45e}
.grp>h2 small{color:#5a5a66;font-weight:400;letter-spacing:0;margin-left:8px}
.grp.split>h2{background:#2a1618;color:#ffb4b4}
.grp table{margin:0}
.grp td{border-bottom:1px solid #17171d}
.grp tr:last-child td{border-bottom:0}
"""

VIEWS = ("lista", "kafelki", "grupy")


class Runtime:
    def __init__(self, conf, doctor, repos):
        self.conf = conf
        self.doctor = doctor
        self.repos = repos
        self.token = _secrets.token_urlsafe(24)
        self.flash: tuple[str, str] | None = None
        # zapamietane, zeby przekierowanie po akcji wracalo tam, gdzie bylo
        self.view = "lista"
        self.filter = "wszystko"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=pl><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body>{body}</body></html>").encode("utf-8")


FILTERS = {
    "wszystko": lambda r: True,
    "brudne": lambda r: r.dirty > 0,
    "niewypchniete": lambda r: r.ahead > 0,
    "z-tylu": lambda r: r.behind > 0,
    "bez-zdalnego": lambda r: not r.remote and not r.label and r.mode == "rw",
    "blizniaki": lambda r: bool(r.twin_of),
    "publiczne": lambda r: r.visibility == "PUBLIC",
    "nieswieze": lambda r: stale(r),
}


MEDIUM_CLS = {"pendrive": "info", "archiwum": "mut"}


def tags_for(r: Repo) -> str:
    t = []
    if r.medium:
        t.append(f"<span class='tag {MEDIUM_CLS.get(r.medium, 'mut')}'>"
                 f"{esc(r.medium)}</span>")
    if r.error:
        t.append(f"<span class='tag crit'>{esc(r.error)}</span>")
    if r.dirty:
        cls = "mut" if r.label == LOCAL_ONLY else "warn"
        t.append(f"<span class='tag {cls}'>brudne {r.dirty}</span>")
    if r.ahead:
        t.append(f"<span class='tag crit'>niewypchniete {r.ahead}</span>")
    if r.behind:
        t.append(f"<span class='tag info'>z tylu {r.behind}</span>")
    if not r.remote:
        if r.label == LOCAL_ONLY:
            t.append("<span class='tag mut'>lokalne z wyboru</span>")
        elif r.label == FOREIGN:
            t.append("<span class='tag mut'>obce</span>")
        else:
            t.append("<span class='tag crit'>BEZ ZDALNEGO</span>")
    if r.visibility == "PUBLIC":
        t.append("<span class='tag pub'>publiczne</span>")
    elif r.visibility == "PRIVATE":
        t.append("<span class='tag mut'>prywatne</span>")
    elif r.visibility == "obce":
        t.append("<span class='tag mut'>obce</span>")
    if r.verdict:
        cls = "crit" if "ROZJAZD" in r.verdict else "mut"
        t.append(f"<span class='tag {cls}'>{esc(r.verdict)}</span>")
    # Sam nosnik to nie jest informacja o stanie - jesli nic poza nim nie doszlo,
    # repo jest czyste (albo po prostu dawno nieodpytywane).
    if len(t) <= (1 if r.medium else 0):
        t.append(f"<span class='tag info'>bez fetcha od {esc(age(r.fetched_at))}</span>"
                 if stale(r) else "<span class='tag ok'>czysto</span>")
    return "".join(t)


def buttons_for(r: Repo, token: str) -> str:
    if r.mode == "archive":
        return "<span class=pth>archiwum — tylko odczyt</span>"

    def form(action: str, label: str, dim: bool = False, extra: str = "") -> str:
        return (f"<form method=post action=/akcja>"
                f"<input type=hidden name=t value='{esc(token)}'>"
                f"<input type=hidden name=a value='{action}'>"
                f"<input type=hidden name=p value='{esc(r.path)}'>{extra}"
                f"<button class='{'d' if dim else ''}'>{label}</button></form>")

    b = []
    if r.remote:
        b.append(form("fetch", "fetch", dim=True))
        if r.behind:
            b.append(form("pull", f"pull {r.behind}"))
        if r.ahead and r.label != FOREIGN:
            b.append(form("push", f"push {r.ahead}"))
    if r.dirty and r.label != FOREIGN:
        b.append(form("commit", "commit",
                      extra="<input type=text name=m placeholder='opis commita' required>"))
    if not r.remote and not r.label:
        b.append(form("mark_local", "to jest lokalne z wyboru", dim=True))
    return "".join(b) or "<span class=pth>—</span>"


def card_class(r: Repo) -> str:
    """Kolor lewej krawedzi kafelka - to, co ma rzucic sie w oczy z odleglosci.

    Szarosc ('m') jest zarezerwowana dla rzeczy CELOWO wyciszonych: archiwum,
    lokalne z wyboru, obce. Dawno nieodpytywane repo dostaje wlasny kolor ('s'),
    bo "nie wiem, jaki masz stan" to nie to samo co "nie interesujesz mnie".
    """
    if r.mode == "archive" or r.label in (LOCAL_ONLY, FOREIGN):
        return "m"
    if r.error or r.ahead or (not r.remote and not r.label) or "ROZJAZD" in r.verdict:
        return "a"
    if r.dirty or r.behind:
        return "w"
    return "s" if stale(r) else "g"


def as_table(repos: list[Repo], token: str) -> str:
    rows = "".join(
        f"<tr><td><div class=nm>{esc(r.name)}</div>"
        f"<div class=pth>{esc(r.path)}</div></td>"
        f"<td class=pth>{esc(r.branch)}</td>"
        f"<td>{tags_for(r)}</td>"
        f"<td class=pth>{esc(age(r.fetched_at))}</td>"
        f"<td>{buttons_for(r, token)}</td></tr>" for r in repos)
    if not rows:
        rows = "<tr><td colspan=5 class=pth>nic w tym filtrze</td></tr>"
    return ("<table><tr><th>repozytorium</th><th>galaz</th><th>stan</th>"
            f"<th>fetch</th><th>akcje</th></tr>{rows}</table>")


def as_tiles(repos: list[Repo], token: str) -> str:
    if not repos:
        return "<p class=pth>nic w tym filtrze</p>"
    cards = "".join(
        f"<div class='card {card_class(r)}'>"
        f"<h3>{esc(r.name)}</h3>"
        f"<div class=br>{esc(r.branch)} &nbsp;·&nbsp; fetch {esc(age(r.fetched_at))}</div>"
        f"<div class=tg>{tags_for(r)}</div>"
        f"<div class=pth style='margin-bottom:9px'>{esc(r.path)}</div>"
        f"<div class=ac>{buttons_for(r, token)}</div></div>" for r in repos)
    return f"<div class=grid>{cards}</div>"


def as_groups(repos: list[Repo], token: str) -> str:
    """Grupowanie po zdalnym. Kopie robocze tego samego repo trafiaja obok
    siebie - a o to w tym narzedziu chodzi od poczatku."""
    groups: dict[str, list[Repo]] = {}
    solo: list[Repo] = []
    for r in repos:
        (groups.setdefault(r.remote_key, []) if r.remote_key else solo).append(r)

    multi = {k: v for k, v in groups.items() if len(v) > 1}
    single = [v[0] for k, v in groups.items() if len(v) == 1]

    out = []
    for key, members in sorted(multi.items()):
        members.sort(key=lambda r: (r.mode == "archive", norm_key(r.path)))
        split = any("ROZJAZD" in m.verdict for m in members)
        media = ", ".join(dict.fromkeys(m.medium or "?" for m in members))
        out.append(
            f"<div class='grp {'split' if split else ''}'>"
            f"<h2>{esc(key)}<small>{len(members)} kopii roboczych: "
            f"{esc(media)}</small></h2>{as_table(members, token)}</div>")

    rest = sorted(single + solo, key=lambda r: norm_key(r.path))
    if rest:
        out.append(f"<div class=grp><h2>pojedyncze<small>{len(rest)} repo, "
                   f"jedna kopia robocza</small></h2>{as_table(rest, token)}</div>")
    return "".join(out) or "<p class=pth>nic w tym filtrze</p>"


def render_page(rt: Runtime, flt: str, view: str = "lista") -> bytes:
    repos = [r for r in rt.repos if FILTERS.get(flt, FILTERS["wszystko"])(r)]
    repos.sort(key=lambda r: (r.mode == "archive", norm_key(r.root), norm_key(r.path)))

    nav = "".join(
        f"<a href='/?f={k}&w={view}' class='{'on' if k == flt else ''}'>{k}"
        f" <span style='opacity:.6'>{sum(1 for r in rt.repos if fn(r))}</span></a>"
        for k, fn in FILTERS.items())
    switch = "".join(
        f"<a href='/?f={flt}&w={v}' class='{'on' if v == view else ''}'>{v}</a>"
        for v in VIEWS)

    body = {"kafelki": as_tiles, "grupy": as_groups}.get(view, as_table)(repos, rt.token)

    flash = ""
    if rt.flash:
        kind, text = rt.flash
        flash = f"<p class='bar {kind}'>{esc(text)}</p>"
        rt.flash = None

    dirty = sum(1 for r in rt.repos if r.dirty and r.mode == "rw")
    unp = sum(1 for r in rt.repos if r.ahead and r.mode == "rw")
    nor = sum(1 for r in rt.repos if not r.remote and not r.label and r.mode == "rw")
    tw = len(find_twins(rt.repos))
    st = sum(1 for r in rt.repos if r.mode == "rw" and stale(r))

    bulk = (f"<form method=post action=/akcja>"
            f"<input type=hidden name=t value='{esc(rt.token)}'>"
            f"<input type=hidden name=a value='fetch_all'>"
            f"<button>odswiez wszystkie ({st} nieswiezych)</button></form>"
            f"<form method=post action=/akcja>"
            f"<input type=hidden name=t value='{esc(rt.token)}'>"
            f"<input type=hidden name=a value='rescan'>"
            f"<button class=d>przeskanuj dysk</button></form>")

    return page("gitdesk", f"""
<header><h1>gitdesk</h1>
<span class=sum>{len(rt.repos)} repo &nbsp;·&nbsp; {dirty} brudnych &nbsp;·&nbsp;
{unp} niewypchnietych &nbsp;·&nbsp; {nor} bez zdalnego do decyzji &nbsp;·&nbsp;
{tw} grup blizniakow</span>
<span style='margin-left:auto'>{bulk}</span></header>
<nav>{nav}<span style='margin-left:auto;display:flex;gap:6px'>{switch}</span></nav>
<main>{flash}{body}
<p class=hint>Werdykt „zgodne" wymaga fetcha mlodszego niz doba — inaczej
„niezweryfikowane". Pull zawsze --ff-only. Push zablokowany, gdy repo jest
publiczne, a skan znajduje sekret.</p>
</main>""")


class Handler(BaseHTTPRequestHandler):
    server_version = "gitdesk"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    rt: Runtime = None          # podstawiane przez serve()

    def _send(self, body: bytes, code=200, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            q = parse_qs(u.query)
            flt = q.get("f", ["wszystko"])[0]
            view = q.get("w", ["lista"])[0]
            if view not in VIEWS:
                view = "lista"
            self.rt.view, self.rt.filter = view, flt
            return self._send(render_page(self.rt, flt, view))
        if u.path == "/json":
            return self._send(
                json.dumps([asdict(r) for r in self.rt.repos],
                           ensure_ascii=False).encode("utf-8"),
                ctype="application/json; charset=utf-8")
        self._send(page("404", "<main><p>Nie ma takiej strony.</p></main>"), code=404)

    def do_POST(self):
        if urlparse(self.path).path != "/akcja":
            return self._send(page("404", "<main>404</main>"), code=404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        token = (form.get("t") or [""])[0]

        # Token sesji: bez niego dowolna strona otwarta w tej samej przegladarce
        # moglaby wyslac POST-a i wysterowac panel.
        if not _secrets.compare_digest(token, self.rt.token):
            self.rt.flash = ("err", "zly token sesji - odswiez strone")
            return self._redirect()

        action = (form.get("a") or [""])[0]
        path = (form.get("p") or [""])[0]
        msg = (form.get("m") or [""])[0]

        if action == "rescan":
            self.rt.repos = scan(self.rt.conf, do_fetch=False,
                                 do_vis=bool(self.rt.conf.get("owner")))
            self.rt.flash = ("done", f"przeskanowane: {len(self.rt.repos)} repo")
            return self._redirect()

        if action == "fetch_all":
            ok, fail = fetch_all(self.rt.repos)
            self.rt.repos = scan(self.rt.conf, do_fetch=False, do_vis=False)
            self.rt.flash = ("done" if not fail else "err",
                             f"odswiezone: {ok} ok, {fail} nieudanych")
            return self._redirect()

        good, note = act(self.rt, action, path, msg)
        name = Path(path).name if path else "?"
        self.rt.flash = ("done" if good else "err", f"{name}: {note}")
        # stan repo po akcji jest inny - przeliczamy, zeby tabela nie klamala
        self.rt.repos = scan(self.rt.conf, do_fetch=False, do_vis=False)
        self._redirect()

    def _redirect(self):
        self.send_response(303)
        self.send_header("Location", f"/?f={self.rt.filter}&w={self.rt.view}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def free_port(preferred: int) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", preferred))
        s.close()
        return preferred
    except OSError:
        s.close()
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.bind(("127.0.0.1", 0))
        p = s2.getsockname()[1]
        s2.close()
        return p


def tailnet_ip() -> str | None:
    """Adres tego hosta w Tailscale, jesli klient chodzi."""
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            addr = out.stdout.strip().splitlines()
            if addr:
                return addr[0].strip()
    return None


def serve(conf: dict, doctor, repos: list[Repo], bind: str = "local",
          open_browser: bool = True) -> int:
    # Domyslnie WYLACZNIE petla zwrotna. Ten panel wykonuje commit, reset, push
    # i pull na kilkudziesieciu repo, uzywajac poswiadczen, ktore maszyna juz ma
    # - nie ma tu tokenu do wykradzenia, bo zaden nie jest potrzebny. Kto dojdzie
    # do panelu, pushuje jako wlasciciel konta. Dlatego zadnego tunelu; jesli
    # potrzebny jest dostep z telefonu, wlasciwa odpowiedzia jest siec prywatna,
    # gdzie uwierzytelnieniem jest tozsamosc WireGuarda.
    host = "127.0.0.1"
    if bind == "tailnet":
        ip = tailnet_ip()
        if not ip:
            print("Nie znalazlem adresu Tailscale - zostaje 127.0.0.1.", file=sys.stderr)
        else:
            host = ip
            print(f"UWAGA: panel widoczny dla CALEGO tailnetu ({ip}). Kazde "
                  f"urzadzenie w tej sieci moze pushowac Twoimi poswiadczeniami.",
                  file=sys.stderr)
    port = free_port(int(conf.get("port", 7420)))
    Handler.rt = Runtime(conf, doctor, repos)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"gitdesk: {url}   (Ctrl+C konczy)", file=sys.stderr)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nzakonczone.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def selftest() -> int:
    """Sprawdza rzeczy, ktorych awaria bylaby CICHA.

    Blokada commita z sekretem nie krzyczy, kiedy przestaje dzialac - po prostu
    przepuszcza. To jedyny element tego narzedzia, ktorego zepsucie zauwazyloby
    sie dopiero po wycieku, wiec ma wlasny test.
    """
    import shutil
    import tempfile

    conf = config_load()
    doctor = load_doctor(conf)
    ok = fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  {C.GREEN}OK{C.OFF}   {name}")
        else:
            fail += 1
            print(f"  {C.RED}BLAD{C.OFF} {name}" + (f"  — {detail}" if detail else ""))

    tmp = Path(tempfile.mkdtemp(prefix="gitdesk-test-"))
    try:
        def g(repo: Path, *a: str) -> str | None:
            return _run(str(repo), *a)

        # ── zdalne "na niby" plus dwie kopie robocze ──────────────────────
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        src = tmp / "pc"
        subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True)
        g(src, "config", "user.email", "t@t"); g(src, "config", "user.name", "t")
        # Globalny gitignore tej maszyny ma '.env' - bez wyzerowania go test
        # blokady sekretow przechodzilby ZA DARMO: plik nigdy nie trafilby do
        # indeksu, wiec skan nie mialby czego zlapac, a wynik wygladalby na
        # sukces. Test ma sprawdzac gitdeska, nie konfiguracje komputera.
        g(src, "config", "core.excludesFile", "")
        (src / "a.txt").write_text("1", encoding="utf-8")
        g(src, "add", "-A"); g(src, "commit", "-qm", "start")
        g(src, "push", "-q", "-u", "origin", "HEAD")
        pen = tmp / "pendrive"
        subprocess.run(["git", "clone", "-q", str(origin), str(pen)], check=True)
        g(pen, "config", "user.email", "t@t"); g(pen, "config", "user.name", "t")

        labels: dict[str, str] = {}
        both = [{"path": str(src), "root": str(tmp), "mode": "rw", "medium": "PC"},
                {"path": str(pen), "root": str(tmp), "mode": "rw", "medium": "pendrive"}]

        repos = probe_all(both, labels)
        find_twins(repos)
        check("dwie kopie tego samego zdalnego to blizniaki",
              all(r.twin_of for r in repos))
        check("zgodne kopie daja werdykt 'zsynchronizowane'",
              all("zsynchronizowane" in r.verdict for r in repos),
              "; ".join(r.verdict for r in repos))

        # ── commit tylko na PC: werdykt ma wskazac wlasciwa kopie ─────────
        (src / "b.txt").write_text("2", encoding="utf-8")
        g(src, "add", "-A"); g(src, "commit", "-qm", "tylko na PC")
        repos = probe_all(both, labels)
        find_twins(repos)
        pc = next(r for r in repos if r.medium == "PC")
        pd = next(r for r in repos if r.medium == "pendrive")
        check("kopia z commitem jest 'z przodu'", "z przodu o 1" in pc.verdict, pc.verdict)
        check("werdykt nazywa nosnik drugiej kopii", "pendrive" in pc.verdict, pc.verdict)
        check("druga kopia nie zglasza sie jako wyprzedzajaca",
              "z przodu" not in pd.verdict, pd.verdict)

        # ── blokada commita z sekretem ────────────────────────────────────
        rt = Runtime(conf, doctor, repos)
        (src / ".env").write_text("OPENAI_API_KEY=sk-QhTvN82wKpLmXr4dYbFj910AcEuZsRoI\n",
                                  encoding="utf-8")
        good, note = act(rt, "commit", str(src), "probuje przemycic sekret")
        check("commit z sekretem ODRZUCONY", not good, note)
        check("odrzucenie nazywa plik", ".env" in note, note)
        check("indeks wyczyszczony po odrzuceniu",
              not (_run(str(src), "diff", "--cached", "--name-only") or "").strip())
        (src / ".env").unlink()

        # ── zwykly commit ma przejsc ──────────────────────────────────────
        (src / "c.txt").write_text("3", encoding="utf-8")
        rt.repos = probe_all(both, labels)
        good, note = act(rt, "commit", str(src), "zwykla zmiana")
        check("commit bez sekretu przechodzi", good, note)

        # ── korzen archiwalny odrzuca zapisy ──────────────────────────────
        arch = [{"path": str(pen), "root": str(tmp), "mode": "archive",
                 "medium": "archiwum"}]
        rt.repos = probe_all(arch, labels)
        good, note = act(rt, "push", str(pen))
        check("korzen archiwalny odrzuca push", not good, note)

        # ── repo obce: bez akcji zapisujacych ─────────────────────────────
        rt.repos = probe_all(both, {norm_key(src): FOREIGN})
        good, note = act(rt, "push", str(src))
        check("repo 'foreign' odrzuca push", not good, note)
        good, _ = act(rt, "fetch", str(src))
        check("repo 'foreign' dopuszcza fetch", good)

        # ── swiezosc: bez fetcha nie ma werdyktu 'zsynchronizowane' ───────
        rt.repos = probe_all(both, labels)
        for r in rt.repos:
            r.ahead = r.behind = 0
            r.fetched_at = int(time.time()) - 5 * 86400
        find_twins(rt.repos)
        check("stary fetch blokuje werdykt 'zsynchronizowane'",
              all("zsynchronizowane" not in r.verdict for r in rt.repos),
              "; ".join(r.verdict for r in rt.repos))
    finally:
        # Windows trzyma pliki .git na tylko-do-odczytu
        def unlock(func, path, _):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(tmp, onexc=unlock)

    print(f"\n  {ok} przeszlo, {fail} nie przeszlo\n")
    return 1 if fail else 0


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
    ap.add_argument("--bind", choices=["local", "tailnet"], default="local",
                    help="local (domyslnie, tylko ten komputer) albo tailnet "
                         "(widoczne dla wszystkich Twoich urzadzen w Tailscale). "
                         "Przez tunel publiczny NIE wystawiac - panel pushuje "
                         "Twoimi poswiadczeniami.")
    ap.add_argument("--selftest", action="store_true",
                    help="sprawdza blizniaki, blokade sekretow i korzen archiwalny")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

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
        return serve(conf, doctor, repos, bind=args.bind)
    else:
        render_list(repos, conf)
        render_deployments(check_deployments(conf, doctor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
