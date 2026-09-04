/* ============================================================================
   ORBIT MAP — live 3D satellite tracker
   Real TLE orbital elements from CelesTrak, propagated in-browser with SGP4
   (satellite.js), rendered on a globe textured with NASA GIBS VIIRS imagery.
   LYRA-1 (the mission satellite this dashboard operates) is injected as a
   synthetic object and colour-coded by its live anomaly state.
   ========================================================================== */
(function () {
  'use strict';

  const EARTH_R = 6371;           // km, for altitude -> globe.gl altitude units
  const PROPAGATE_HZ = 1;         // full constellation position refresh rate
  const TRAIL_MINUTES = 45;       // orbit path drawn around selected satellite

  const CONSTELLATIONS = {
    starlink: { label: 'Starlink',    color: '#38bdf8', group: 'starlink' },
    stations: { label: 'Space Stations', color: '#34d399', group: 'stations' },
    gps:      { label: 'GPS / Navigation', color: '#f59e0b', group: 'gps-ops' },
    weather:  { label: 'Weather',     color: '#c084fc', group: 'weather' },
  };

  const LYRA_COLOR = { NOMINAL: '#22d3ee', LOW: '#84cc16', MEDIUM: '#facc15', HIGH: '#fb923c', CRITICAL: '#ef4444' };

  let globe = null;
  let booted = false;
  let sats = [];                  // [{name, satrec, kind, color, noradId, intl}]
  let selected = null;
  let watchlist = [];
  let propagateTimer = null;
  let lyraState = { severity: 'NOMINAL', alt: 513.7, lat: 0, lng: 0 };
  let showOrbits = true;
  let showLabels = false;
  let filters = { starlink: true, stations: true, gps: true, weather: true };

  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- utils */
  function tleFieldsFromLine1(l1) {
    // Col 3-7 NORAD id, col 10-17 international designator.
    return {
      noradId: (l1.slice(2, 7) || '').trim(),
      intl: (l1.slice(9, 17) || '').trim(),
    };
  }

  function propagateOne(satrec, when) {
    try {
      const pv = satellite.propagate(satrec, when);
      if (!pv || !pv.position) return null;
      const gmst = satellite.gstime(when);
      const geo = satellite.eciToGeodetic(pv.position, gmst);
      const lat = satellite.degreesLat(geo.latitude);
      const lng = satellite.degreesLong(geo.longitude);
      if (!isFinite(lat) || !isFinite(lng)) return null;
      return { lat, lng, altKm: geo.height };
    } catch (e) {
      return null;
    }
  }

  /* ------------------------------------------------------------ data load */
  async function loadGroup(kind) {
    const cfg = CONSTELLATIONS[kind];
    try {
      const res = await fetch(`/api/space/tle/${cfg.group}`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      let added = 0;
      for (const rec of data.satellites) {
        try {
          const satrec = satellite.twoline2satrec(rec.l1, rec.l2);
          if (!satrec || satrec.error) continue;
          sats.push({
            name: rec.name,
            satrec,
            kind,
            color: cfg.color,
            ...tleFieldsFromLine1(rec.l1),
          });
          added++;
        } catch (e) { /* skip malformed TLE */ }
      }
      return { ok: true, added, stale: !!data.stale };
    } catch (e) {
      console.warn('[orbitmap] failed to load', kind, e);
      return { ok: false, added: 0, error: String(e) };
    }
  }

  /* ------------------------------------------------------------- rendering */
  // One shared geometry per size class; materials are cached per colour so a
  // 1300-object scene allocates a handful of GPU resources instead of 1300.
  /* T1: satellite meshes come from satmesh.js — two tiers, articulated for
     the focus object and an 8-triangle polyhedron for the field. */
  const satMesh = AetherSatMesh.factory();
  /* materialFor/_matCache removed along with the sphere factory — satmesh.js
     keeps its own shared material cache and nothing else referenced them. */

  function currentPoints() {
    const now = new Date();
    const pts = [];

    for (const s of sats) {
      if (!filters[s.kind]) continue;
      const p = propagateOne(s.satrec, now);
      if (!p) continue;
      pts.push({
        lat: p.lat, lng: p.lng, altKm: p.altKm,
        alt: Math.max(p.altKm / EARTH_R, 0.005),
        color: s.color, name: s.name, kind: s.kind,
        noradId: s.noradId, intl: s.intl, satrec: s.satrec,
        isLyra: false,
      });
    }

    // The mission satellite, driven by live dashboard telemetry.
    pts.push({
      lat: lyraState.lat, lng: lyraState.lng, altKm: lyraState.alt,
      alt: Math.max(lyraState.alt / EARTH_R, 0.02),
      color: LYRA_COLOR[lyraState.severity] || LYRA_COLOR.NOMINAL,
      name: 'LYRA-1', kind: 'mission', noradId: '—', intl: 'SIH-2026',
      isLyra: true,
    });

    return pts;
  }

  function orbitPath(pt) {
    if (!pt) return [];
    // LYRA-1 has no TLE — trace its ground track from the same spherical
    // geometry the backend simulator uses, anchored to its current position.
    if (pt.isLyra) {
      const inc = ((lyraState.inc || 51.6) * Math.PI) / 180;
      const u0 = Math.asin(Math.max(-1, Math.min(1, Math.sin(pt.lat * Math.PI / 180) / Math.sin(inc))));
      const ring = [];
      for (let i = 0; i <= 120; i++) {
        const u = u0 + (i / 120) * 2 * Math.PI;
        const lat = (Math.asin(Math.sin(inc) * Math.sin(u)) * 180) / Math.PI;
        const dlon = (Math.atan2(Math.cos(inc) * Math.sin(u), Math.cos(u)) * 180) / Math.PI;
        const dlon0 = (Math.atan2(Math.cos(inc) * Math.sin(u0), Math.cos(u0)) * 180) / Math.PI;
        const lng = (((pt.lng + (dlon - dlon0)) + 540) % 360) - 180;
        ring.push([lat, lng, pt.altKm / EARTH_R]);
      }
      return [ring];
    }
    if (!pt.satrec) return [];
    const path = [];
    const now = Date.now();
    for (let m = -TRAIL_MINUTES; m <= TRAIL_MINUTES; m += 1.5) {
      const p = propagateOne(pt.satrec, new Date(now + m * 60000));
      if (p) path.push([p.lat, p.lng, Math.max(p.altKm / EARTH_R, 0.005)]);
    }
    return path.length > 2 ? [path] : [];
  }

  function refresh() {
    if (!globe) return;
    const pts = currentPoints();
    AetherSatMesh.beginFrame();
    globe.objectsData(pts);

    if (selected) {
      // Keep the selection object in sync with the freshly propagated set.
      const match = pts.find((p) => p.isLyra ? selected.isLyra : p.noradId === selected.noradId);
      if (match) { selected = match; updateInfoPanel(selected); }
      globe.pathsData(showOrbits ? orbitPath(selected) : []);
      globe.ringsData([{ lat: selected.lat, lng: selected.lng, color: selected.color }]);
    } else {
      globe.pathsData([]);
      globe.ringsData([]);
    }

    const shown = pts.length;
    const el = $('omCount');
    if (el) el.textContent = shown.toLocaleString();
    renderWatchlist();
  }

  /* ---------------------------------------------------------- info panel */
  function updateInfoPanel(pt) {
    const box = $('omInfo');
    if (!box) return;
    if (!pt) { box.style.display = 'none'; return; }
    box.style.display = 'block';

    $('omInfoName').textContent = pt.name;
    $('omInfoNorad').textContent = pt.noradId || '—';
    $('omInfoIntl').textContent = pt.intl || '—';
    $('omInfoConst').textContent = pt.isLyra ? 'MISSION' : (CONSTELLATIONS[pt.kind]?.label || pt.kind);
    $('omInfoLat').textContent = pt.lat.toFixed(4) + '°';
    $('omInfoLng').textContent = pt.lng.toFixed(4) + '°';
    $('omInfoAlt').textContent = pt.altKm.toFixed(1) + ' km';

    // Circular-orbit approximation: v = sqrt(GM / r)
    const r = EARTH_R + pt.altKm;
    const v = Math.sqrt(398600.4418 / r);
    $('omInfoVel').textContent = v.toFixed(2) + ' km/s';
    const periodMin = (2 * Math.PI * Math.sqrt(Math.pow(r, 3) / 398600.4418)) / 60;
    $('omInfoPeriod').textContent = periodMin.toFixed(1) + ' min';

    const tag = $('omInfoTag');
    tag.textContent = pt.altKm < 2000 ? 'LEO' : pt.altKm < 35000 ? 'MEO' : 'GEO';
    const minTag = $('omInfoMinTag');
    if (minTag) minTag.textContent = tag.textContent;
    const tag2 = $('omInfoTag2');
    if (pt.isLyra) { tag2.style.display = ''; tag2.textContent = lyraState.severity; tag2.style.color = pt.color; }
    else { tag2.style.display = 'none'; }
  }

  function selectPoint(pt) {
    selected = pt;
    AetherSatMesh.setFocus(pt ? pt.noradId : null);
    setReferenceSat(pt);
    updateInfoPanel(pt);
    if (pt) {
      globe.pointOfView({ lat: pt.lat, lng: pt.lng, altitude: 1.9 }, 900);
      if (!watchlist.find((w) => w.noradId === pt.noradId && w.name === pt.name)) {
        watchlist.unshift({ name: pt.name, noradId: pt.noradId, isLyra: pt.isLyra });
        watchlist = watchlist.slice(0, 6);
      }
    }
    refresh();
  }

  function renderWatchlist() {
    const list = $('omSelectorList');
    if (!list) return;
    if (!watchlist.length) { list.innerHTML = '<div class="om-empty">Click a satellite to track</div>'; return; }
    list.innerHTML = watchlist.map((w, i) =>
      `<div class="om-sel-row" data-idx="${i}">
         <span class="om-sel-info">info</span>
         <span class="om-sel-name">${w.name}</span>
         <button class="om-sel-x" data-x="${i}">✕</button>
       </div>`).join('');
  }

  /* ------------------------------------------------------------- controls */
  function wireControls() {
    $('omSelectorList')?.addEventListener('click', (e) => {
      const x = e.target.closest('[data-x]');
      if (x) { watchlist.splice(+x.dataset.x, 1); renderWatchlist(); return; }
      const row = e.target.closest('[data-idx]');
      if (row) {
        const w = watchlist[+row.dataset.idx];
        const pt = currentPoints().find((p) => p.name === w.name);
        if (pt) selectPoint(pt);
      }
    });

    $('omClearSel')?.addEventListener('click', () => {
      watchlist = []; selected = null; updateInfoPanel(null); renderWatchlist(); refresh();
    });

    $('omInfoMin')?.addEventListener('click', (e) => {
      e.stopPropagation();
      const box = $('omInfo');
      if (!box) return;
      const isMin = box.classList.toggle('minimised');
      const btn = $('omInfoMin');
      if (btn) {
        btn.textContent = isMin ? '+' : '−';
        btn.title = isMin ? 'Expand panel' : 'Minimise panel';
        btn.setAttribute('aria-expanded', String(!isMin));
      }
      const minTag = $('omInfoMinTag');
      if (minTag) minTag.style.display = isMin ? 'inline-block' : 'none';
    });

    $('omInfoHead')?.addEventListener('click', (e) => {
      if (e.target.closest('#omInfoClose') || e.target.closest('#omInfoMin')) return;
      const box = $('omInfo');
      if (box && box.classList.contains('minimised')) {
        box.classList.remove('minimised');
        const btn = $('omInfoMin');
        if (btn) {
          btn.textContent = '−';
          btn.title = 'Minimise panel';
          btn.setAttribute('aria-expanded', 'true');
        }
        const minTag = $('omInfoMinTag');
        if (minTag) minTag.style.display = 'none';
      }
    });

    $('omInfoClose')?.addEventListener('click', () => { selected = null; updateInfoPanel(null); refresh(); });

    document.querySelectorAll('.om-legend-row').forEach((row) => {
      row.addEventListener('click', () => {
        const k = row.dataset.kind;
        filters[k] = !filters[k];
        row.classList.toggle('off', !filters[k]);
        refresh();
      });
    });

    $('omToggleOrbit')?.addEventListener('click', (e) => {
      showOrbits = !showOrbits;
      e.currentTarget.classList.toggle('active', showOrbits);
      refresh();
    });

    $('omToggleLabels')?.addEventListener('click', (e) => {
      showLabels = !showLabels;
      e.currentTarget.classList.toggle('active', showLabels);
      globe.labelsData(showLabels ? currentPoints().filter((p) => p.isLyra || p.kind === 'stations') : []);
    });

    $('omTrackLyra')?.addEventListener('click', () => {
      const pt = currentPoints().find((p) => p.isLyra);
      if (pt) selectPoint(pt);
    });

    $('omHome')?.addEventListener('click', () => {
      selected = null; updateInfoPanel(null);
      globe.pointOfView({ lat: 15, lng: 40, altitude: 2.6 }, 1000);
      refresh();
    });
  }

  /* ----------------------------------------------------------------- boot */
  async function boot() {
    if (booted) return;
    booted = true;

    const host = $('omGlobe');
    const status = $('omStatus');
    if (!host) return;

    if (typeof Globe === 'undefined' || typeof satellite === 'undefined' || typeof THREE === 'undefined') {
      status.textContent = 'Globe libraries failed to load (static/vendor missing)';
      status.classList.add('om-err');
      return;
    }

    status.textContent = 'Building globe…';

    globe = Globe()(host)
      .backgroundColor('rgba(0,0,0,0)')
      .globeImageUrl('/api/space/earth-texture?width=2048')
      .showAtmosphere(true)
      .atmosphereColor('#3b82f6')
      .atmosphereAltitude(0.18)
      // objectsData, not pointsData: the points layer extrudes a bar from the
      // surface up to the given altitude, which turns MEO/GEO satellites into
      // enormous radial spikes. Objects place a real mesh at the altitude.
      .objectsData([])
      .objectLat('lat').objectLng('lng').objectAltitude('alt')
      .objectFacesSurface(false)
      .objectThreeObject(satMesh)
      .pathsData([])
      .pathPointLat((p) => p[0]).pathPointLng((p) => p[1]).pathPointAlt((p) => p[2])
      .pathColor(() => (selected ? selected.color : '#38bdf8'))
      .pathStroke(1.4)
      .pathTransitionDuration(0)
      .ringsData([])
      .ringColor((d) => () => d.color)
      .ringMaxRadius(4).ringPropagationSpeed(2).ringRepeatPeriod(700)
      .labelsData([])
      .labelLat('lat').labelLng('lng').labelAltitude((d) => d.alt + 0.01)
      .labelText('name').labelSize(0.9).labelColor('color').labelDotRadius(0)
      .objectLabel((d) => d.name)
      .onObjectClick(selectPoint)
      .onGlobeClick(() => {
        selected = null;
        AetherSatMesh.setFocus(null);
        updateInfoPanel(null);
        refresh();
      });

    globe.pointOfView({ lat: 15, lng: 40, altitude: 2.6 });

    // Slow auto-rotate, like satellitemap's idle state.
    const ctrl = globe.controls();
    ctrl.autoRotate = true;
    ctrl.autoRotateSpeed = 0.28;
    ctrl.enableDamping = true;
    host.addEventListener('mousedown', () => { ctrl.autoRotate = false; });

    const page = document.getElementById('page-orbitmap');
    const resize = () => {
      // Size the stage from where it actually sits, so header height and main
      // padding can change without the panels falling off the bottom edge.
      if (page && page.classList.contains('active')) {
        const top = page.getBoundingClientRect().top;
        page.style.height = Math.max(380, Math.floor(window.innerHeight - top - 14)) + 'px';
      }
      globe.width(host.clientWidth || 800);
      globe.height(host.clientHeight || 600);
    };
    resize();
    window.addEventListener('resize', resize);
    new ResizeObserver(resize).observe(host);

    // Load constellations progressively so the globe is interactive fast.
    status.textContent = 'Fetching orbital elements…';
    const order = ['stations', 'starlink', 'gps', 'weather'];
    let anyOk = false;
    for (const kind of order) {
      const r = await loadGroup(kind);
      anyOk = anyOk || r.ok;
      const el = $('omLegendCount-' + kind);
      if (el) el.textContent = r.ok ? sats.filter((s) => s.kind === kind).length.toLocaleString() : '—';
      refresh();
    }

    status.textContent = anyOk
      ? `${sats.length.toLocaleString()} objects · SGP4 live`
      : 'No orbital data (offline) — showing LYRA-1 only';
    if (!anyOk) status.classList.add('om-err');

    wireControls();
    renderWatchlist();
    clearInterval(propagateTimer);
    propagateTimer = setInterval(refresh, 1000 / PROPAGATE_HZ);
    refresh();
    loadEpic();
  }

  /* ------------------------------------------------- NASA EPIC camera feed */
  async function loadEpic() {
    const wrap = $('omEpic');
    if (!wrap) return;
    try {
      const r = await fetch('/api/space/epic');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      if (!d.frames || !d.frames.length) throw new Error('no frames');
      const f = d.frames[0];
      $('omEpicImg').src = f.image;
      $('omEpicImg').alt = f.caption || 'NASA EPIC Earth image';
      $('omEpicDate').textContent = f.date + ' UTC';
      $('omEpicCap').textContent = 'DSCOVR · EPIC · L1 Lagrange point';
    } catch (e) {
      $('omEpicDate').textContent = 'unavailable offline';
      $('omEpicCap').textContent = 'NASA EPIC feed unreachable';
      wrap.classList.add('om-err');
    }
  }

  /* --------------------------------------------- live dashboard telemetry */
  // Driven by the existing WebSocket stream so LYRA-1 on the globe reflects
  // the same telemetry and anomaly state as the rest of the dashboard.
  window.orbitMapTelemetry = function (payload) {
    const orb = payload && payload.orbital;
    if (!orb) return;
    if (typeof orb.latitude === 'number') lyraState.lat = orb.latitude;
    if (typeof orb.longitude === 'number') lyraState.lng = orb.longitude;
    if (typeof orb.altitude_km === 'number') lyraState.alt = orb.altitude_km;
    if (typeof orb.inclination_deg === 'number') lyraState.inc = orb.inclination_deg;
  };

  window.orbitMapSeverity = function (sev) {
    lyraState.severity = (sev || 'NOMINAL').toUpperCase();
  };

  /* Lazy-boot when the Orbit Map nav item is first opened — building a globe
     with thousands of objects on page load would stall the dashboard. */
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.querySelector('.nav-item[data-page="orbitmap"]');
    if (btn) btn.addEventListener('click', () => setTimeout(boot, 60));
  });

  /* Focus a satellite by name — used by the "track LYRA-1" control and handy
     for driving the map from the console during a demo. */
  window.orbitMapSelectByName = function (name) {
    const pt = currentPoints().find((p) => p.name === name)
            || currentPoints().find((p) => p.name.includes(name));
    if (pt) selectPoint(pt);
    return !!pt;
  };


  /* =====================================================================
     T3 — reference satellite
     The Overview page shows the altitude and velocity of whichever object
     is selected here. There is no backend endpoint for this on this branch
     and the backend is not being modified, so the reference lives in memory
     only: it does NOT survive a page reload. Say so rather than pretending.
     ===================================================================== */
  let referenceSat = null;
  let refTimer = null;

  function setReferenceSat(pt) {
    referenceSat = pt && pt.satrec
      ? { name: pt.name, noradId: pt.noradId, satrec: pt.satrec, color: pt.color }
      : null;
    pumpReference();
  }

  /* Full state vector, including velocity, which propagateOne() discards. */
  function referenceState() {
    if (!referenceSat) return null;
    try {
      const now = new Date();
      const pv = satellite.propagate(referenceSat.satrec, now);
      if (!pv || !pv.position || !pv.velocity) return null;
      const gmst = satellite.gstime(now);
      const geo = satellite.eciToGeodetic(pv.position, gmst);
      const v = pv.velocity;
      const speedKms = Math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z);
      if (!isFinite(geo.height) || !isFinite(speedKms)) return null;
      return {
        name: referenceSat.name,
        noradId: referenceSat.noradId,
        altKm: geo.height,
        speedKms: speedKms,
        lat: satellite.degreesLat(geo.latitude),
        lng: satellite.degreesLong(geo.longitude),
      };
    } catch (e) {
      return null;
    }
  }

  function pumpReference() {
    if (typeof window.onReferenceSatState === 'function') {
      window.onReferenceSatState(referenceState());
    }
  }

  /* Runs regardless of which page is visible — Overview needs it even when
     the globe is paused. One satellite at 1 Hz is negligible. */
  clearInterval(refTimer);
  refTimer = setInterval(pumpReference, 1000);

  window.orbitMapReferenceState = referenceState;
  window.orbitMapClearReference = function () { setReferenceSat(null); };

  /* =====================================================================
     T2 — stop work the user cannot see
     Propagating ~1300 satellites and rendering WebGL while the user is on
     Telemetry or Reports is pure waste. app.js calls this on nav change.
     ===================================================================== */
  window.orbitMapSetPageActive = function (active) {
    if (!globe) return;                    // not booted yet; nothing to pause
    if (active) {
      globe.resumeAnimation();
      clearInterval(propagateTimer);
      propagateTimer = setInterval(refresh, 1000 / PROPAGATE_HZ);
      refresh();
    } else {
      clearInterval(propagateTimer);
      propagateTimer = null;
      globe.pauseAnimation();
    }
  };

  window.orbitMapBoot = boot;
})();
