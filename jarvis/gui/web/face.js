// «Своё лицо»: a 2D portrait animated in WebGL. Plane mesh with a warp grid (pseudo-3D bulge for parallax)
// + fragment-shader warps: jaw/lower lip drop with a dark inner mouth, eyelid squash for blinks,
// hologram layer (hue shift to the hologram colour or original colours, scanlines, flicker, glow pulses, particles).
// API (same as head.js): window.jarvis.{setState, setColor, setMic, speak, stopSpeech, setQuality, setGaze, pose,
//                                        setFace(url, landmarks), setColorMode('hologram'|'original')}
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

THREE.ColorManagement.enabled = false;           // authored as display values
const params = new URLSearchParams(location.search);
const LABELS = { listening: 'СЛУШАЮ', thinking: 'ДУМАЮ', muted: 'МИКРОФОН ВЫКЛ', loading: 'ЗАГРУЗКА' };
const S = {
  state: params.get('state') || 'loading', mic: 0, micS: 0,
  env: [], wid: [], envDt: 0.02, envStart: 0, mouth: 0, width: 0.5, forceMouth: undefined, forceWidth: undefined, lastT: null,
  blink: 1, nextBlink: 2.5, blinkStart: -10, forceBlink: undefined,
  fixedTime: params.has('t') ? parseFloat(params.get('t')) : null,
  quality: params.get('quality') || 'full', glitch: 0, nextGlitch: 5,
};
window.addEventListener('error', () => { if (!window.jarvisReady) document.title = 'error'; });

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power', preserveDrawingBuffer: true });
} catch (e) { document.title = 'error'; throw e; }
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
renderer.outputColorSpace = THREE.LinearSRGBColorSpace;
renderer.setClearColor(0x000000, 1);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
camera.position.set(0, 0, 2.05);

const uniforms = {
  uTex: { value: null }, uAspect: { value: 1 }, uTime: { value: 0 },
  uEyeL: { value: new THREE.Vector4(0.39, 0.47, 0.05, 0.015) }, uEyeR: { value: new THREE.Vector4(0.6, 0.47, 0.05, 0.015) },
  uMouth: { value: new THREE.Vector4(0.5, 0.69, 0.085, 0.015) }, uCurve: { value: 0 }, uChin: { value: 0.83 }, uWidth: { value: 1 },
  uJaw: { value: 0 }, uBlink: { value: 1 }, uLevel: { value: 0 }, uDim: { value: 1 }, uThink: { value: 0 },
  uColor: { value: new THREE.Color('#46dcff') }, uHueShift: { value: 0 }, uHolo: { value: 1 }, uGlitch: { value: 0 },
};
let imageHue = 0.55;                              // dominant hue of the picture (measured on load)
let targetColor = new THREE.Color('#46dcff');
function updateHue() {
  const hsl = {}; targetColor.getHSL(hsl);
  let d = hsl.h - imageHue; d -= Math.round(d);   // shortest way round the hue circle
  uniforms.uHueShift.value = d * Math.PI * 2;
  uniforms.uColor.value.copy(targetColor);
}
function setColor(hex) { try { targetColor = new THREE.Color(hex); updateHue(); } catch (e) { /* ignore bad colour */ } }
function setColorMode(m) { uniforms.uHolo.value = m === 'original' ? 0 : 1; }

