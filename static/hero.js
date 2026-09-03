/* ============================================================================
   CINEMATIC HERO SCENE — Overview card
   A live WebGL shot of LYRA-1 over the Earth limb: NASA day imagery on the
   sunlit side, NASA Black Marble city lights on the night side, atmospheric
   rim glow, drifting starfield, and a slow camera move. Replaces the flat 2D
   canvas. Driven by the same telemetry as the rest of the dashboard.
   ========================================================================== */
(function () {
  'use strict';

  const R = 100;                 // Earth radius in scene units
  let renderer, scene, camera, earth, clouds, atmo, sat, starfield;
  let host, raf = null, started = false;
  let t0 = performance.now();
  let eclipse = false, severity = 'NOMINAL';
  let quality = 1;

  const SEV_COLOR = {
    NOMINAL: 0x22d3ee, LOW: 0x84cc16, MEDIUM: 0xfacc15,
    HIGH: 0xfb923c, CRITICAL: 0xef4444,
  };

  /* --------------------------------------------------------------- shaders */
  // Blends the day and night textures across the terminator, so the city
  // lights appear exactly where the sun isn't — the look of the reference shot.
  const earthVert = `
    varying vec2 vUv;
    varying vec3 vNormal;
    void main() {
      vUv = uv;
      vNormal = normalize(normalMatrix * normal);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`;

  const earthFrag = `
    uniform sampler2D dayMap;
    uniform sampler2D nightMap;
    uniform vec3 sunDir;
    uniform float nightBoost;
    varying vec2 vUv;
    varying vec3 vNormal;
    void main() {
      vec3 day = texture2D(dayMap, vUv).rgb;
      vec3 night = texture2D(nightMap, vUv).rgb;
      float lambert = dot(normalize(vNormal), normalize(sunDir));
      // Soft terminator instead of a hard shadow line.
      float mixv = smoothstep(-0.22, 0.30, lambert);
      vec3 lit = day * (0.35 + 0.85 * max(lambert, 0.0));
      vec3 dark = night * nightBoost;
      gl_FragColor = vec4(mix(dark, lit, mixv), 1.0);
    }`;

  // Fresnel rim — brightest where the surface turns away from the camera.
  const atmoVert = `
    varying vec3 vNormal;
    void main() {
      vNormal = normalize(normalMatrix * normal);
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`;

  const atmoFrag = `
    varying vec3 vNormal;
    uniform vec3 glow;
    void main() {
      float intensity = pow(0.72 - dot(vNormal, vec3(0.0, 0.0, 1.0)), 3.0);
      gl_FragColor = vec4(glow, 1.0) * intensity;
    }`;

  /* ------------------------------------------------------------- satellite */
  // Built from primitives: a gold-foil bus, two solar wings, a dish and a
  // boom. Cheap to render and reads clearly at small size.
  function buildSatellite() {
    const g = new THREE.Group();

    const gold = new THREE.MeshStandardMaterial({ color: 0xd8b25a, metalness: 0.85, roughness: 0.35 });
    const panelMat = new THREE.MeshStandardMaterial({ color: 0x1b3a86, metalness: 0.6, roughness: 0.35,
                                                      emissive: 0x0a1836, emissiveIntensity: 0.6 });
    const frameMat = new THREE.MeshStandardMaterial({ color: 0x8a8f9a, metalness: 0.9, roughness: 0.4 });
    const dishMat = new THREE.MeshStandardMaterial({ color: 0xdfe4ea, metalness: 0.5, roughness: 0.5,
                                                     side: THREE.DoubleSide });

    const bus = new THREE.Mesh(new THREE.BoxGeometry(4.2, 4.6, 4.2), gold);
    g.add(bus);

    // Solar wings
    for (const dir of [-1, 1]) {
      const boom = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 3.2, 8), frameMat);
      boom.rotation.z = Math.PI / 2;
      boom.position.x = dir * 3.6;
      g.add(boom);

      const wing = new THREE.Mesh(new THREE.BoxGeometry(9.5, 3.4, 0.18), panelMat);
      wing.position.x = dir * 10.2;
      g.add(wing);

      // Cell striping
      for (let i = -2; i <= 2; i++) {
        const rib = new THREE.Mesh(new THREE.BoxGeometry(0.12, 3.4, 0.24), frameMat);
        rib.position.set(dir * 10.2 + i * 1.9, 0, 0);
        g.add(rib);
      }
    }

    // Communications dish, facing local -Z so it points at Earth after lookAt.
    const dish = new THREE.Mesh(new THREE.SphereGeometry(2.1, 20, 12, 0, Math.PI * 2, 0, Math.PI / 2.6), dishMat);
    dish.position.set(0, 0.4, -2.9);
    dish.rotation.x = -Math.PI / 2;
    g.add(dish);
    const feed = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.12, 1.7, 6), frameMat);
    feed.position.set(0, 0.4, -4.3);
    feed.rotation.x = Math.PI / 2;
    g.add(feed);

    // Upward antenna boom, for silhouette interest against the sky.
    const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.1, 4.2, 6), frameMat);
    mast.position.set(0, 4.2, 0.6);
    g.add(mast);

    // Status beacon — recoloured by anomaly severity
    const beacon = new THREE.Mesh(
      new THREE.SphereGeometry(0.45, 10, 8),
      new THREE.MeshBasicMaterial({ color: SEV_COLOR.NOMINAL })
    );
    beacon.position.set(0, -2.7, 1.4);
    beacon.name = 'beacon';
    g.add(beacon);

    return g;
  }

  function makeStars() {
    const n = 1400;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      // Shell well outside the Earth so stars never intersect it.
      const r = 900 + Math.random() * 500;
      const th = Math.random() * Math.PI * 2;
      const ph = Math.acos(2 * Math.random() - 1);
      pos[i * 3] = r * Math.sin(ph) * Math.cos(th);
      pos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th);
      pos[i * 3 + 2] = r * Math.cos(ph);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    return new THREE.Points(geo, new THREE.PointsMaterial({ color: 0xdfe9ff, size: 2.0, sizeAttenuation: false, transparent: true, opacity: 0.85 }));
  }

  /* ------------------------------------------------------------------ boot */
  function init() {
    host = document.getElementById('heroStage');
    if (!host || started) return;
    if (typeof THREE === 'undefined') return;      // vendor script missing
    started = true;

    const w = host.clientWidth || 800;
    const h = host.clientHeight || 420;

    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    } catch (e) {
      host.classList.add('hero-failed');
      return;                                       // no WebGL — leave the 2D canvas visible
    }
    quality = Math.min(window.devicePixelRatio || 1, 2);
    renderer.setPixelRatio(quality);
    renderer.setSize(w, h);
    host.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(34, w / h, 1, 4000);

    const loader = new THREE.TextureLoader();
    const dayTex = loader.load('/api/space/earth-texture?width=2048');
    const nightTex = loader.load('/api/space/night-texture?width=2048');
    [dayTex, nightTex].forEach((t) => { t.colorSpace = THREE.SRGBColorSpace; });

    earth = new THREE.Mesh(
      new THREE.SphereGeometry(R, 96, 64),
      new THREE.ShaderMaterial({
        uniforms: {
          dayMap: { value: dayTex },
          nightMap: { value: nightTex },
          sunDir: { value: new THREE.Vector3(1, 0.25, 0.6).normalize() },
          nightBoost: { value: 1.35 },
        },
        vertexShader: earthVert,
        fragmentShader: earthFrag,
      })
    );
    scene.add(earth);

    atmo = new THREE.Mesh(
      new THREE.SphereGeometry(R * 1.055, 64, 48),
      new THREE.ShaderMaterial({
        uniforms: { glow: { value: new THREE.Color(0x3f8cff) } },
        vertexShader: atmoVert,
        fragmentShader: atmoFrag,
        blending: THREE.AdditiveBlending,
        side: THREE.BackSide,
        transparent: true,
        depthWrite: false,
      })
    );
    scene.add(atmo);

    starfield = makeStars();
    scene.add(starfield);

    sat = buildSatellite();
    scene.add(sat);

    scene.add(new THREE.AmbientLight(0x9fb6d8, 1.15));
    const key = new THREE.DirectionalLight(0xfff3dd, 2.6);
    key.position.set(180, 60, 130);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x5b9bff, 1.0);
    rim.position.set(-140, -40, -90);
    scene.add(rim);

    const resize = () => {
      const cw = host.clientWidth || 800, ch = host.clientHeight || 420;
      renderer.setSize(cw, ch);
      camera.aspect = cw / ch;
      camera.updateProjectionMatrix();
    };
    window.addEventListener('resize', resize);
    new ResizeObserver(resize).observe(host);
    resize();

    animate();
  }

  /* ------------------------------------------------------------------ loop */
  function animate() {
    raf = requestAnimationFrame(animate);

    // Skip work entirely when the Overview page isn't showing.
    const page = document.getElementById('page-overview');
    if (page && !page.classList.contains('active')) return;
    if (document.hidden) return;

    const t = (performance.now() - t0) / 1000;

    earth.rotation.y = t * 0.011;
    starfield.rotation.y = t * 0.002;

    // --- Satellite on an inclined orbit -----------------------------------
    const sa = t * 0.055;                       // orbital angle
    const inc = 0.42;                           // inclination
    const rr = R * 1.30;
    const pos = new THREE.Vector3(
      Math.cos(sa) * rr,
      Math.sin(sa) * rr * Math.sin(inc),
      Math.sin(sa) * rr * Math.cos(inc)
    );
    sat.position.copy(pos);

    // Orbital basis, needed by both the satellite's attitude and the camera.
    const fwd = new THREE.Vector3(-Math.sin(sa), Math.cos(sa) * Math.sin(inc), Math.cos(sa) * Math.cos(inc)).normalize();
    const up = pos.clone().normalize();                       // local "up" = away from Earth
    const side = new THREE.Vector3().crossVectors(fwd, up).normalize();

    // Nadir-pointing: local -Z (the dish) faces Earth, which leaves the solar
    // wings on local X — spread across the frame rather than pointing at the
    // planet. `up` must not be parallel to the look direction, and the look
    // direction here is radially inward, so use the velocity vector.
    sat.up.copy(fwd);
    sat.lookAt(0, 0, 0);
    sat.rotateZ(Math.sin(t * 0.22) * 0.13);

    // --- Chase camera ------------------------------------------------------
    // Sit behind, above and outboard of the satellite so it reads large in the
    // foreground with the Earth limb filling the frame behind it — the
    // composition of the reference shot. Breathing on the offsets keeps the
    // shot alive without ever losing the subject.
    const back = 74 + Math.sin(t * 0.17) * 7;
    const outward = 26 + Math.sin(t * 0.11) * 5;
    const lateral = 34 + Math.cos(t * 0.13) * 7;

    camera.position.copy(pos)
      .addScaledVector(fwd, -back)
      .addScaledVector(up, outward)
      .addScaledVector(side, lateral);

    // Aim below the satellite so it sits in the upper third with the Earth
    // limb sweeping across the lower two thirds.
    const aim = pos.clone().addScaledVector(up, -30).addScaledVector(fwd, 16);
    camera.lookAt(aim);
    camera.up.copy(up);

    const beacon = sat.getObjectByName('beacon');
    if (beacon) {
      beacon.material.color.setHex(SEV_COLOR[severity] || SEV_COLOR.NOMINAL);
      const pulse = severity === 'NOMINAL' ? 0.75 : 0.45 + 0.55 * Math.abs(Math.sin(t * 4));
      beacon.scale.setScalar(0.8 + pulse * 0.7);
    }

    // Dim the sun a little during eclipse so the card reflects the telemetry.
    earth.material.uniforms.nightBoost.value += ((eclipse ? 1.9 : 1.3) - earth.material.uniforms.nightBoost.value) * 0.02;

    renderer.render(scene, camera);
  }

  /* --------------------------------------------------------- external hooks */
  window.heroTelemetry = function (payload) {
    if (payload && payload.orbital) eclipse = !!payload.orbital.in_eclipse;
  };
  window.heroSeverity = function (sev) { severity = (sev || 'NOMINAL').toUpperCase(); };

  document.addEventListener('DOMContentLoaded', () => setTimeout(init, 120));
})();
