/* ===========================================================================
   AETHER — Overview hero scene
   ---------------------------------------------------------------------------
   Vertex-points Earth (icosahedron point cloud driven by a custom shader,
   with a wireframe shell and a starfield) plus a high-detail satellite on a
   visible orbit track.

   Ported from the "Three.js Interactive Vertex Earth" demo. Two deliberate
   changes were required to run here:

     1. The original is an ES module using an importmap that pulls three@0.161
        and OrbitControls from a CDN. This project has no build step, vendors
        three r160 as a global script, and has to work offline during a demo.
        So: rewritten as a plain IIFE against the global THREE.
     2. OrbitControls was dropped rather than vendored. This is a background
        scene, not something the operator drags — it auto-rotates, and the
        mouse still drives the shader's ripple via a raycast.

   Textures live in /static/assets/vertex-earth/ (copied from the demo's src/).

   Public API is unchanged from the previous hero.js, so app.js's existing
   call sites keep working:
     window.heroTelemetry(payload)   — orbital state from the WS stream
     window.heroSeverity(severity)   — drives the accent colour
   Adds:
     window.heroSetPageActive(bool)  — stop rendering when Overview is hidden
   =========================================================================== */

(function () {
  'use strict';

  /* Point-cloud density. The original demo uses 120, which is ~288k points.
     100 is ~200k and holds framerate far more comfortably on laptop GPUs,
     which is what this gets demoed on. Raise it if you have the headroom. */
  const DETAIL = 100;
  const TEX = '/static/assets/vertex-earth/';

  const SEVERITY_COLOR = {
    NOMINAL: 0x22d3ee,
    WARNING: 0xe8a34b,
    DEGRADED: 0xe8a34b,
    CRITICAL: 0xff4242,
  };

  const ORBIT_R = 1.5;
  const ORBIT_TILT = 0.42;

  let host, renderer, scene, camera, raf = null, started = false;
  let globeGroup, points, wire, stars, satellite, orbitRing;
  let uniforms, raycaster, pointerPos, globeUV;
  let orbitAngle = 0;
  let pageActive = true;
  const reduceMotion = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------ starfield */
  /* Inlined from the demo's src/getStarfield.js — same maths, no import. */
  function buildStarfield(numStars, sprite) {
    function randomSpherePoint() {
      const radius = Math.random() * 25 + 25;
      const u = Math.random();
      const v = Math.random();
      const theta = 2 * Math.PI * u;
      const phi = Math.acos(2 * v - 1);
      return new THREE.Vector3(
        radius * Math.sin(phi) * Math.cos(theta),
        radius * Math.sin(phi) * Math.sin(theta),
        radius * Math.cos(phi)
      );
    }

    const verts = [];
    const colors = [];
    for (let i = 0; i < numStars; i++) {
      const pos = randomSpherePoint();
      const col = new THREE.Color().setHSL(0.6, 0.2, Math.random());
      verts.push(pos.x, pos.y, pos.z);
      colors.push(col.r, col.g, col.b);
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

    return new THREE.Points(geo, new THREE.PointsMaterial({
      size: 0.2,
      vertexColors: true,
      map: sprite,
      transparent: true,
    }));
  }

  /* ------------------------------------------------------------- shaders */
  const vertexShader = `
    uniform float size;
    uniform sampler2D elevTexture;
    uniform vec2 mouseUV;

    varying vec2 vUv;
    varying float vVisible;
    varying float vDist;

    void main() {
      vUv = uv;
      vec4 mvPosition = modelViewMatrix * vec4( position, 1.0 );
      float elv = texture2D(elevTexture, vUv).r;
      vec3 vNormal = normalMatrix * normal;
      vVisible = step(0.0, dot( -normalize(mvPosition.xyz), normalize(vNormal)));
      mvPosition.z += 0.35 * elv;

      float dist = distance(mouseUV, vUv);
      float zDisp = 0.0;
      float thresh = 0.04;
      if (dist < thresh) {
        zDisp = (thresh - dist) * 10.0;
      }
      vDist = dist;
      mvPosition.z += zDisp;

      gl_PointSize = size;
      gl_Position = projectionMatrix * mvPosition;
    }
  `;

  const fragmentShader = `
    uniform sampler2D colorTexture;
    uniform sampler2D alphaTexture;
    uniform sampler2D otherTexture;

    varying vec2 vUv;
    varying float vVisible;
    varying float vDist;

    void main() {
      if (floor(vVisible + 0.1) == 0.0) discard;
      float alpha = 1.0 - texture2D(alphaTexture, vUv).r;
      vec3 color = texture2D(colorTexture, vUv).rgb;
      vec3 other = texture2D(otherTexture, vUv).rgb;
      float thresh = 0.04;
      if (vDist < thresh) {
        color = mix(color, other, (thresh - vDist) * 50.0);
      }
      gl_FragColor = vec4(color, alpha);
    }
  `;

  /* ------------------------------------------------------------ satellite */
  /* Built here rather than reusing satmesh.js: that module is sized for the
     orbit map, where the globe radius is 100 scene units and satellites are
     distant specks. Here the Earth radius is 1 and the satellite is a hero
     object seen close up, so it gets more parts and finer segments. */
  function buildSatellite(accent) {
    const g = new THREE.Group();

    const gold = new THREE.MeshStandardMaterial({
      color: 0x8a6f2e, roughness: 0.45, metalness: 0.85,
    });
    const panelMat = new THREE.MeshStandardMaterial({
      color: 0x14295c, roughness: 0.35, metalness: 0.5,
    });
    const panelRib = new THREE.MeshStandardMaterial({ color: 0x2f5ea8, roughness: 0.6 });
    const white = new THREE.MeshStandardMaterial({
      color: 0xc9d4e4, roughness: 0.5, metalness: 0.3,
    });

    // Bus, wrapped in MLI-gold.
    g.add(new THREE.Mesh(new THREE.BoxGeometry(0.055, 0.055, 0.055), gold));

    // Two deployed arrays with ribs, on a boom either side.
    for (const side of [-1, 1]) {
      const boom = new THREE.Mesh(
        new THREE.CylinderGeometry(0.003, 0.003, 0.045, 8), white
      );
      boom.rotation.z = Math.PI / 2;
      boom.position.x = side * 0.05;
      g.add(boom);

      const wing = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.004, 0.05), panelMat);
      wing.position.x = side * 0.135;
      g.add(wing);

      for (const off of [-0.014, 0, 0.014]) {
        const rib = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.005, 0.002), panelRib);
        rib.position.set(side * 0.135, 0.0025, off);
        g.add(rib);
      }
    }

    // Nadir dish + feed horn.
    const dish = new THREE.Mesh(new THREE.SphereGeometry(
      0.028, 20, 12, 0, Math.PI * 2, 0, Math.PI / 2.4
    ), white);
    dish.position.y = 0.042;
    g.add(dish);

    const feed = new THREE.Mesh(new THREE.CylinderGeometry(0.0025, 0.0025, 0.022, 6), white);
    feed.position.y = 0.05;
    g.add(feed);

    // Status beacon — recoloured by heroSeverity().
    const beacon = new THREE.Mesh(
      new THREE.SphereGeometry(0.008, 12, 10),
      new THREE.MeshBasicMaterial({ color: accent })
    );
    beacon.position.set(0, -0.018, 0.03);
    beacon.name = 'beacon';
    g.add(beacon);

    // Antenna boom trailing anti-nadir.
    const ant = new THREE.Mesh(new THREE.CylinderGeometry(0.002, 0.002, 0.06, 6), white);
    ant.position.y = -0.055;
    g.add(ant);

    return g;
  }

  function buildOrbitRing(radius, accent) {
    const pts = [];
    for (let i = 0; i <= 180; i++) {
      const a = (i / 180) * Math.PI * 2;
      pts.push(new THREE.Vector3(Math.cos(a) * radius, 0, Math.sin(a) * radius));
    }
    return new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: accent, transparent: true, opacity: 0.48 })
    );
  }

  /* ----------------------------------------------------------------- boot */
  function boot() {
    host = document.getElementById('heroStage');
    if (!host || started) return;

    if (typeof THREE === 'undefined') {
      host.classList.add('hero-failed');
      return;
    }

    started = true;

    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch (e) {
      host.classList.add('hero-failed');
      started = false;
      return;
    }

    const w = host.clientWidth || 800;
    const h = host.clientHeight || 500;

    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    host.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 1000);
    camera.position.set(0, 0.55, 3.1);
    camera.lookAt(0, 0, 0);

    raycaster = new THREE.Raycaster();
    pointerPos = new THREE.Vector2();
    globeUV = new THREE.Vector2();

    const loader = new THREE.TextureLoader();
    const starSprite = loader.load(TEX + 'circle.png');
    const otherMap = loader.load(TEX + '04_rainbow1k.jpg');
    const colorMap = loader.load(TEX + '00_earthmap1k.jpg');
    const elevMap = loader.load(TEX + '01_earthbump1k.jpg');
    const alphaMap = loader.load(TEX + '02_earthspec1k.jpg');

    globeGroup = new THREE.Group();
    scene.add(globeGroup);

    wire = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1, 16),
      new THREE.MeshBasicMaterial({
        color: 0x0099ff, wireframe: true, transparent: true, opacity: 0.1,
      })
    );
    globeGroup.add(wire);

    uniforms = {
      size: { type: 'f', value: 4.0 },
      colorTexture: { type: 't', value: colorMap },
      otherTexture: { type: 't', value: otherMap },
      elevTexture: { type: 't', value: elevMap },
      alphaTexture: { type: 't', value: alphaMap },
      mouseUV: { type: 'v2', value: new THREE.Vector2(0.0, 0.0) },
    };

    points = new THREE.Points(
      new THREE.IcosahedronGeometry(1, DETAIL),
      new THREE.ShaderMaterial({
        uniforms: uniforms,
        vertexShader: vertexShader,
        fragmentShader: fragmentShader,
        transparent: true,
      })
    );
    globeGroup.add(points);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x080820, 3));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(3, 2, 4);
    scene.add(key);

    stars = buildStarfield(4500, starSprite);
    scene.add(stars);

    const accent = SEVERITY_COLOR.NOMINAL;
    orbitRing = buildOrbitRing(ORBIT_R, accent);
    // Lift orbit path so the front trajectory sweeps centered and visibly across the globe
    orbitRing.rotation.x = -ORBIT_TILT;
    orbitRing.rotation.y = 0.25;
    scene.add(orbitRing);

    satellite = buildSatellite(accent);
    scene.add(satellite);

    // Interactive camera rotation controls (drag to rotate from any angle, like Orbit Map)
    initOrbitControls();

    window.addEventListener('resize', resize);
    if (window.ResizeObserver) {
      new ResizeObserver(() => resize()).observe(host);
    }
    resize();
    animate();
  }

  /* ------------------------------------------------ interactive controls */
  let isDragging = false;
  let prevPointerX = 0;
  let prevPointerY = 0;
  let camTheta = 0;                  // azimuth angle
  let camPhi = Math.PI / 2.3;        // polar angle (elevation)
  let camRadius = 3.1;               // distance from earth
  let targetTheta = 0;
  let targetPhi = Math.PI / 2.3;
  let targetRadius = 3.1;
  const MIN_RADIUS = 1.8;
  const MAX_RADIUS = 5.2;

  function initOrbitControls() {
    if (!host) return;

    host.style.cursor = 'grab';

    host.addEventListener('pointerdown', (e) => {
      // Ignore clicks on floating panels or controls
      if (e.target.closest && e.target.closest('.ov-float, .card, button, a')) return;
      isDragging = true;
      prevPointerX = e.clientX;
      prevPointerY = e.clientY;
      host.style.cursor = 'grabbing';
      if (host.setPointerCapture) {
        try { host.setPointerCapture(e.pointerId); } catch (_) {}
      }
    });

    window.addEventListener('pointermove', (e) => {
      if (isDragging) {
        const deltaX = e.clientX - prevPointerX;
        const deltaY = e.clientY - prevPointerY;
        prevPointerX = e.clientX;
        prevPointerY = e.clientY;

        targetTheta -= deltaX * 0.007;
        targetPhi -= deltaY * 0.007;
        // Clamp phi to avoid flipping at poles
        targetPhi = Math.max(0.12, Math.min(Math.PI - 0.12, targetPhi));
      } else {
        onMouse(e);
      }
    });

    const stopDrag = (e) => {
      if (isDragging) {
        isDragging = false;
        if (host) host.style.cursor = 'grab';
        if (host && host.releasePointerCapture && e.pointerId != null) {
          try { host.releasePointerCapture(e.pointerId); } catch (_) {}
        }
      }
    };

    window.addEventListener('pointerup', stopDrag);
    window.addEventListener('pointercancel', stopDrag);

    host.addEventListener('wheel', (e) => {
      e.preventDefault();
      targetRadius += e.deltaY * 0.003;
      targetRadius = Math.max(MIN_RADIUS, Math.min(MAX_RADIUS, targetRadius));
    }, { passive: false });
  }

  function resize() {
    if (!renderer || !host) return;
    const w = host.clientWidth || 800;
    const h = host.clientHeight || 500;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  function onMouse(evt) {
    if (!host) return;
    const r = host.getBoundingClientRect();
    pointerPos.set(
      ((evt.clientX - r.left) / r.width) * 2 - 1,
      -((evt.clientY - r.top) / r.height) * 2 + 1
    );
  }

  function handleRaycast() {
    if (isDragging) return;
    raycaster.setFromCamera(pointerPos, camera);
    const hits = raycaster.intersectObjects([wire], false);
    if (hits.length > 0 && hits[0].uv) globeUV.copy(hits[0].uv);
    uniforms.mouseUV.value = globeUV;
  }

  /* ----------------------------------------------------------------- loop */
  function animate() {
    raf = requestAnimationFrame(animate);

    // Skip all work when Overview isn't showing or the tab is backgrounded.
    if (!pageActive) return;
    const page = document.getElementById('page-overview');
    if (page && !page.classList.contains('active')) return;
    if (document.hidden) return;

    // Realistic rotational speeds:
    // Earth completes 1 rotation every 24h; LEO satellite completes 1 orbit every ~92min (~15.6:1 ratio).
    // Slower, smooth and majestic speeds:
    const EARTH_SPEED = 0.00016;
    const SAT_SPEED = EARTH_SPEED * 15.6; // ~0.0025

    if (!reduceMotion) {
      globeGroup.rotation.y += EARTH_SPEED;
      orbitAngle += SAT_SPEED;
    }

    // Smooth camera damping for interactive rotation
    camTheta += (targetTheta - camTheta) * 0.08;
    camPhi += (targetPhi - camPhi) * 0.08;
    camRadius += (targetRadius - camRadius) * 0.08;

    camera.position.set(
      camRadius * Math.sin(camPhi) * Math.sin(camTheta),
      camRadius * Math.cos(camPhi),
      camRadius * Math.sin(camPhi) * Math.cos(camTheta)
    );
    camera.lookAt(0, 0, 0);

    // Satellite rides the lifted, centered visible orbit track:
    // Transform through the orbit ring's euler rotation (-ORBIT_TILT on X, 0.25 on Y)
    const rawX = Math.cos(orbitAngle) * ORBIT_R;
    const rawZ = Math.sin(orbitAngle) * ORBIT_R;
    const satPos = new THREE.Vector3(rawX, 0, rawZ);
    satPos.applyEuler(new THREE.Euler(-ORBIT_TILT, 0.25, 0));

    satellite.position.copy(satPos);
    satellite.lookAt(0, 0, 0);
    satellite.rotateX(Math.PI / 2);   // point dish at nadir

    handleRaycast();
    renderer.render(scene, camera);
  }

  /* ------------------------------------------------------------ public API */
  window.heroTelemetry = function (payload) {
    // Deliberately a no-op on geometry. The previous hero drove its animation
    // from telemetry; this scene's motion is decorative, and wiring unrelated
    // numbers into it would be fake data dressed as instrumentation. The hook
    // stays so app.js's call site is valid and real orbital state has a home.
    return payload;
  };

  window.heroSeverity = function (sev) {
    const c = SEVERITY_COLOR[(sev || 'NOMINAL').toUpperCase()] || SEVERITY_COLOR.NOMINAL;
    if (orbitRing) orbitRing.material.color.setHex(c);
    if (satellite) {
      const beacon = satellite.getObjectByName('beacon');
      if (beacon) beacon.material.color.setHex(c);
    }
  };

  window.heroSetPageActive = function (active) {
    pageActive = !!active;
  };

  document.addEventListener('DOMContentLoaded', boot);
  if (document.readyState !== 'loading') boot();
})();
