# Freifunk Expansion Map

Diese Karte zeigt Verwaltungsgebiete, in denen Freifunk-Knoten stehen. Die neue Version kann **mehrere Hopglass-/Meshviewer-JSON-Feeds** (sowie die historischen `ffmap`- und `nodelist`-Formate) zusammenführen und daraus `nodes.geojson` erzeugen.

## Voraussetzungen

- Python 3.11 oder neuer
- ein Webserver für die statischen Dateien (`python -m http.server` genügt)
- Internetzugriff auf die konfigurierten Node-Feeds und auf MapIt

Das Python-Programm hat keine externen Laufzeitabhängigkeiten.

## Schnellstart

Konfiguration anlegen:

```bash
cp sources.example.json sources.json
$EDITOR sources.json
```

Beispiel:

```json
{
  "sources": [
    "https://community-a.example/meshviewer.json",
    {
      "name": "community-b",
      "url": "https://community-b.example/nodes.json",
      "format": "hopglass"
    }
  ]
}
```

Kartendaten erzeugen:

```bash
./mkpoly --config sources.json -v
```

Anschließend lokal anzeigen:

```bash
python -m http.server 8000
```

Dann `http://localhost:8000/` öffnen.

Alternativ können URLs direkt angegeben werden:

```bash
./mkpoly \
  https://community-a.example/meshviewer.json \
  https://community-b.example/nodes.json
```

Benannte Quellen sind mit wiederholtem `--source NAME=URL` möglich:

```bash
./mkpoly \
  --source rhein-neckar=https://example.org/meshviewer.json \
  --source nachbarn=https://example.net/nodes.json
```

## Unterstützte Eingabeformate

`auto` erkennt die Formate anhand der Knotenstruktur. Unterstützt werden:

- Meshviewer/Hopglass: `nodeinfo.location.latitude` und `nodeinfo.location.longitude`
- altes `ffmap`: `geo: [lat, lon]`
- altes `nodelist`: `position.lat` und `position.long`/`position.lng`

Die `nodes`-Sammlung darf sowohl ein JSON-Array als auch ein Objekt mit Node-ID als Schlüssel sein. Doppelte Node-IDs über mehrere Feeds werden nur einmal gezählt; bei widersprüchlichen Koordinaten gewinnt der zuerst konfigurierte Feed und es wird eine Warnung ausgegeben.

## Ausgabe

`nodes.geojson` bleibt eine normale GeoJSON `FeatureCollection`. Pro Verwaltungsgebiet werden u. a. diese Properties erzeugt:

```json
{
  "area_id": "12345",
  "area_type": "O08",
  "name": "Beispielstadt",
  "count": 17,
  "sources": {
    "community-a": 12,
    "community-b": 5
  }
}
```

Damit bleibt `count` kompatibel zur alten Kartenlogik, während `sources` die Herkunft der Knoten sichtbar macht.

## MapIt und Cache

Standardmäßig wird `https://global.mapit.mysociety.org/` verwendet. Gesucht wird in der Reihenfolge `O08`, `O07`, `O06`. Beides ist konfigurierbar:

```json
{
  "mapit_url": "https://mapit.example.org/",
  "area_types": ["O08", "O07", "O06"],
  "cache": ".cache/mapit.json",
  "request_delay": 1.0
}
```

Der persistente Cache ist wichtig: Ein Node-Feed kann viele Punkte enthalten, und die öffentliche MapIt-Instanz ist für niedrige Abfrageraten gedacht. Für häufige Generierung oder große Netze sollte eine eigene MapIt-Instanz genutzt werden. Den Cache kann man mit `--no-cache` abschalten.

## Weitere CLI-Optionen

```text
./mkpoly --help
```

Nützlich sind insbesondere `--output`, `--mapit-url`, `--area-types`, `--keep-going`, `--strict`, `--timeout` und `--request-delay`. Für den öffentlichen MapIt-Dienst gilt standardmäßig eine Pause von einer Sekunde zwischen Abfragen. Bei HTTP 403 oder 429 wartet der Client vor einem erneuten Versuch mindestens fünf Sekunden und berücksichtigt einen numerischen `Retry-After`-Header.

## Frontend

Das Frontend wurde auf Leaflet 1.9.4 und Browser-`fetch()` aktualisiert. jQuery wird nicht mehr benötigt. Die OSM-Kacheln kommen von `https://map03.4830.org/tiles_cache/osm_mapnik/{z}/{x}/{y}.png`; die Karte zoomt nach dem Laden automatisch auf die erzeugten Verwaltungsgebiete.

## Tests

```bash
python -m unittest discover -v
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
