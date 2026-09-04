/* ===========================================================================
   AETHER — polygon satellite meshes for the orbit map
   ---------------------------------------------------------------------------
   Drop-in replacement for the `satMesh(d)` factory in static/orbitmap.js.

   Why this exists
   ---------------
   orbitmap.js currently returns a sphere for every tracked object:

       _geoSmall = new THREE.SphereGeometry(0.7, 8, 6);   // ~84 triangles
       _geoBig   = new THREE.SphereGeometry(1.9, 12, 10); // ~240 triangles

   With ~1300 objects that is ~110k triangles of featureless dots, and none of
   them read as a satellite. This module gives two tiers instead:

     FOCUS  — a real articulated mesh (bus + two solar arrays + dish + boom
              + edge outline) for LYRA-1, the selected object, and anything
              explicitly flagged. Capped, by default at 12.
     FIELD  — a flat-shaded Octahedron, 8 triangles, for everything else.
              This is ~10x cheaper than the sphere it replaces and reads as a
              faceted object rather than a blurry dot.

   Everything is unlit (MeshBasicMaterial). That is deliberate: globe.gl's
   default scene lighting is not under this module's control, so shading is
   baked in as per-part colour instead of depending on a light that may or may
   not point where we expect. Geometries and materials are created once and
   shared; only the Object3D wrapper is per-instance.

   Usage — three edits in static/orbitmap.js
   -----------------------------------------
     1. In index.html, before orbitmap.js:
            <script src="/static/satmesh.js"></script>

     2. Replace the whole satMesh() function with:
            const satMesh = AetherSatMesh.factory();

     3. Wherever `selected` changes (selectPoint / onGlobeClick), add:
            AetherSatMesh.setFocus(selected ? selected.noradId : null);

   Optional: `.objectFacesSurface(true)` on the globe makes the solar arrays
   sit perpendicular to nadir, which is how a real LEO bird flies. This module
   builds the mesh with +Y as the nadir axis so that reads correctly. Left as
   your call — the current code passes false.

   Verified: THREE r160 and globe.gl 2.32.4 as vendored in static/vendor/.
   Every geometry class used below is present in that bundle.
   =========================================================================== */

(function (global) {
  'use strict';

  if (typeof THREE === 'undefined') {
    throw new Error('[satmesh] THREE is not loaded — include three.min.js first.');
  }

  // --- tuning ---------------------------------------------------------------
  // Units are globe.gl scene units, where the Earth radius is 100.
  const CFG = {
    scale: 1.0,        // multiplier on the whole focus mesh
    fieldRadius: 0.62, // octahedron radius for the field tier
    maxFocus: 12,      // hard cap on articulated meshes in the scene
    edges: true,       // silhouette outline on focus meshes
  };

  const PART = {
    bus: '#b9c4d4',
    busDark: '#5c6a7e',
    panel: '#16233d',
    panelRib: '#4d8fd6',
    dish: '#e2e8f2',
  };

  // --- shared resources -----------------------------------------------------
  // Built once on first use, then reused for the life of the page. Not
  // disposed: they live as long as the globe does, and disposing them would
  // invalidate meshes still in the scene graph.
  let geo = null;
  const matCache = new Map();
  let focusId = null;
  let focusCount = 0;

  function mat(color) {
    if (!matCache.has(color)) {
      matCache.set(color, new THREE.MeshBasicMaterial({ color: color }));
    }
    return matCache.get(color);
  }

  function lineMat(color) {
    const key = 'line:' + color;
    if (!matCache.has(key)) {
      matCache.set(key, new THREE.LineBasicMaterial({
        color: color, transparent: true, opacity: 0.55,
      }));
    }
    return matCache.get(key);
  }

  function buildGeometry() {
    if (geo) return geo;
    geo = {
      // Spacecraft bus. +Y points at nadir once objectFacesSurface is on.
      bus: new THREE.BoxGeometry(0.80, 1.05, 0.80),
      // Solar arrays: thin slabs either side, long axis on X.
      panel: new THREE.BoxGeometry(1.75, 0.045, 0.70),
      // Two ribs per array so the wing reads as panelled, not as a blank slab.
      rib: new THREE.BoxGeometry(1.75, 0.05, 0.045),
      // Nadir-pointing dish.
      dish: new THREE.ConeGeometry(0.34, 0.30, 10, 1, true),
      // Antenna boom off the anti-nadir face.
      boom: new THREE.CylinderGeometry(0.035, 0.035, 0.85, 5),
      // Field tier: a real polyhedron, 8 triangles.
      field: new THREE.OctahedronGeometry(CFG.fieldRadius, 0),
    };
    geo.busEdges = new THREE.EdgesGeometry(geo.bus);
    return geo;
  }

  // --- focus mesh -----------------------------------------------------------
  function buildFocus(accent) {
    const g = buildGeometry();
    const grp = new THREE.Group();

    const bus = new THREE.Mesh(g.bus, mat(PART.bus));
    grp.add(bus);

    // Darker underside band so the bus reads as a solid volume without lights.
    const skirt = new THREE.Mesh(g.bus, mat(PART.busDark));
    skirt.scale.set(1.02, 0.30, 1.02);
    skirt.position.y = -0.42;
    grp.add(skirt);

    for (const side of [-1, 1]) {
      const wing = new THREE.Mesh(g.panel, mat(PART.panel));
      wing.position.x = side * 1.30;
      grp.add(wing);

      for (const off of [-0.20, 0.20]) {
        const rib = new THREE.Mesh(g.rib, mat(PART.panelRib));
        rib.position.set(side * 1.30, 0.026, off);
        grp.add(rib);
      }
    }

    const dish = new THREE.Mesh(g.dish, mat(PART.dish));
    dish.position.y = -0.62;
    dish.rotation.x = Math.PI; // mouth toward nadir
    grp.add(dish);

    const boom = new THREE.Mesh(g.boom, mat(accent));
    boom.position.y = 0.90;
    grp.add(boom);

    if (CFG.edges) {
      const outline = new THREE.LineSegments(g.busEdges, lineMat(accent));
      grp.add(outline);
    }

    grp.scale.setScalar(CFG.scale);
    return grp;
  }

  function buildField(accent) {
    return new THREE.Mesh(buildGeometry().field, mat(accent));
  }

  // --- public API -----------------------------------------------------------
  const API = {
    /**
     * Returns a function suitable for globe.gl's .objectThreeObject().
     * Called once per object per data update by globe.gl.
     */
    factory: function (options) {
      Object.assign(CFG, options || {});
      buildGeometry();

      return function satMesh(d) {
        const accent = d.color || '#38bdf8';
        const wantsFocus =
          d.isLyra === true ||
          d.detail === true ||
          (focusId != null && d.noradId === focusId);

        if (wantsFocus && focusCount < CFG.maxFocus) {
          focusCount++;
          return buildFocus(accent);
        }
        return buildField(accent);
      };
    },

    /**
     * Mark one NORAD ID as the focus object. Pass null to clear.
     * Call this from selectPoint() and from the onGlobeClick deselect handler,
     * then let the existing refresh() re-issue objectsData as it already does.
     */
    setFocus: function (noradId) {
      focusId = noradId == null ? null : String(noradId);
    },

    /**
     * globe.gl rebuilds every object when objectsData receives a new array, so
     * the focus budget has to reset on each pass. Call this at the top of
     * refresh(), immediately before globe.objectsData(pts).
     */
    beginFrame: function () {
      focusCount = 0;
    },

    config: CFG,
  };

  global.AetherSatMesh = API;
})(window);
