// Jarvis hologram head: real head scan (LeePerrySmith, CC BY 3.0) + custom hologram shader + bloom.
// API for the host app: window.jarvis.{setState, setColor, setMic, speak, stopSpeech, setQuality, setGaze}
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

// shader colours are authored as display values: no sRGB re-encoding (keeps the background black, colours saturated)
THREE.ColorManagement.enabled = false;
const params = new URLSearchParams(location.search);
// ?still=1: frontal, symmetric, static portrait (used to render the bundled default face image)
const STILL = params.has('still');
const LABELS = { sleeping: 'СПЛЮ', listening: 'СЛУШАЮ', thinking: 'ДУМАЮ', muted: 'МИКРОФОН ВЫКЛ', loading: 'ЗАГРУЗКА' };
const S = {
  state: params.get('state') || 'loading', mic: 0, micS: 0,
  env: [], envDt: 0.02, envStart: 0, mouth: 0,
  blink: 1, nextBlink: 2, blinkStart: -10,
  gaze: new THREE.Vector2(), gazeT: new THREE.Vector2(), nextSaccade: 0,
  fixedTime: params.has('t') ? parseFloat(params.get('t')) : null,
  quality: params.get('quality') || 'full',
  yaw: params.has('yaw') ? parseFloat(params.get('yaw')) : null,   // fixed head turn (tests/screenshots)
};

// any failure before the model is ready -> title 'error' so the host app switches to the 2D head at once
window.addEventListener('error', () => { if (!window.jarvisReady) document.title = 'error'; });
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'low-power', preserveDrawingBuffer: true });
} catch (e) { document.title = 'error'; throw e; }
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
renderer.outputColorSpace = THREE.LinearSRGBColorSpace;
renderer.setClearColor(0x04070d, 1);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(24, 1, 0.1, 100);
camera.position.set(0, 1.5, 15.5);
camera.lookAt(0, 1.45, 0);
if (STILL) { camera.position.set(-0.11, 1.45, 15.5); camera.lookAt(-0.11, 1.45, 0); }   // scan midline is x = -0.11

// ------------------------------------------------------------------ colours (main + complementary accent)
const uniforms = {
  uTime: { value: 0 }, uColor: { value: new THREE.Color('#46dcff') }, uAccent: { value: new THREE.Color('#b45cff') },
  uJaw: { value: 0 }, uBlink: { value: 1 }, uGaze: { value: new THREE.Vector2() }, uLevel: { value: 0 },
  uDim: { value: 1 }, uThink: { value: 0 }, uCircuitSpeed: { value: 1 }, uStill: { value: STILL ? 1 : 0 },
};
function setColor(hex) {
  const c = new THREE.Color(hex);
  uniforms.uColor.value.copy(c);
  const hsl = {}; c.getHSL(hsl);
  uniforms.uAccent.value.setHSL((hsl.h + 0.30) % 1, Math.min(1, hsl.s * 0.9 + 0.1), 0.62);
}
setColor(params.get('color') ? '#' + params.get('color') : '#46dcff');