const vert = /* glsl */`
  uniform float uAspect, uTime, uJaw;
  uniform vec4 uMouth; uniform float uChin;
  varying vec2 vUv;
  void main() {
    vUv = uv;
    vec3 p = position;
    // pseudo-3D: the face region bulges towards the camera, so a slight head turn gives parallax
    vec2 c = vec2(uMouth.x, 1.0 - (uMouth.y + uChin) * 0.25 - 0.25);   // rough face centre (uv, y up)
    vec2 d = (uv - c) / vec2(0.32, 0.42);
    p.z += 0.0 * exp(-dot(d, d) * 1.6);   // (no bulge: the picture is never distorted)
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }`;
const frag = /* glsl */`
  uniform sampler2D uTex; uniform float uTime, uJaw, uBlink, uLevel, uDim, uThink, uHueShift, uHolo, uGlitch, uCurve, uChin, uWidth;
  uniform vec4 uEyeL, uEyeR, uMouth; uniform vec3 uColor;
  varying vec2 vUv;
  float hash(float n) { return fract(sin(n) * 43758.5453); }
  // exact HSV hue rotation (a = shift in radians), matches the hue measured on the picture
  vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + 1e-10)), d / (q.x + 1e-10), q.x);
  }
  vec3 hsv2rgb(vec3 c) {
    vec3 p = abs(fract(c.xxx + vec3(1.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0);
    return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);
  }
  vec3 hueRotate(vec3 c, float a) { vec3 h = rgb2hsv(c); h.x = fract(h.x + a / 6.2831853); return hsv2rgb(h); }
  // blink: squash the eye opening towards its centre line, upper/lower lid skin slides in
  vec2 eyeWarp(vec2 p, vec4 e, inout float lidLine) {
    vec2 c = e.xy; float hw = e.z * 1.25, hh = e.w * 1.9;
    float dx = (p.x - c.x) / hw; if (abs(dx) >= 1.0) return p;
    float shape = sqrt(1.0 - dx * dx);                      // lens-shaped opening
    float H = hh * shape + 1e-4;
    float dy = p.y - c.y;
    float open = max(uBlink, 0.0);
    float kx = smoothstep(1.0, 0.75, abs(dx));
    if (abs(dy) > H * 1.9) return p;
    vec2 q = p;
    if (abs(dy) < H * open) q.y = c.y + dy / max(open, 0.02);              // squashed eye
    else {                                                                 // lid skin (from just outside the eye)
      float s = sign(dy); float t = (abs(dy) - H * open) / max(H * 1.9 - H * open, 1e-4);
      q.y = c.y + s * mix(H * 1.05, H * 1.9, t);
    }
    lidLine += kx * (1.0 - open) * exp(-pow(dy / (e.w * 0.35 + 1e-4), 2.0)) * shape;
    return mix(p, q, kx);
  }
  void main() {
    vec2 p = vec2(vUv.x, 1.0 - vUv.y);                      // image coords, y down (like the landmarks)
    // occasional horizontal glitch band
    float band = step(abs(p.y - fract(uTime * 0.37) ), 0.012) * uGlitch;
    p.x += band * 0.012 * sin(uTime * 90.0);
    float lid = 0.0;
    p = eyeWarp(p, uEyeL, lid); p = eyeWarp(p, uEyeR, lid);
    // ---- mouth: a small, rounded opening, symmetric about the mouth midline (centre between the corners).
    // Only the lips move, only vertically and only locally: the lip line splits, the upper lip lifts a bit, the
    // lower lip drops a bit; cheeks, jaw and chin stay untouched. Inside: a soft dark tone taken from the lips.
    vec2 m = uMouth.xy; float mh = uMouth.w;
    float a = uMouth.z * (0.50 + 0.22 * (uWidth - 1.0) / 0.18);   // opening half-width (О/У narrower, Э/И wider)
    float open = clamp(uJaw, 0.0, 1.0);
    float H = open * (mh * 1.3 + uMouth.z * 0.05);              // max opening half-height (modest)
    float dxn = (p.x - m.x) / a;                                 // symmetric: depends on |x - midline| only
    float prof = sqrt(max(0.0, 1.0 - dxn * dxn));                // rounded corners
    prof = prof * prof * (3.0 - 2.0 * prof);
    float lineY = m.y + uCurve * pow((p.x - m.x) / uMouth.z, 2.0);
    float up = H * 0.38 * prof, dn = H * 0.62 * prof;            // upper lip lifts less than the lower drops
    float Z = mh * 2.6 + 0.004;                                  // lip zone that absorbs the movement
    float rel = p.y - lineY;
    vec2 src = p;
    float inner = 0.0, t = 1.0;
    if (rel >= 0.0 && rel < dn + Z) {                            // lower lip
      src.y = rel < dn ? lineY + 0.0012 : lineY + (rel - dn) * (dn + Z) / Z;
      if (rel < dn) { inner = 1.0; t = rel / max(dn, 1e-5); }
    } else if (rel < 0.0 && -rel < up + Z) {                     // upper lip
      float r = -rel;
      src.y = r < up ? lineY - 0.0012 : lineY - (r - up) * (up + Z) / Z;
      if (r < up) { inner = 1.0; t = r / max(up, 1e-5); }
    }
    vec3 col = texture2D(uTex, clamp(src, 0.001, 0.999) * vec2(1.0, -1.0) + vec2(0.0, 1.0)).rgb;
    vec3 lipTone = col;                                          // colour of the lip line (matched to the picture)
    // hologram recolour
    vec3 holo = hueRotate(col, uHueShift);
    col = mix(col, max(holo, 0.0), uHolo);
    if (inner > 0.0) {
      // soft rounded inside: darkened lip tone + a little depth, feathered edges, faint glow on the lip rims
      float feather = (1.0 - smoothstep(0.45, 1.0, t)) * smoothstep(0.0, 0.35, prof) * smoothstep(0.0, 0.25, open);
      vec3 dark = lipTone * 0.18 + vec3(0.010, 0.020, 0.050);
      vec3 depth = dark + lipTone * 0.06 * (1.0 - t);             // a touch lighter in the middle
      col = mix(col, depth, feather * 0.85);
      col += lipTone * 0.35 * exp(-pow((1.0 - t) / 0.22, 2.0)) * smoothstep(0.0, 0.4, prof) * smoothstep(0.0, 0.3, open);
    }
    col += uColor * lid * 0.25;                              // closed-lid line glows faintly
    // eye glow pulse + speech glow
    float lum = dot(col, vec3(0.299, 0.587, 0.114));
    float pulse = 1.0 + uLevel * 0.55 + uThink * 0.12 * sin(uTime * 4.0);
    col *= mix(1.0, pulse, smoothstep(0.35, 0.9, lum));
    for (int i = 0; i < 2; i++) {
      vec4 e = i == 0 ? uEyeL : uEyeR;
      vec2 d = (p - e.xy) / vec2(e.z, e.z);
      col += uColor * exp(-dot(d, d) * 2.5) * (0.04 + 0.2 * uLevel) * uBlink;
    }
    // scanlines + flicker
    float scan = 0.90 + 0.10 * sin(gl_FragCoord.y * 1.7 + uTime * 5.0);
    float flick = 1.0 - 0.05 * step(0.97, hash(floor(uTime * 12.0)));
    col *= scan * flick * uDim;
    col += uColor * band * 0.3;
    // soft vignette so the picture edges never show
    vec2 e2 = smoothstep(vec2(0.0), vec2(0.16), vUv) * smoothstep(vec2(0.0), vec2(0.16), 1.0 - vUv);
    col = max(col - vec3(0.012), 0.0) * e2.x * e2.y;     // also drop the picture's black level
    gl_FragColor = vec4(col, 1.0);
  }`;
