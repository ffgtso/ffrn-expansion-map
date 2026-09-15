'use strict';

const grades = [0, 5, 10, 15, 20, 30, 40, 50];

function getColor(value) {
  return value > 50 ? '#1b9e77' :
    value > 40 ? '#d95f02' :
    value > 30 ? '#7570b3' :
    value > 20 ? '#e7298a' :
    value > 15 ? '#66a61e' :
    value > 10 ? '#e6ab02' :
    value > 5 ? '#a6761d' :
    '#666666';
}

function featureStyle(feature) {
  return {
    fillColor: getColor(feature.properties.count),
    weight: 2,
    opacity: 1,
    color: '#fff',
    dashArray: '3',
    fillOpacity: 0.55,
  };
}

function sourceBreakdown(sources = {}) {
  return Object.entries(sources)
    .sort(([, left], [, right]) => right - left)
    .map(([name, count]) => `${name}: ${count}`)
    .join(' · ');
}

function setError(message) {
  const element = document.getElementById('error');
  element.textContent = message;
  element.hidden = false;
}

window.addEventListener('DOMContentLoaded', async () => {
  if (typeof L === 'undefined') {
    setError('Leaflet konnte nicht geladen werden.');
    return;
  }

  const map = L.map('map', { preferCanvas: true }).setView([49.47, 8.56], 9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap-Mitwirkende',
  }).addTo(map);

  const info = L.control();
  info.onAdd = function onAdd() {
    this._div = L.DomUtil.create('section', 'info');
    this.update();
    return this._div;
  };
  info.update = function update(properties) {
    this._div.replaceChildren();
    const heading = document.createElement('h1');
    heading.textContent = 'Freifunk Knoten-Einzugsgebiet';
    this._div.appendChild(heading);

    if (!properties) {
      const hint = document.createElement('p');
      hint.textContent = 'Über ein Gebiet fahren oder klicken, um Details zu sehen.';
      this._div.appendChild(hint);
      return;
    }

    const name = document.createElement('strong');
    name.textContent = properties.name;
    this._div.appendChild(name);

    const count = document.createElement('p');
    count.textContent = `${properties.count} Knoten`;
    this._div.appendChild(count);

    const sources = sourceBreakdown(properties.sources);
    if (sources) {
      const breakdown = document.createElement('small');
      breakdown.textContent = sources;
      this._div.appendChild(breakdown);
    }
  };
  info.addTo(map);

  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function onAdd() {
    const div = L.DomUtil.create('div', 'info legend');
    const title = document.createElement('strong');
    title.textContent = 'Knoten';
    div.appendChild(title);

    grades.forEach((grade, index) => {
      const row = document.createElement('div');
      row.className = 'legend-point';
      const swatch = document.createElement('i');
      swatch.style.background = getColor(grade + 1);
      const next = grades[index + 1];
      row.append(swatch, document.createTextNode(next ? `${grade}–${next}` : `${grade}+`));
      div.appendChild(row);
    });
    return div;
  };
  legend.addTo(map);

  try {
    const response = await fetch('nodes.geojson', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    let geojson;

    const onEachFeature = (feature, layer) => {
      layer.on({
        mouseover(event) {
          event.target.setStyle({ weight: 3, color: '#555', dashArray: '', fillOpacity: 0.75 });
          event.target.bringToFront();
          info.update(feature.properties);
        },
        mouseout(event) {
          geojson.resetStyle(event.target);
          info.update();
        },
        click(event) {
          map.fitBounds(event.target.getBounds(), { padding: [20, 20] });
          info.update(feature.properties);
        },
      });
    };

    geojson = L.geoJSON(data, { style: featureStyle, onEachFeature }).addTo(map);
    const bounds = geojson.getBounds();
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [20, 20], maxZoom: 11 });
    }
  } catch (error) {
    console.error(error);
    setError(`nodes.geojson konnte nicht geladen werden: ${error.message}`);
  }
});
