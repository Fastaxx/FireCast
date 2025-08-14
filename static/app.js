// --- Carte
const map = L.map('map', { zoomControl: true }).setView([44.5, 2.0], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18,
  attribution: '&copy; OpenStreetMap'
}).addTo(map);

const drawLayer = new L.FeatureGroup().addTo(map);
const resultLayer = new L.FeatureGroup().addTo(map);
map.addLayer(resultLayer);

const drawControl = new L.Control.Draw({
  draw: {
    polyline: false, marker: false, rectangle: false, circle: false, circlemarker: false,
    polygon: {
      allowIntersection: false,
      showArea: true,
      shapeOptions: { color: '#ff6b00', weight: 2, fillOpacity: 0.1 }
    }
  },
  edit: { featureGroup: drawLayer, edit: true, remove: true }
});
map.addControl(drawControl);

// --- Etat global
let currentGeom = null;
let latestFC = null;
let hourPolys = [];   // [h] -> Leaflet layer (polygon)
let hourLabels = [];  // [h] -> Leaflet marker(label)
let tlMax = 0;
let tlTimer = null;

// --- Outils UI
function hslColor(t) { const hue = 20 + 300 * t; return `hsl(${hue}, 90%, 55%)`; }
function windTowardsDeg(fromDeg) { return (Number(fromDeg) + 180) % 360; }

function updateCompass() {
  const from = Number(document.getElementById('wind_deg').value);
  const towards = windTowardsDeg(from);
  document.getElementById('from-deg').textContent = `${from.toFixed(0)}°`;
  document.getElementById('towards-deg').textContent = `${towards.toFixed(0)}°`;
  const arrow = document.getElementById('arrow-from');
  arrow.style.transform = `translate(-50%, -100%) rotate(${from}deg)`;
}
updateCompass();
document.getElementById('wind_deg').addEventListener('input', updateCompass);

// Vent fixe désactivé si météo
function toggleWindInputs() {
  const useMeteo = document.getElementById('use_meteo').checked;
  document.getElementById('wind_ms').disabled = useMeteo;
  document.getElementById('wind_deg').disabled = useMeteo;
  document.getElementById('accumulate').disabled = useMeteo;
}
document.getElementById('use_meteo').addEventListener('change', toggleWindInputs);
toggleWindInputs();

// --- Dessin
map.on(L.Draw.Event.CREATED, function (e) {
  drawLayer.clearLayers();
  clearResults();
  currentGeom = e.layer.toGeoJSON().geometry;
  drawLayer.addLayer(e.layer);
});
map.on('draw:edited', function(e) {
  e.layers.eachLayer(function(layer) { currentGeom = layer.toGeoJSON().geometry; });
});
map.on('draw:deleted', function() { currentGeom = null; clearResults(); });

// --- Résultats (construction par heure)
function clearResults() {
  latestFC = null;
  hourPolys = [];
  hourLabels = [];
  tlMax = 0;
  resultLayer.clearLayers();
  setTimelineMax(1);
  updateTlLabel(1);
}

function buildHourLayers(fc) {
  resultLayer.clearLayers();
  hourPolys = [];
  hourLabels = [];
  tlMax = fc.features.reduce((m,f)=>Math.max(m, f.properties.hour||0), 0);

  // Crée une couche par heure (polygone + label) mais ne les ajoute pas encore
  fc.features.forEach(f => {
    const h = f.properties.hour || 0;
    const t = h / Math.max(1, tlMax);
    const style = { color: hslColor(t), weight: 2, fillOpacity: 0.08 };

    const poly = L.geoJSON(f, { style }).getLayers()[0];
    let label = null;
    if (poly && poly.getBounds) {
      const c = poly.getBounds().getCenter();
      label = L.marker(c, { opacity: 0.0 })
                .bindTooltip(`H+${h}`, { permanent: true, direction: 'center', className: 'time-label' });
    }
    hourPolys[h] = poly;
    hourLabels[h] = label;
  });

  setTimelineMax(tlMax);
}