const mat = new THREE.ShaderMaterial({ uniforms, vertexShader: vert, fragmentShader: frag });
const plane = new THREE.Mesh(new THREE.PlaneGeometry(1, 1, 48, 48), mat);
scene.add(plane);

// particles at several depths (parallax)
{
  const n = 160, a = new Float32Array(n * 3), ph = new Float32Array(n);
  for (let i = 0; i < n; i++) { a[i * 3] = (Math.random() - 0.5) * 2.2; a[i * 3 + 1] = (Math.random() - 0.5) * 2.4; a[i * 3 + 2] = Math.random() * 0.8 - 0.3; ph[i] = Math.random() * 6.28; }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(a, 3));
  g.setAttribute('phase', new THREE.BufferAttribute(ph, 1));
  const m = new THREE.ShaderMaterial({ uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `uniform float uTime; attribute float phase; varying float vA;
      void main(){ vec3 p = position; p.y = mod(p.y + uTime * 0.02 * (0.5 + fract(phase)) + 1.2, 2.4) - 1.2;
        vA = 0.25 + 0.35 * sin(uTime * 1.5 + phase * 3.0); gl_PointSize = 2.0 + p.z * 2.0;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0); }`,
    fragmentShader: `uniform vec3 uColor; uniform float uDim; varying float vA;
      void main(){ float r = length(gl_PointCoord - 0.5); if (r > 0.5) discard;
        gl_FragColor = vec4(uColor * vA * uDim * (1.0 - r * 2.0), 1.0); }` });
  scene.add(new THREE.Points(g, m));
}

const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.28, 0.35, 0.72);
composer.addPass(bloom);
composer.addPass(new OutputPass());

function fit() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setSize(w, h); composer.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
  // plane fills the view: 1.0 high at distance 2.05 with fov 30 -> visible height 1.10
  const visH = 2 * Math.tan(THREE.MathUtils.degToRad(15)) * 2.05, visW = visH * camera.aspect;
  const a = uniforms.uAspect.value;                 // image width / height
  const s = Math.min(visW / a, visH) * 1.02;
  plane.scale.set(s * a, s, 1);
}
window.addEventListener('resize', fit);

