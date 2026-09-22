# Helios - integracja Home Assistant dla Lenovo Smart Clock 2

Integracja dla zegara z aplikacją [Helios](https://github.com/SychPL/helios) (Lenovo Smart Clock 2 jako dashboard i satelita głosowy Home Assistant). Zegar łączy się z HA własnym gniazdem WebSocket; ta integracja rejestruje urządzenie i encje po stronie HA. Bez zależności pip, wyłącznie push (`iot_class: local_push`).

## Co daje

| Encja | Źródło |
| --- | --- |
| `sensor` Wersja aplikacji (diagnostyczny) | wersja APK Heliosa |
| `sensor` Stan głosu | idle / listening / processing / responding / error |
| `sensor` Wersja docka (diagnostyczny) | firmware docka ładującego |
| `binary_sensor` Dock podłączony | listener OEM docka |
| `binary_sensor` Ładowanie telefonu | listener ładowania Qi; niedostępny bez docka |
| `light` Lampka docka | on/off z odczytu OEM, jasność 1-10 jako nastawa |
| `number` Głośność urządzenia | `STREAM_MUSIC` 0-100 %, odczyt co 3 s |
| `media_player` Głośnik zegara | ta sama głośność urządzenia jako odtwarzacz (tylko `volume_set`/`volume_step`), żeby Assist rozumiał "głośniej"/"ustaw głośność" w obszarze zegara; muzykę obsługuje osobny odtwarzacz Music Assistant |

Urządzenie w rejestrze HA ma stały identyfikator instalacji Heliosa, więc obszar przypisany w HA daje kontekst pokoju dla poleceń głosowych (`device_id` w `assist_pipeline/run`).

## Instalacja

1. HACS → Integracje → menu ⋮ → **Niestandardowe repozytoria** → dodaj URL tego repozytorium, kategoria *Integracja*.
2. Zainstaluj **Helios** i zrestartuj Home Assistant.
3. Na zegarze (Helios 0.9.0 lub nowszy): przytrzymaj HELIOS → **Paruj z HA** → wybierz swój Home Assistant z listy (mDNS) albo wpisz adres.
4. Ustawienia → Urządzenia i usługi → **Dodaj integrację** → Helios. HA pokaże 6-cyfrowy kod ważny 5 minut; zostaw to okno otwarte.
5. Na zegarze dotknij **Dalej** i wpisz kod → OK. Zegar dostaje własny token HA (bez wklejania czegokolwiek), a jeśli w HA jest integracja Music Assistant, także dostęp do muzyki.
6. Przypisz urządzenie do obszaru.

Ponowne parowanie tego samego zegara odświeża istniejący wpis (nowy token, stary unieważniony). Usunięcie integracji usuwa użytkownika HA zegara i jego token Music Assistant; zegar przestaje przekazywać `device_id` i prosi o ponowne parowanie. Zegary z Heliosem 0.8.x (parowane przez WebSocket z tokenem administratora) działają dalej do czasu ponownego parowania kodem po aktualizacji aplikacji.

## Zachowanie i bezpieczeństwo

- Encje są niedostępne, dopóki zegar nie prześle pierwszego snapshotu; rozłączenie gniazda oznacza je jako `unavailable` bez dodatkowych heartbeatów.
- Polecenia (`lamp.turn_on`, `lamp.turn_off`, `lamp.set_brightness`, `audio.set_device_volume`) to zamknięta lista; HA czeka na potwierdzenie do 10 s i nigdy nie ponawia.
- Każdy zegar ma własnego użytkownika systemowego HA (bez uprawnień administratora, tylko z sieci lokalnej) i token ważny 10 lat, tworzone przy parowaniu i usuwane razem z wpisem. Kanał przyjmuje tylko połączenia tego użytkownika.
- Parowanie idzie przez nieuwierzytelniony `POST /api/helios/pair` w sieci lokalnej: kod 6 cyfr ważny 5 minut, po 5 błędnych próbach z jednego adresu ten adres jest blokowany na 5 minut, odpowiedzi błędów nie zdradzają niczego, kod i token nie trafiają do dziennika.
- Music Assistant: integracja bierze adres i token z wpisu core `music_assistant`, tworzy dla zegara osobny token MA (widoczny na liście tokenów w MA) i podaje adres Sendspin (`ws://<host MA>:8927/sendspin`, do nadpisania w opcjach). Bez MA w HA zegar działa bez muzyki.

## Rozwój

`pip install -r requirements_test.txt && python -m pytest tests` (Python 3.14, instaluje Home Assistant 2026.8.3 przez `pytest-homeassistant-custom-component`) uruchamia testy czystych helperów i testy komponentowe parowania, tożsamości i kanału. Workflow GitHub uruchamia hassfest, walidację HACS, te testy i kontrolę importu.

Protokół kanału (`helios/connect`, `helios/state`, `helios/result`) opisuje specyfikacja w repozytorium aplikacji: `docs/SPEC-0.7-home-assistant-integration.md`.

## Edytor dashboardu

Pozycja **Helios** w pasku bocznym (tylko administrator) to wizualny edytor układu zegara: siatka 4×3, strony, formularz na kartę. Kliknij pustą komórkę, aby dodać kartę; kliknij kartę, aby ją edytować lub usunąć. Wybór encji i ikon to formularze HA (`ha-form`); gdy się nie załadują, panel przechodzi na zwykłe pola z listą encji. Edytor sprawdza układ regułami i słowami zegara - to, co przyjmie, przyjmie też zegar.

Edytuje sekcję `helios` pulpitu w trybie storage (domyślnie `helios-clock`, inny z listy) poleceniami frontendu `lovelace/config` i `lovelace/config/save`; reszta pulpitu zostaje nietknięta. Przed zapisem czyta pulpit ponownie i odmawia nadpisania wersji zmienionej w międzyczasie. Dokument w schemacie 2-5 wczytuje się jako jedna strona i przy pierwszym zapisie przechodzi na schemat 6 - wymaga to Heliosa 0.12; starszy zegar odrzuci dokument i zachowa poprzedni układ. JavaScript jest publiczny (bez sekretów); jedyną drogą zapisu jest `lovelace/config/save`, które HA udostępnia tylko administratorom.

## Wygląd zegara (tło i motyw)

Ustawienia → Urządzenia i usługi → Helios → wybrany zegar → **Konfiguruj**:

- motyw: Ciepły grafit / Nocny błękit,
- tło: kolor motywu, zachowaj bieżące zdjęcie albo wgraj nowe (JPEG/PNG do 10 MB),
- przyciemnienie 35-80 % i punkt kadru,
- adres Sendspin (puste = wyliczony z adresu Music Assistant) i adres diagnostyki (puste = brak) - zegar dostaje je od razu przez subskrypcję.

Zdjęcie jest normalizowane w HA (orientacja EXIF, usunięcie metadanych, spłaszczenie przezroczystości, obwiednia 1600×960, JPEG ≤ 2 MB) i zapisane prywatnie w `config/helios/<entry_id>/`. Zegar pobiera je uwierzytelnionym GET `/api/helios/appearance/<entry_id>/<image_id>` (tylko właściciel parowania albo administrator). Zapis wyglądu nie restartuje integracji, muzyki ani zegara - zegar dostaje pełny snapshot `appearance` przez istniejącą subskrypcję.