function showUpTo(h) {
  if (!tlMax) return;
  resultLayer.clearLayers();
  for (let i = 1; i <= Math.min(h, tlMax); i++) {
    const poly = hourPolys[i];
    const lbl  = hourLabels[i];
    if (!poly) continue;

    // style: courant plus visible
    const t = i / Math.max(1, tlMax);
    poly.setStyle({ color: hslColor(t), weight: i === h ? 3 : 1.5, fillOpacity: i === h ? 0.12 : 0.05 });

    poly.addTo(resultLayer);
    if (lbl) { lbl.addTo(resultLayer); lbl.openTooltip(); }
  }
  updateTlLabel(h);
}

function updateTlLabel(h) {
  const lbl = document.getElementById('tl-label');
  lbl.textContent = `H+${h}`;
}

function setTimelineMax(maxH) {
  const r = document.getElementById('tl-range');
  r.max = String(maxH);
  r.value = String(maxH);
  updateTlLabel(maxH);
  // remettre les boutons en état initial
  document.getElementById('tl-play').style.display = '';
  document.getElementById('tl-pause').style.display = 'none';
  stopAnim();
}

// --- Timeline events
function currentTl() { return Number(document.getElementById('tl-range').value); }
function setTl(h) {
  const r = document.getElementById('tl-range');
  const clamped = Math.max(1, Math.min(h, Number(r.max)));
  r.value = String(clamped);
  showUpTo(clamped);
}

document.getElementById('tl-range').addEventListener('input', e => setTl(Number(e.target.value)));
document.getElementById('tl-first').addEventListener('click', ()=> setTl(1));
document.getElementById('tl-last').addEventListener('click', ()=> setTl(tlMax));
document.getElementById('tl-prev').addEventListener('click', ()=> setTl(currentTl()-1));
document.getElementById('tl-next').addEventListener('click', ()=> setTl(currentTl()+1));

function stepMs() {
  const speed = parseFloat(document.getElementById('tl-speed').value || '1');
  // base 700 ms par pas ; accélérer/ralentir avec le facteur
  return Math.max(80, 700 / speed);
}

function play() {
  if (!tlMax) return;
  document.getElementById('tl-play').style.display = 'none';
  document.getElementById('tl-pause').style.display = '';
  stopAnim();
  tlTimer = setInterval(() => {
    const h = currentTl();
    if (h >= tlMax) {
      pause();
      return;
    }
    setTl(h + 1);
  }, stepMs());
}
function pause() {
  document.getElementById('tl-play').style.display = '';
  document.getElementById('tl-pause').style.display = 'none';
  stopAnim();
}
function stopAnim() {
  if (tlTimer) { clearInterval(tlTimer); tlTimer = null; }
}

document.getElementById('tl-play').addEventListener('click', play);
document.getElementById('tl-pause').addEventListener('click', pause);
document.getElementById('tl-speed').addEventListener('change', ()=> {
  if (tlTimer) { // relancer avec nouvelle vitesse
    stopAnim(); play();
  }
});

// --- Simulation
async function simulate() {
  if (!currentGeom) { alert("Dessine un périmètre d'abord."); return; }
  clearResults();

  const use_dem = document.getElementById('use_dem').checked;
  const use_meteo = document.getElementById('use_meteo').checked;
  const slopeInput = document.getElementById('slope_tan');
  slopeInput.disabled = use_dem;

  const payload = {
    perimeter: currentGeom,
    hours: Number(document.getElementById('hours').value),
    wind_ms: Number(document.getElementById('wind_ms').value),
    wind_deg: Number(document.getElementById('wind_deg').value),
    base_ros_ms: Number(document.getElementById('base_ros_ms').value),
    slope_tan: Number(slopeInput.value),
    accumulate: document.getElementById('accumulate').checked,
    use_dem,
    use_meteo
  };

  const btn = document.getElementById('btn-simulate');
  btn.disabled = true; btn.textContent = 'Calcul…';
  try {
    const res = await fetch('/api/simulate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) { throw new Error(await res.text()); }
    const fc = await res.json();
    latestFC = fc;

    // Infos pente
    const meta = fc.meta || {};
    const slopeDiv = document.getElementById('slope-info');
    if (meta.use_dem && meta.slope_from_dem_mean !== undefined) {
      slopeDiv.textContent = `Pente (DEM): tanθ=${Number(meta.slope_from_dem_mean).toFixed(3)} (p90 ${Number(meta.slope_from_dem_p90).toFixed(3)})`;
    } else if (meta.dem_error) {
      slopeDiv.textContent = `DEM indisponible (${meta.dem_error}). Pente saisie: tanθ=${payload.slope_tan.toFixed(3)}`;
    } else {
      slopeDiv.textContent = `Pente saisie: tanθ=${payload.slope_tan.toFixed(3)}`;
    }

    // Infos météo
    const meteoDiv = document.getElementById('meteo-info');
    if (meta.use_meteo && meta.meteo_preview) {
      const rows = meta.meteo_preview.slice(0, 6).map(p => {
        const hh = p.t.split('T')[1]?.slice(0,5) || p.t;
        return `${hh}: ${Number(p.ws_ms).toFixed(1)} m/s, ${Number(p.wd_deg).toFixed(0)}° from`;
      });
      meteoDiv.textContent = `Météo (Open-Meteo) — prochaines heures: ${rows.join(' | ')}`;
    } else if (meta.meteo_error) {
      meteoDiv.textContent = `Météo non utilisée (${meta.meteo_error}). Vent fixe appliqué.`;
    } else {
      meteoDiv.textContent = `Vent fixe: ${payload.wind_ms.toFixed(1)} m/s, ${payload.wind_deg.toFixed(0)}° from`;
    }

    // Construire couches par heure et afficher la fin (H+max)
    buildHourLayers(fc);
    setTl(tlMax);

    // Ajuster la vue une seule fois
    const bounds = resultLayer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20,20] });

    addLegend(tlMax);
  } catch (e) {
    alert("Erreur API: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Lancer';
  }
}