function measureHue(img) {
  // dominant hue = peak of a chroma-weighted hue histogram (rim highlights of another hue do not skew it)
  try {
    const c = document.createElement('canvas'); c.width = c.height = 64;
    const x = c.getContext('2d'); x.drawImage(img, 0, 0, 64, 64);
    const d = x.getImageData(0, 0, 64, 64).data; const bins = new Float32Array(36); const hsl = {}; const col = new THREE.Color();
    for (let i = 0; i < d.length; i += 4) {
      col.setRGB(d[i] / 255, d[i + 1] / 255, d[i + 2] / 255).getHSL(hsl);
      const ch = hsl.s * (1 - Math.abs(2 * hsl.l - 1));
      if (ch > 0.08) bins[Math.floor(hsl.h * 36) % 36] += ch;
    }
    let k = 0; for (let i = 1; i < 36; i++) if (bins[i] > bins[k]) k = i;
    const a = bins[(k + 35) % 36], b = bins[k], cc = bins[(k + 1) % 36];
    const off = (a - cc) / (2 * (a - 2 * b + cc) || 1);          // parabolic refinement
    imageHue = (((k + 0.5 - Math.max(-0.5, Math.min(0.5, off))) / 36) + 1) % 1;
  } catch (e) { imageHue = 0.55; }
}

function setLandmarks(lm) {
  if (!lm) return;
  const [l, r] = lm.eyes;
  uniforms.uEyeL.value.set(l[0], l[1], l[2], l[3]); uniforms.uEyeR.value.set(r[0], r[1], r[2], r[3]);
  // mouth opens symmetrically about the face midline (between the eyes); the detected mouth centre is used only
  // when it is far from it (head turned in the picture)
  const eyeMid = (l[0] + r[0]) / 2;
  const mid = Math.abs(eyeMid - lm.mouth[0]) < lm.mouth[2] * 0.35 ? eyeMid : lm.mouth[0];
  uniforms.uMouth.value.set(mid, lm.mouth[1], lm.mouth[2], lm.mouth[3]);
  uniforms.uCurve.value = lm.mouth[4] || 0; uniforms.uChin.value = lm.chin;
}
function setFace(url, lm) {
  return new Promise((resolve) => {
    new THREE.TextureLoader().load(url, (tex) => {
      tex.colorSpace = THREE.NoColorSpace; tex.minFilter = THREE.LinearFilter; tex.generateMipmaps = false;
      const old = uniforms.uTex.value; uniforms.uTex.value = tex; if (old) old.dispose();
      uniforms.uAspect.value = tex.image.width / tex.image.height;
      if (lm && typeof lm.hue === 'number') imageHue = lm.hue; else measureHue(tex.image);
      updateHue(); setLandmarks(lm); fit();
      window.jarvisReady = true; document.title = 'ready'; resolve(true);
    }, undefined, () => { if (!window.jarvisReady) document.title = 'error'; resolve(false); });
  });
}