// ------------------------------------------------------------------ shaders
const common = /* glsl */`
  uniform float uTime, uJaw, uBlink, uLevel, uDim, uThink, uCircuitSpeed, uStill;
  uniform vec3 uColor, uAccent; uniform vec2 uGaze;
  // eye centres measured on the scan (face midline is x = -0.11, not 0)
  const vec3 EYE_L = vec3(-0.72, 1.69, 1.90);
  const vec3 EYE_R = vec3( 0.50, 1.69, 1.90);
  const float MOUTH_Y = 0.38;
  float cutY(float z) { return -0.80 + 0.40 * (1.6 - z); }   // plane under the jaw: no neck
  // jaw weight: sharp split at the lips in the middle of the mouth, softer towards the cheeks
  float jawW(vec3 p) {
    float band = mix(0.05, 0.22, smoothstep(0.25, 0.75, abs(p.x)));
    return smoothstep(MOUTH_Y + 0.01, MOUTH_Y + 0.01 - band, p.y) * smoothstep(-0.9, 0.6, p.z)
         * (1.0 - smoothstep(1.35, 1.9, abs(p.x)));
  }
`;
const vert = /* glsl */`
  ${common}
  varying vec3 vPos; varying vec3 vN; varying vec3 vViewN; varying vec3 vViewDir; varying float vJw; varying vec3 vRest;
  void main() {
    vec3 p = position; vec3 n = normal;
    // jaw: rotate the lower face around a hinge in front of the ears
    float w = jawW(p);
    float a = uJaw * 0.20 * w;
    vec3 hinge = vec3(0.0, 1.05, -0.35);
    vec3 q = p - hinge;
    float ca = cos(a), sa = sin(a);
    p = hinge + vec3(q.x, ca * q.y - sa * q.z, sa * q.y + ca * q.z);
    n = vec3(n.x, ca * n.y - sa * n.z, sa * n.y + ca * n.z);
    // eyelids: upper lid skin slides down over the eye while blinking
    for (int i = 0; i < 2; i++) {
      vec3 e = i == 0 ? EYE_L : EYE_R;
      vec2 d = (p.xy - e.xy) / vec2(0.34, 0.24);
      float lid = exp(-dot(d, d) * 2.0) * step(e.y - 0.02, p.y) * step(1.2, p.z);
      p.y -= (1.0 - uBlink) * 0.15 * lid;
      p.z += (1.0 - uBlink) * 0.035 * lid;
    }
    vPos = p; vN = normalize(n); vJw = w; vRest = position;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vViewN = normalize(normalMatrix * n);
    vViewDir = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;
const frag = /* glsl */`
  ${common}
  varying vec3 vPos; varying vec3 vN; varying vec3 vViewN; varying vec3 vViewDir; varying float vJw; varying vec3 vRest;
  vec3 hash3(vec3 p) {
    p = vec3(dot(p, vec3(127.1, 311.7, 74.7)), dot(p, vec3(269.5, 183.3, 246.1)), dot(p, vec3(113.5, 271.9, 124.6)));
    return fract(sin(p) * 43758.5453);
  }
  // cellular noise: x = F1, y = F2
  vec2 voronoi(vec3 x) {
    vec3 i = floor(x), f = fract(x); vec2 r = vec2(8.0);
    for (int k = -1; k <= 1; k++) for (int j = -1; j <= 1; j++) for (int h = -1; h <= 1; h++) {
      vec3 g = vec3(float(h), float(j), float(k));
      vec3 o = hash3(i + g);
      float d = length(g + o - f);
      if (d < r.x) { r.y = r.x; r.x = d; } else if (d < r.y) r.y = d;
    }
    return r;
  }
  float eyeGlow(vec3 e, out float ringOut) {
    vec2 d = vPos.xy - e.xy - uGaze * vec2(0.07, 0.04);
    d.y /= max(uBlink, 0.06);
    float r = length(d);
    float front = smoothstep(0.35, 0.0, abs(vPos.z - e.z)) * step(0.0, vN.z);
    float ring = smoothstep(0.022, 0.0, abs(r - 0.105)) + 0.6 * smoothstep(0.018, 0.0, abs(r - 0.155));
    float pupil = smoothstep(0.055, 0.035, r);
    float iris = smoothstep(0.14, 0.06, r) * 0.55;
    ringOut = ring * front;
    return (iris * 0.35 + pupil * 1.2 + exp(-r * 16.0) * 0.25) * front * smoothstep(0.05, 0.25, uBlink);
  }
  void main() {
    float cut = cutY(vRest.z);           // cut in rest pose: the chin moves with the jaw
    if (vRest.y < cut) discard;
    float fade = smoothstep(cut, cut + 0.75, vRest.y);
    vec3 N = normalize(vViewN), V = normalize(vViewDir);
    float ndv = clamp(dot(N, V), 0.0, 1.0);
    float fres = pow(1.0 - ndv, 2.4);
    vec3 L1 = normalize(mix(vec3(-0.5, 0.65, 0.6), vec3(0.0, 0.55, 0.85), uStill));   // still: symmetric front light
    vec3 L2 = normalize(vec3(0.85, -0.2, 0.25));
    float key = max(dot(N, L1), 0.0);
    float side = mix(pow(max(dot(N, L2), 0.0), 2.0), pow(abs(N.x), 3.0) * 0.6, uStill);
    vec3 white = vec3(0.85, 0.97, 1.0);
    // translucent body with volumetric key light + subsurface-like inner glow
    vec3 deep = mix(uColor * 0.5, uColor * vec3(0.35, 0.55, 1.0), 0.6);
    vec3 col = deep * (0.10 + 0.30 * key * key) + uColor * 0.06 * ndv;
    col += mix(uColor, uAccent, 0.15) * 0.07 * pow(ndv, 4.0);
    // rims: bright cyan/white + softer magenta/violet secondary on the opposite edges
    col += mix(uColor, white, mix(0.40, 0.25, uStill)) * fres * mix(0.85, 0.5, uStill);
    // secondary magenta/violet rim, strongest on the edges facing away from the key light
    float away = mix(smoothstep(-0.3, 0.7, dot(N, normalize(vec3(0.9, -0.35, 0.0)))), smoothstep(0.2, 0.9, abs(N.x)) * 0.8, uStill);
    col += uAccent * (fres * (0.45 + 0.6 * away) + side * 0.4) * (0.75 + 0.35 * uThink);
    // brain circuits through the cranium (animated veins + central fissure)
    float t = uTime * uCircuitSpeed;
    float cran = smoothstep(2.25, 2.85, vPos.y) * smoothstep(-1.0, 0.4, vPos.z);
    vec3 bp = vPos * 1.35 + vec3(0.0, t * 0.04, 0.0);
    bp += 0.35 * vec3(sin(vPos.y * 2.1 + t * 0.3), sin(vPos.z * 1.7), sin(vPos.x * 2.3 - t * 0.2));
    vec2 vb = voronoi(bp);
    float vein = smoothstep(0.035, 0.0, vb.y - vb.x);
    vec2 vb2 = voronoi(bp * 2.3 + 7.0);
    vein += 0.55 * smoothstep(0.03, 0.0, vb2.y - vb2.x);
    float pulse = 0.55 + 0.45 * sin(vPos.y * 5.0 - t * 2.4 + vPos.x * 2.0);
    float fissure = exp(-pow(vPos.x / 0.035, 2.0)) * smoothstep(2.3, 3.0, vPos.y) * smoothstep(-1.4, 1.2, vPos.z);
    col += mix(uColor, white, 0.25) * (vein * pulse * 0.6 + fissure * 0.7) * cran;
    // line-and-node network over cheeks and jaw
    float cheek = smoothstep(0.35, 0.95, abs(vPos.x)) * smoothstep(1.55, 1.05, vPos.y) * step(0.3, vN.z + 0.5);
    vec2 vn = voronoi(vPos * vec3(1.9, 1.9, 1.9) + 3.0);
    float net = smoothstep(0.035, 0.0, vn.y - vn.x) * 0.55 + smoothstep(0.09, 0.0, vn.x) * 1.4;
    col += mix(uColor, uAccent, 0.35) * net * cheek * (0.75 + 0.35 * sin(t * 1.3 + vPos.y * 3.0));
    // glowing eyes with iris rings
    float rl, rr;
    float eg = eyeGlow(EYE_L, rl) + eyeGlow(EYE_R, rr);
    float sock = 0.0;
    for (int i = 0; i < 2; i++) { vec2 d = (vPos.xy - (i == 0 ? EYE_L : EYE_R).xy) / vec2(0.36, 0.22); sock += exp(-dot(d, d) * 1.5); }
    col *= 1.0 - 0.45 * sock * step(1.3, vPos.z);   // deep eye sockets
    col += mix(uColor, white, 0.75) * eg * 0.8 + mix(uColor, white, 0.55) * (rl + rr) * 1.1 * smoothstep(0.05, 0.3, uBlink);
    // closed-lid line when blinking
    // soft mouth line
    float mouth = exp(-pow((vPos.y - MOUTH_Y + 0.02 + uJaw * 0.05) / 0.035, 2.0)) * smoothstep(0.5, 0.15, abs(vPos.x))
                * step(1.6, vPos.z);
    col += mix(uColor, white, 0.5) * mouth * 0.35 * (1.0 - uJaw);
    float mreg = smoothstep(0.62, 0.30, abs(vPos.x)) * step(1.4, vPos.z);
    float gap = smoothstep(0.08, 0.3, vJw) * smoothstep(0.92, 0.7, vJw) * mreg * smoothstep(0.05, 0.3, uJaw);
    float lips = (exp(-pow((vJw - 0.12) / 0.05, 2.0)) + exp(-pow((vJw - 0.88) / 0.05, 2.0))) * mreg * smoothstep(0.05, 0.3, uJaw);
    col = mix(col, deep * 0.04, gap * 0.9) + mix(uColor, white, 0.5) * lips * 0.45;
    // listening: rim reacts to the mic level
    col += mix(uColor, white, 0.3) * fres * uLevel * 1.4;
    // faint moving scanlines
    float scan = mix(0.88 + 0.12 * sin(gl_FragCoord.y * 1.6 + uTime * 6.0), 1.0, uStill);
    col *= scan * uDim;
    float alpha = fade;
    gl_FragColor = vec4(col * fade, alpha);
    #ifdef DEBUG_JAW
    gl_FragColor = vec4(vJw, gap, lips, 1.0);
    #endif
  }
