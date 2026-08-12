# gitdesk

Panel nad **wszystkimi** repozytoriami na maszynie — nie nad jednym naraz.

GitHub Desktop nie umie przeskanowac dysku (prosby otwarte od 2017: [#1574](https://github.com/desktop/desktop/issues/1574), [#19662](https://github.com/desktop/desktop/issues/19662)),
wiec kazde repo dodaje sie recznie. Ale brak skanowania to nie jest prawdziwy
problem. Prawdziwy problem to pytania, ktorych zaden klient nie zadaje, bo widzi
tylko jedno repo:

- gdzie nie ma commita, a gdzie commit jest, ale nie ma pusha
- co jest publiczne, a co prywatne
- **ktora kopia robocza jest z przodu**, gdy to samo repo lezy na PC i na pendrivie
- gdzie sekret jest o jeden `git add -A` od wyciekniecia
- co niesie kopia wdrozeniowa bez `.git`, ktorej zaden klient nie widzi

## Uzycie

```
python gitdesk.py list            # raport (dane z cache remote'ow)
python gitdesk.py list --fetch    # najpierw odswiez remote'y - wolniej, ale prawdziwie
python gitdesk.py twins           # tylko grupy blizniakow
python gitdesk.py scan            # przeskanuj i zapisz cache
python gitdesk.py list --json     # do skryptow
```

Pierwsze uruchomienie tworzy `config.json` z domyslnymi ustawieniami.

## Swiezosc danych, czyli dlaczego `--fetch`

`git status` liczy ahead/behind wzgledem `origin/...` zapisanego lokalnie przy
**ostatnim fetchu**. Repo, ktorego nie fetchowano od 111 dni, moze byc 20
commitow z tylu, a status pokaze zero i bedzie formalnie poprawny.

Dlatego werdykt „zgodne" pojawia sie tylko przy swiezym fetchu (< 24 h). Bez
niego jest `niezweryfikowane`. Rozjazd i wyprzedzenie pokazuja sie zawsze —
lokalny commit jest faktem niezaleznie od tego, kiedy ostatnio pytalismy zdalnego.

## Blizniaki

Kopie robocze wskazujace na ten sam remote. Werdykt liczony jest **bez siegania
miedzy repo**: kazda kopia ma wlasne `origin/<branch>`, wiec porownanie ahead/behind
kazdej z osobna wystarczy. `git merge-base A B` tu nie zadziala — to osobne bazy
obiektow, kopia z PC nie zna commitow z pendrive'a.

## Etykiety intencji

Bez nich narzedzie zglasza jako usterke kazde repo bez zdalnego — a wtedy uczy,
zeby ignorowac czerwone. W `config.json`:

- `local_only` — celowo bez remote'a, prywatne narzedzie. Widoczne na szaro,
  bez nagabywania o GitHuba.
- `foreign` — nie moj kod. Zero akcji zapisujacych.

## Wdrozenia

Kopie bez `.git` (np. dropgate na pendrivie). Dzialaja, ale cicho sie starzeja —
`git log` nie odpowie, bo nie ma czego pytac. `gitdesk` porownuje tresc plik po
pliku z HEAD repo zrodlowego i osobno zglasza, co takiego kopia niesie, czego w
repo nie ma: to wlasnie tam laduja klucze, ktore gitignore slusznie trzyma poza
repo, a ktore razem z nosnikiem wychodza z domu.

## Zaleznosci

Zadnych zewnetrznych — sama biblioteka standardowa (Python 3.14).

Skan sekretow nie jest tu pisany od nowa: `gitdesk` laduje
[`workspace-doctor`](../workspace-doctor) jako modul i uzywa jego wzorcow oraz
`looks_synthetic()`. **Brak doktora to twardy blad startu**, nie ciche pominiecie
— narzedzie, ktore po cichu wylacza swoj failsafe, jest gorsze niz jego brak.

## Stan

Fazy 1–2 gotowe (odkrywanie, stan repo, fetch, widocznosc, blizniaki, wdrozenia).
Fazy 3–6 przed nami: panel w przegladarce, akcje, selftest, graf historii.