const clock = new THREE.Clock();
function frame() {
  const t = S.fixedTime !== null ? S.fixedTime : clock.getElapsedTime();
  uniforms.uTime.value = t;
  let target = 0, wTarget = 0.5;
  if (S.env.length) {
    const i = Math.floor((performance.now() / 1000 - S.envStart) / S.envDt);
    if (i >= 0 && i < S.env.length) { target = S.env[i]; if (S.wid.length > i) wTarget = S.wid[i]; }
    else if (i >= S.env.length) { S.env = []; S.wid = []; }
  }
  if (S.forceMouth !== undefined) target = S.forceMouth;
  if (S.forceWidth !== undefined) wTarget = S.forceWidth;
  // time-based attack/release smoothing (frame-rate independent): opens fast, closes softly
  const now = performance.now() / 1000; const dt = S.lastT === null ? 1 / 30 : Math.min(0.2, now - S.lastT); S.lastT = now;
  const still = S.forceMouth !== undefined && S.fixedTime !== null;
  const ka = still ? 1 : 1 - Math.exp(-dt / 0.045), kr = still ? 1 : 1 - Math.exp(-dt / 0.11);
  S.mouth += (target - S.mouth) * (target > S.mouth ? ka : kr);
  S.width += (wTarget - S.width) * (still ? 1 : 1 - Math.exp(-dt / 0.08));
  uniforms.uJaw.value = Math.min(1, Math.max(0, S.mouth));
  // О/У (low width) narrower, И/Э/А (high width) wider; only while the mouth is open
  uniforms.uWidth.value = 1 + (S.width - 0.5) * 0.36 * Math.min(1, S.mouth * 2.5);
  if (S.forceBlink !== undefined) S.blink = S.forceBlink;
  else if (S.fixedTime === null) {
    if (t > S.nextBlink) { S.blinkStart = t; S.nextBlink = t + 2.5 + Math.random() * 3.5; }
    const d = t - S.blinkStart; S.blink = d >= 0 && d < 0.2 ? 1 - Math.sin(d / 0.2 * Math.PI) : 1;
    if (t > S.nextGlitch) { S.glitch = 1; S.nextGlitch = t + 6 + Math.random() * 10; }
    S.glitch *= 0.9;
  }
  uniforms.uBlink.value = S.blink; uniforms.uGlitch.value = S.glitch;
  S.micS += (S.mic - S.micS) * 0.3;
  const st = S.state;
  uniforms.uLevel.value = st === 'listening' ? S.micS : (st === 'speaking' ? S.mouth * 0.6 : 0);
  uniforms.uDim.value += ((st === 'muted' ? 0.45 : 1) - uniforms.uDim.value) * 0.1;
  uniforms.uThink.value += ((st === 'thinking' || st === 'loading' ? 1 : 0) - uniforms.uThink.value) * 0.05;
  // idle: breathing scale + slight head sway (parallax through the bulge)
  const br = 1 + 0.008 * Math.sin(t * 1.25);
  // gentle float: whole-image transform only (tiny tilt, drift and breathing scale), no warping
  plane.rotation.z = 0.008 * Math.sin(t * 0.37);
  plane.position.x = 0.004 * Math.sin(t * 0.23); plane.position.y = 0.006 * Math.sin(t * 0.8) - (st === 'thinking' ? 0.004 : 0);
  camera.zoom = br; camera.updateProjectionMatrix();
  composer.render();
}
let timer = null;
function setQuality(q) {
  S.quality = q; bloom.enabled = q !== 'off';
  renderer.setPixelRatio(q === 'full' ? Math.min(window.devicePixelRatio, 1.5) : 1); fit();
  if (timer) clearInterval(timer);
  timer = setInterval(frame, 1000 / ({ full: 30, lite: 20, off: 8 }[q] || 30));
}
setQuality(S.quality);

window.jarvis = {
  setState(s) { S.state = s; document.getElementById('label').textContent = LABELS[s] || ''; },
  setColor, setColorMode, setFace, setLandmarks,
  setMic(v) { S.mic = Math.max(0, Math.min(1, v)); },
  speak(env, dt, wid) { S.env = env || []; S.wid = wid || []; S.envDt = dt || 0.02; S.envStart = performance.now() / 1000; S.state = 'speaking'; },
  stopSpeech() { S.env = []; S.wid = []; },
  setQuality, setGaze() { /* painted eyes do not move */ },
  pose(mouth, blink, width) { S.forceMouth = mouth; S.forceBlink = blink; S.forceWidth = width; },
};
window.jarvis.setState(S.state);
setColor(params.get('color') ? '#' + params.get('color') : '#46dcff');
if (params.get('colors') === 'original') setColorMode('original');
if (params.has('mouth') || params.has('blink')) window.jarvis.pose(params.has('mouth') ? parseFloat(params.get('mouth')) : undefined,
  params.has('blink') ? parseFloat(params.get('blink')) : undefined, params.has('width') ? parseFloat(params.get('width')) : undefined);
// initial face: ?img=<url>&lm=<json>, else the bundled default (landmarks from faces/default.json)
(async () => {
  let lm = null;
  try { lm = params.has('lm') ? JSON.parse(params.get('lm')) : await (await fetch('./faces/default.json')).json(); } catch (e) { lm = null; }
  if (params.get('img') || !params.has('wait')) await setFace(params.get('img') || './faces/default.jpg', lm);
})();
if (params.has('capture')) {
  const wait = () => {
    if (!window.jarvisReady) return setTimeout(wait, 100);
    setTimeout(() => { frame(); const pre = document.createElement('pre'); pre.id = 'shot';
      pre.textContent = renderer.domElement.toDataURL('image/png'); document.body.appendChild(pre); }, 1200);
  };
  wait();
}