`;
const headMat = new THREE.ShaderMaterial({ uniforms, vertexShader: vert, fragmentShader: frag, defines: params.has('debugjaw') ? { DEBUG_JAW: 1 } : {}, transparent: true,
  blending: THREE.AdditiveBlending, depthWrite: false, depthTest: true });
// depth pre-pass: only the front-most surface is lit (reads as a solid translucent volume, not a jumble of layers)
const depthMat = new THREE.ShaderMaterial({ uniforms, vertexShader: vert,
  fragmentShader: `${common} varying vec3 vPos; varying vec3 vN; varying vec3 vViewN; varying vec3 vViewDir; varying float vJw; varying vec3 vRest;
    void main(){ if (vRest.y < cutY(vRest.z)) discard; gl_FragColor = vec4(0.0); }`, colorWrite: false });

const pointsVert = /* glsl */`
  ${common}
  varying float vA; varying vec3 vPos2;
  void main() {
    vec3 p = position;
    float w = jawW(p);
    float a = uJaw * 0.20 * w; vec3 hinge = vec3(0.0, 1.05, -0.35); vec3 q = p - hinge;
    p = hinge + vec3(q.x, cos(a) * q.y - sin(a) * q.z, sin(a) * q.y + cos(a) * q.z);
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vec3 n = normalize(normalMatrix * normal);
    float ndv = clamp(dot(n, normalize(-mv.xyz)), 0.0, 1.0);
    vA = pow(1.0 - ndv, 1.5) * step(0.05, ndv) * (0.5 + 0.5 * sin(uTime * 2.0 + position.x * 9.0 + position.y * 7.0));
    vPos2 = position;
    gl_PointSize = 1.6;
    gl_Position = projectionMatrix * mv;
  }`;
const pointsMat = new THREE.ShaderMaterial({ uniforms, vertexShader: pointsVert, transparent: true, depthWrite: false,
  blending: THREE.AdditiveBlending,
  fragmentShader: `${common} varying float vA; varying vec3 vPos2;
    void main(){ float c = cutY(vPos2.z); if (vPos2.y < c) discard;
      gl_FragColor = vec4(mix(uColor, vec3(1.0), 0.4) * vA * 0.55 * uDim * smoothstep(c, c + 0.75, vPos2.y), 1.0); }` });

const pivot = new THREE.Group();
scene.add(pivot);
new GLTFLoader().load('./models/LeePerrySmith.glb', (gltf) => {
  let mesh = null;
  gltf.scene.traverse((o) => { if (o.isMesh && !mesh) mesh = o; });
  const geo = mesh.geometry;
  const head = new THREE.Group();
  const pre = new THREE.Mesh(geo, depthMat); pre.renderOrder = 0;
  const holo = new THREE.Mesh(geo, headMat); holo.renderOrder = 1;
  // sparse point overlay (every 3rd vertex)
  const pos = geo.attributes.position, nrm = geo.attributes.normal;
  const sp = [], sn = [];
  for (let i = 0; i < pos.count; i += 3) { sp.push(pos.getX(i), pos.getY(i), pos.getZ(i)); sn.push(nrm.getX(i), nrm.getY(i), nrm.getZ(i)); }
  const pg = new THREE.BufferGeometry();
  pg.setAttribute('position', new THREE.Float32BufferAttribute(sp, 3));
  pg.setAttribute('normal', new THREE.Float32BufferAttribute(sn, 3));
  const pts = new THREE.Points(pg, pointsMat); pts.renderOrder = 2;
  head.add(pre, holo, pts);
  head.position.set(0, -1.45, -0.2);          // rotate around the middle of the head
  pivot.position.set(0, 1.45, 0.2);
  pivot.add(head);
  window.jarvisReady = true;
  document.title = 'ready';
}, undefined, (e) => { document.title = 'error'; console.error(e); });

// sparse floating particles
{
  const n = 140, a = new Float32Array(n * 3), ph = new Float32Array(n);
  for (let i = 0; i < n; i++) { a[i * 3] = (Math.random() - 0.5) * 12; a[i * 3 + 1] = Math.random() * 8 - 2.5; a[i * 3 + 2] = (Math.random() - 0.5) * 6 - 2; ph[i] = Math.random() * 6.28; }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(a, 3));
  g.setAttribute('phase', new THREE.BufferAttribute(ph, 1));
  const m = new THREE.ShaderMaterial({ uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `uniform float uTime; attribute float phase; varying float vA;
      void main(){ vec3 p = position; p.y = mod(p.y + uTime * 0.08 * (0.5 + fract(phase)) + 2.5, 8.0) - 2.5;
        vA = 0.25 + 0.35 * sin(uTime * 1.5 + phase * 3.0); vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_PointSize = 2.2; gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `uniform vec3 uColor; uniform float uDim; varying float vA;
      void main(){ vec2 d = gl_PointCoord - 0.5; float r = length(d); if (r > 0.5) discard;
        gl_FragColor = vec4(uColor * vA * uDim * (1.0 - r * 2.0), 1.0); }` });
  if (!STILL) scene.add(new THREE.Points(g, m));
}

