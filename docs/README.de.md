# smart1 EMS für Home Assistant

[![CI](https://github.com/philipp-reede/ha-smart1/actions/workflows/ci.yml/badge.svg)](https://github.com/philipp-reede/ha-smart1/actions/workflows/ci.yml)
[![HACS validation](https://github.com/philipp-reede/ha-smart1/actions/workflows/validate.yml/badge.svg)](https://github.com/philipp-reede/ha-smart1/actions/workflows/validate.yml)
[![Neueste Version](https://img.shields.io/github/v/release/philipp-reede/ha-smart1?display_name=tag&sort=semver&label=Version)](https://github.com/philipp-reede/ha-smart1/releases/latest)
[![Lizenz](https://img.shields.io/badge/Lizenz-MIT-blue.svg)](https://github.com/philipp-reede/ha-smart1/blob/main/LICENSE)

<p align="center">
  <img src="https://raw.githubusercontent.com/philipp-reede/ha-smart1/main/custom_components/smart1_ems/brand/icon.png" width="128" alt="Logo der smart1 EMS Integration">
</p>

Eine inoffizielle, ausschließlich lesende Home-Assistant-Integration für das
smart1 Energiemanagementsystem. Sie ruft Messwerte über die CSV-API des smart1
Portals ab und bildet neben der Photovoltaikanlage auch Netzanschluss, Batterie,
Wallbox, Wärmepumpe und Zusatzheizung ab.

> **Inoffizielles Community-Projekt:** Diese Integration ist kein offizielles
> smart1 Release und weder mit smart1 verbunden noch von smart1 freigegeben. Die
> Implementierung basiert auf der
> [offiziellen Dokumentation der smart1 CSV-Portal-API](https://data.smart1.eu/s/W3M4E8EkqMAPWqL?dir=/01%20SOFTWARE%20%26%20FIRMWARE/02%20PORTAL&editing=false&openfile=true).

> **Entwicklungshinweis:** Die gesamte Integration wurde in einem
> KI-gestützten Vibe-Coding-Prozess mit OpenAI Codex erstellt. Änderungen werden
> durch automatisierte Tests und an einer realen smart1 Anlage geprüft.

> Das Projekt ist eine frühe öffentliche Beta-Version. Es wurde wiederholt mit
> einer realen smart1 Anlage sowie Home Assistant Core 2026.7.4 und 2026.8.3
> geprüft.

[English documentation](../README.md) ·
[Installation](#installation-über-hacs) ·
[Screenshots](#screenshots) ·
[Energy Dashboard](#energy-dashboard) ·
[Fehler melden](#fehler-melden) ·
[Roadmap](#roadmap)

## Auf einen Blick

| | |
| --- | --- |
| Zugriff | Ausschließlich lesendes Cloud-Polling über die offizielle CSV-Portal-API |
| Einrichtung | Home-Assistant-Oberfläche mit verdecktem persönlichem API-Schlüssel |
| Geräte | EMS, PV, Wechselrichter, Netz, Batterie, Wallbox, Wärmepumpe und Zusatzheizung |
| Energy Dashboard | PV, Netz, Batterie und ausgewählte Einzelverbraucher |
| Historie | Automatischer Import von bis zu 365 Tagen |
| Getestete Hardware | M-TEC Energy Hero, Energy Butler, Energy Heater und KEBA Wallbox |

## Funktionen

- Automatische Erkennung von Anlage und Messpunkten
- Live-Sensoren für Leistung, Energie, Temperaturen, Prozentwerte und Diagnosen
- Optionale physische Wechselrichter-Geräte aus dem dokumentierten
  Wechselrichter-Endpunkt mit AC-/DC-Leistung und DC-Spannung je PV-String sowie
  Wechselrichtertemperatur als Diagnosesensoren
- Optionale Modulfeld-Diagnosen für installierte Leistung, Ausrichtung, Neigung
  und konfigurierte Verschattungszeiträume
- Optionale statische Diagnosen für aktive Wechselrichter-Bussysteme und ihre
  dokumentierten Herstellerprotokolle
- Ladezustand der Batterie aus dem strukturierten smart1 `SOC`-Signal
- Exakte PV-Produktion aus dem dokumentierten kumulativen PV-Endpunkt
- Energy-Dashboard-Statistiken für Netz, Batterie, Wallbox, Wärmepumpe und
  Zusatzheizung
- Automatischer historischer Import im Hintergrund
- Redigierte Home-Assistant-Diagnosen ohne API-Schlüssel, Anlagen-IDs,
  Messpunkt-IDs oder Messwerte

Alle Zugriffe auf das Portal sind lesend. Die Integration stellt keine Schalter,
Befehle oder andere Steuerungsmöglichkeiten bereit.

## Screenshots

**Einrichtung mit verdecktem API-Schlüssel**

<p align="center">
  <img src="https://raw.githubusercontent.com/philipp-reede/ha-smart1/main/docs/images/setup-api-key.jpg" width="720" alt="Einrichtung von smart1 EMS mit verdecktem API-Schlüssel">
</p>

**Automatisch erkannte Geräte**

<p align="center">
  <img src="https://raw.githubusercontent.com/philipp-reede/ha-smart1/main/docs/images/device-overview.jpg" width="900" alt="Home-Assistant-Geräteübersicht für smart1 EMS">
</p>

<details>
  <summary><strong>Batteriekonfiguration für das Energy Dashboard</strong></summary>
  <p align="center">
    <img src="https://raw.githubusercontent.com/philipp-reede/ha-smart1/main/docs/images/energy-battery.jpg" width="500" alt="Konfiguration von Batterieladung, Entladung, Leistung und Ladezustand">
  </p>
</details>

## Kompatibilität

Die Integration verwendet die gemeinsame, offiziell dokumentierte CSV-API der
smart1 Portale. Sie sollte deshalb mit allen smart1 Portalen funktionieren, die
die dokumentierten Endpunkte und kompatible Messpunkt-Metadaten bereitstellen.
Praktisch getestet werden konnte sie bisher nur mit einer Anlage:

| Komponente | Getestete Hardware | Getestete Versionen | Validierung | Nachweis |
| --- | --- | --- | --- | --- |
| EMS und Portal | M-TEC Energy Hero PV V2 EMS | Hardware `RevA.2024`, Software `1.28.57` und `1.28.59` | Praxistest erfolgreich | [#13](https://github.com/philipp-reede/ha-smart1/issues/13) |
| Wechselrichter und Speicher | M-TEC Energy Butler 20 kW 3G40 | Firmware `V04.02.00.02-V23.54.05.00` | Praxistest erfolgreich | [#13](https://github.com/philipp-reede/ha-smart1/issues/13) |
| Zusatzheizung | M-TEC Energy Heater 9 kW 3P 1G | Hardware `V12.12`, Firmware `V47.22.18.24` | Praxistest erfolgreich | [#13](https://github.com/philipp-reede/ha-smart1/issues/13) |
| Wallbox | KEBA P40 M-TEC | Software `1.4.5` | Praxistest erfolgreich | [#13](https://github.com/philipp-reede/ha-smart1/issues/13) |
| Wärmepumpe | M-TEC AP440 | Nicht angegeben | Praxistest erfolgreich | [#13](https://github.com/philipp-reede/ha-smart1/issues/13) |

Diese Angaben dokumentieren bestätigte Testkombinationen und keine
Mindestversionen. Bericht
[#13](https://github.com/philipp-reede/ha-smart1/issues/13) wurde mit smart1 EMS
`v0.6.1`, Home Assistant Core `2026.7.4` und `portal.smart1.eu` erstellt. Die
gleiche Anlage wurde anschließend mit Energy Hero Software `1.28.59` und Home
Assistant Core `2026.8.3` erneut erfolgreich getestet.

Aktive Wechselrichter-Buskonfigurationen, Anlagen mit mehreren Wechselrichtern
und andere smart1 Portale oder Hardwarekombinationen konnten noch nicht an
einer realen Anlage bestätigt werden.

Andere Anlagen und Hardwarekombinationen können abweichende Messpunktnamen oder
Schnittstellen-Metadaten verwenden. Diagnosen solcher Systeme helfen dabei, die
Erkennungsregeln zu erweitern. Erfolgreiche oder teilweise erfolgreiche Tests
können über den
[Kompatibilitätsbericht](https://github.com/philipp-reede/ha-smart1/issues/new?template=compatibility_report.yml)
gemeldet werden.

## Installation über HACS

Bis die Integration im Standardkatalog von HACS enthalten ist, wird sie als
benutzerdefiniertes Repository hinzugefügt:

[![Home Assistant öffnen und dieses Repository in HACS anzeigen](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=philipp-reede&repository=ha-smart1&category=integration)

Alternativ kann das Repository manuell hinzugefügt werden:

1. HACS in Home Assistant öffnen.
2. Im Drei-Punkte-Menü **Benutzerdefinierte Repositories** auswählen.
3. `https://github.com/philipp-reede/ha-smart1` als Kategorie **Integration**
   hinzufügen.
4. **smart1 EMS** suchen und herunterladen.
5. Home Assistant neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste** öffnen.
2. **Integration hinzufügen** wählen und nach **smart1 EMS** suchen.
3. Den persönlichen API-Schlüssel aus dem smart1 Portal eingeben. Das Feld ist
   verdeckt; der Schlüssel wird im Home-Assistant-Konfigurationseintrag
   gespeichert.
4. Falls mehrere Anlagen verfügbar sind, die gewünschte Anlage auswählen.

Die Live-Entitäten stehen anschließend zur Verfügung. Der historische Import
startet automatisch im Hintergrund und wird auch nach späteren Neustarts
ausgeführt. Der erste Import kann abhängig von der Antwortzeit des Portals
mehrere Minuten dauern.

## Energy Dashboard

Unter **Einstellungen → Geräte & Dienste → smart1 EMS → Konfigurieren** wird für
jede gewünschte Energierolle ein Leistungsmesspunkt ausgewählt. Die Integration
schlägt erkannte Messpunkte vor. Die Auswahl bleibt bewusst explizit, weil eine
smart1 Anlage mehrere Messpfade für dasselbe physische Gerät bereitstellen kann.

Danach können unter **Einstellungen → Dashboards → Energie** folgende
Statistiken hinzugefügt werden:

| Bereich | smart1 EMS Statistik |
| --- | --- |
| PV-Erzeugung | smart1 EMS PV production |
| Netzbezug | smart1 EMS grid import |
| Netzeinspeisung | smart1 EMS grid export |
| Batterieladung | smart1 EMS battery charge |
| Batterieentladung | smart1 EMS battery discharge |
| Einzelgeräte | Wallbox, Wärmepumpe und Zusatzheizung |

Die PV-Gesamtsumme stammt aus dem dokumentierten kumulativen Endpunkt und wird
anhand des gemessenen 5-Minuten-Leistungsprofils auf Stunden verteilt. Dadurch
bleibt die exakte Tagessumme erhalten und die stündliche Energieverteilung ist
zeitlich konsistent.

Die übrigen Energiewerte sind Schätzungen aus 5-Minuten-Leistungswerten. Lücken
von mehr als 15 Minuten werden nicht überbrückt. Unvollständige Portaldaten
können deshalb zu niedrigeren Summen führen.

## Fehler melden

Reproduzierbare Probleme bitte über das strukturierte
[Fehlerformular](https://github.com/philipp-reede/ha-smart1/issues/new?template=bug_report.yml)
melden. Erfolgreiche Tests mit anderen Portalen oder Hardwarekombinationen
können über den
[Kompatibilitätsbericht](https://github.com/philipp-reede/ha-smart1/issues/new?template=compatibility_report.yml)
geteilt werden. Niemals API-Schlüssel oder andere Zugangsdaten veröffentlichen.
Protokolle und Diagnosen sollten vor dem Anhängen geprüft werden, auch wenn die
Integrationsdiagnose Zugangsdaten, Kennungen und Messwerte gezielt entfernt.

## Bekannte Einschränkungen

- Die Verfügbarkeit der Daten hängt vom smart1 CSV-Portal ab.
- Wechselrichterdiagnosen benötigen die optionalen Endpunkte für
  Wechselrichter-Metadaten und detaillierte Photovoltaikdaten. Stellt ein Portal
  diese nicht bereit, funktionieren alle übrigen Geräte und Sensoren weiter.
- Die Modulfeld-Konfiguration benötigt den optionalen dokumentierten
  Modulfeld-Endpunkt und wird ausgelassen, wenn das Portal ihn nicht bereitstellt.
- Die Wechselrichter-Buskonfiguration benötigt den optionalen dokumentierten
  Bus-Endpunkt. Leere Busplätze und Portale ohne diesen Endpunkt erzeugen keine
  Entitäten.
- Historische Energiewerte außerhalb der PV-Anlage werden aus
  5-Minuten-Leistungswerten berechnet, weil die getestete Anlage keine
  verwendbaren linearen Summenzähler über den kumulativen Endpunkt liefert.
- Ungewöhnliche Messpunktnamen oder Schnittstellen-Metadaten können zusätzliche
  Zuordnungsregeln erfordern.
- Die Integration ist ausschließlich lesend und kann weder das EMS noch
  angeschlossene Geräte steuern.

## Roadmap

- Erkennung und Gerätezuordnung mit weiteren smart1 Portalen und
  Hardwarekombinationen validieren
- Die neuen Wechselrichter- und PV-String-Diagnosen mit weiteren
  Wechselrichtermodellen und Anlagen mit mehreren Wechselrichtern prüfen
- Modulfeld-Zuordnungen und Ausrichtungsdaten mit weiteren Dachaufteilungen
  prüfen
- Wechselrichter-Buskonfiguration und Herstellerprotokolle mit weiteren EMS-
  und Wechselrichterkombinationen prüfen
- Den Zeitraum des historischen Imports bei Bedarf konfigurierbar machen
- Die Aufnahme in den HACS-Standardkatalog abschließen und Rückmeldungen aus dem
  Review bearbeiten

## Projekt unterstützen

Wenn dir die Integration hilft, kannst du ihre Weiterentwicklung hier
unterstützen:

[![Buy me a beer](https://raw.githubusercontent.com/philipp-reede/ha-smart1/main/docs/images/buy-me-a-beer.svg)](https://www.buymeacoffee.com/philipp_reede)

Weitere technische Details stehen in den englischen
[`API_NOTES.md`](../API_NOTES.md).

## Lizenz

Dieses Projekt steht unter der [MIT-Lizenz](../LICENSE).
