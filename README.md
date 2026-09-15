# Helios - integracja Home Assistant dla Lenovo Smart Clock 2

Integracja dla zegara z aplikacją [Helios](https://github.com/OWNER/helios) (Lenovo Smart Clock 2 jako dashboard i satelita głosowy Home Assistant). Zegar łączy się z HA własnym gniazdem WebSocket; ta integracja rejestruje urządzenie i encje po stronie HA. Bez zależności pip, wyłącznie push (`iot_class: local_push`).

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

Urządzenie w rejestrze HA ma stały identyfikator instalacji Heliosa, więc obszar przypisany w HA daje kontekst pokoju dla poleceń głosowych (`device_id` w `assist_pipeline/run`).

## Instalacja

1. HACS → Integracje → menu ⋮ → **Niestandardowe repozytoria** → dodaj URL tego repozytorium, kategoria *Integracja*.
2. Zainstaluj **Helios** i zrestartuj Home Assistant.
3. Ustawienia → Urządzenia i usługi → **Dodaj integrację** → Helios. HA pokaże 6-cyfrowy kod ważny 5 minut.
4. Na zegarze (Helios 0.7.0 lub nowszy) przytrzymaj HELIOS → **Paruj z HA (kod)** → wpisz kod → OK.
5. Przypisz urządzenie do obszaru.

Ponowne parowanie tego samego zegara odświeża istniejący wpis. Usunięcie integracji kończy kanał: zegar przestaje przekazywać `device_id` i nie kontynuuje starych rozmów.

## Zachowanie i bezpieczeństwo

- Encje są niedostępne, dopóki zegar nie prześle pierwszego snapshotu; rozłączenie gniazda oznacza je jako `unavailable` bez dodatkowych heartbeatów.
- Polecenia (`lamp.turn_on`, `lamp.turn_off`, `lamp.set_brightness`, `audio.set_device_volume`) to zamknięta lista; HA czeka na potwierdzenie do 10 s i nigdy nie ponawia.
- Kanał przyjmuje tylko połączenia użytkownika HA, z którym zegar został sparowany. Użyj dla zegara dedykowanego konta bez uprawnień administratora.

## Rozwój

`python -m pytest tests` uruchamia testy czystych helperów (mapowanie jasności, walidacja komend, kody parowania) bez Home Assistant. Workflow GitHub uruchamia hassfest, walidację HACS i te testy.

Protokół kanału (`helios/connect`, `helios/state`, `helios/result`) opisuje specyfikacja w repozytorium aplikacji: `docs/SPEC-0.7-home-assistant-integration.md`.