// ------------------------------------------------------------------ post-processing (bloom)
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.50, 0.30, 0.30);
composer.addPass(bloom);
composer.addPass(new OutputPass());

function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setSize(w, h); composer.setSize(w, h);
  camera.aspect = w / h;
  camera.fov = w / h < 0.9 ? 24 / (w / h) * 0.9 : 24;   // keep the whole head visible in narrow panels
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize); resize();

// ------------------------------------------------------------------ animation
const clock = new THREE.Clock();
function frame() {
  const t = S.fixedTime !== null ? S.fixedTime : clock.getElapsedTime();
  uniforms.uTime.value = t;
  // lip sync: envelope played against our own clock
  let target = 0;
  if (S.env.length) {
    const i = Math.floor((performance.now() / 1000 - S.envStart) / S.envDt);
    if (i >= 0 && i < S.env.length) target = S.env[i]; else if (i >= S.env.length) S.env = [];
  }
  if (S.forceMouth !== undefined) target = S.forceMouth;
  S.mouth += (target - S.mouth) * (target > S.mouth ? 0.55 : 0.3);
  uniforms.uJaw.value = Math.min(1, S.mouth);
  // blink + saccades
  if (S.fixedTime === null) {
    if (t > S.nextBlink) { S.blinkStart = t; S.nextBlink = t + 2.5 + Math.random() * 3.5; }
    const d = t - S.blinkStart; S.blink = d >= 0 && d < 0.18 ? 1 - Math.sin(d / 0.18 * Math.PI) : 1;
    if (t > S.nextSaccade) { S.gazeT.set((Math.random() - 0.5) * 1.4, (Math.random() - 0.5) * 0.8); S.nextSaccade = t + 0.8 + Math.random() * 2.2; }
    S.gaze.lerp(S.gazeT, 0.15);
  }
  uniforms.uBlink.value = S.blink; uniforms.uGaze.value.copy(S.gaze);
  S.micS += (S.mic - S.micS) * 0.3;
  const st = S.state;
  uniforms.uLevel.value = st === 'listening' ? S.micS : (st === 'speaking' ? S.mouth * 0.25 : 0);
  uniforms.uDim.value += ((st === 'muted' ? 0.4 : 1) - uniforms.uDim.value) * 0.1;
  uniforms.uThink.value += ((st === 'thinking' || st === 'loading' ? 1 : 0) - uniforms.uThink.value) * 0.05;
  uniforms.uCircuitSpeed.value = 1 + uniforms.uThink.value * 2.5;
  // slow idle head motion with a slight 3/4 angle
  pivot.rotation.y = S.yaw !== null ? S.yaw : 0.20 * Math.sin(t * 0.27) + 0.06 + S.gaze.x * 0.03;
  pivot.rotation.x = STILL ? 0 : 0.05 * Math.sin(t * 0.21) - (st === 'thinking' ? 0.06 : 0) + 0.04;
  pivot.position.y = STILL ? 1.45 : 1.45 + 0.04 * Math.sin(t * 0.8);
  composer.render();
}
let timer = null;
function setQuality(q) {
  S.quality = q;
  bloom.enabled = q !== 'off';
  renderer.setPixelRatio(q === 'full' ? Math.min(window.devicePixelRatio, 1.5) : 1);
  resize();
  if (timer) clearInterval(timer);
  timer = null;
  if (S.paused) return;                       // 1.0.8: window hidden / in tray -> no rendering at all
  let full = Math.max(10, Math.min(30, S.maxFps || 30));     // fps cap 30 («Нагрузка»)
  if (S.state === 'sleeping' || S.state === 'muted') full = Math.min(full, 15);   // idle: slow breathing only
  timer = setInterval(frame, 1000 / ({ full: full, lite: Math.min(20, full), off: 8 }[q] || full));
}
function setPaused(p) { S.paused = !!p; setQuality(S.quality); }
function setMaxFps(n) { S.maxFps = n; setQuality(S.quality); }
setQuality(S.quality);