function addLegend(maxHour) {
  const existing = document.querySelector('.legend');
  if (existing) existing.remove();
  const legend = document.createElement('div');
  legend.className = 'legend';
  legend.innerHTML = `<b>Isochrones (heures)</b><br/>`;
  const n = Math.min(maxHour, 6);
  for (let i=1;i<=n;i++) {
    const t = i / Math.max(1, n);
    legend.innerHTML += `<span style="display:inline-block;width:12px;height:12px;background:${hslColor(t)};margin-right:6px;border-radius:2px;"></span>H+${i}<br/>`;
  }
  document.body.appendChild(legend);
}

// --- Exports
function exportGeoJSON() {
  if (!latestFC) { alert("Lance une simulation d'abord."); return; }
  const blob = new Blob([JSON.stringify(latestFC)], {type:'application/geo+json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'isochrones.geojson'; a.click(); URL.revokeObjectURL(a.href);
}
async function exportPDF() {
  if (!latestFC) { alert("Lance une simulation d'abord."); return; }
  const mapEl = document.getElementById('map');
  const canvas = await html2canvas(mapEl, {useCORS: true, logging: false});
  const dataUrl = canvas.toDataURL('image/png');

  const params = {
    hours: Number(document.getElementById('hours').value),
    wind_ms: Number(document.getElementById('wind_ms').value),
    wind_deg: Number(document.getElementById('wind_deg').value),
    base_ros_ms: Number(document.getElementById('base_ros_ms').value),
    slope_tan: Number(document.getElementById('slope_tan').value),
    accumulate: document.getElementById('accumulate').checked,
    use_dem: document.getElementById('use_dem').checked,
    use_meteo: document.getElementById('use_meteo').checked
  };

  const res = await fetch('/api/report', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ params, map_png: dataUrl })
  });
  if (!res.ok) { alert("Erreur PDF: " + await res.text()); return; }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'feucast_report.pdf';
  a.click(); URL.revokeObjectURL(a.href);
}

// --- Self-tests
async function runSelfTests() {
  const res = await fetch('/api/selftest');
  if (!res.ok) { alert("Selftest ko"); return; }
  const r = await res.json();
  alert(`Tests:
- Aire croissante: ${r.area_increasing ? 'OK' : 'KO'}
- Inclusion H+1 ⊂ H+2: ${r.nested ? 'OK' : 'KO'}`);
}

// --- Boutons
document.getElementById('btn-simulate').addEventListener('click', simulate);
document.getElementById('btn-clear').addEventListener('click', ()=>{
  drawLayer.clearLayers(); clearResults(); currentGeom = null;
});
document.getElementById('btn-export-geojson').addEventListener('click', exportGeoJSON);
document.getElementById('btn-export-pdf').addEventListener('click', exportPDF);
document.getElementById('btn-selftest').addEventListener('click', runSelfTests);