window.jarvis = {
  setPaused, setMaxFps,
  setState(s) { const idle = (x) => x === 'sleeping' || x === 'muted'; const ch = idle(s) !== idle(S.state);
    S.state = s; document.getElementById('label').textContent = LABELS[s] || ''; if (ch) setQuality(S.quality); },
  setColor, setMic(v) { S.mic = Math.max(0, Math.min(1, v)); },
  speak(env, dt) { S.env = env || []; S.envDt = dt || 0.02; S.envStart = performance.now() / 1000; S.state = 'speaking'; },
  stopSpeech() { S.env = []; },
  setQuality, setGaze(x, y) { S.gazeT.set(x, y); },
  pose(mouth, blink) { S.forceMouth = mouth; S.blink = blink; },   // for screenshots/tests
};
window.jarvis.setState(S.state);
if (params.has('mouth')) window.jarvis.pose(parseFloat(params.get('mouth')), parseFloat(params.get('blink') || '1'));
// ?capture=1: after the model is loaded, store a PNG of the canvas in the DOM (used by headless screenshot tests)
if (params.has('capture')) {
  const wait = () => {
    if (!window.jarvisReady) return setTimeout(wait, 100);
    setTimeout(() => { frame(); const pre = document.createElement('pre'); pre.id = 'shot';
      pre.textContent = renderer.domElement.toDataURL('image/png'); document.body.appendChild(pre); }, 1500);
  };
  wait();
}
