/* Motion Capture — the whole pipeline runs in this tab, free and local:
 *
 *   MediaPipe (face / pose / hands)  →  Kalidokit solvers  →  VRM avatar
 *   →  composited over the footage (or webcam / green / solid color)
 *   →  recorded with MediaRecorder  →  POST /api/mocap/rec  →  ffmpeg mp4.
 *
 * Two modes:
 *   video — upload footage of a person; the avatar copies their body + lips,
 *           stuck on the video; the original audio is muxed back server-side.
 *   live  — webcam drives the avatar in real time; record with mic audio.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";
import { FilesetResolver, FaceLandmarker, PoseLandmarker, HandLandmarker }
  from "/static/vendor/mediapipe/vision_bundle.mjs";

const K = window.Kalidokit;
const $ = VS.$;

const S = {
  mode: "video",                    // "video" | "live" | "mirror" (Avatar Creator)
  vrm: null, avatarUrl: null,
  source: null,                     // {name, url} — uploaded footage
  mirror: false,
  track: { body: true, hands: true, face: true },
  snap: 0.65,                       // rig lerp per frame (1 - smoothing)
  bg: "footage", bgColor: "#101522",
  av: { x: 0, y: 0, scale: 1 },     // avatar layer offset + scale on the stage
  running: false, recording: false, recMode: "", instant: false,
  trackers: {}, fileset: null, lastTs: 0, lastFace: null,
  person: null, personImg: null,
  camStream: null, recorder: null, chunks: [],
};

// ---------------------------------------------------------------- three scene
const stage = $("#stage");
const ctx = stage.getContext("2d");
const glCanvas = document.createElement("canvas");
const renderer = new THREE.WebGLRenderer({ canvas: glCanvas, alpha: true, antialias: true });
renderer.setClearColor(0x000000, 0);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 16 / 9, 0.1, 20);
camera.position.set(0, 1.0, 3.4);
camera.lookAt(0, 1.0, 0);
scene.add(new THREE.AmbientLight(0xffffff, Math.PI * 0.4));
const sun = new THREE.DirectionalLight(0xffffff, Math.PI * 0.9);
sun.position.set(0.5, 1.5, 1.5);
scene.add(sun);
const clock = new THREE.Clock();

const srcVideo = document.createElement("video");
srcVideo.playsInline = true; srcVideo.crossOrigin = "anonymous";
const camVideo = document.createElement("video");
camVideo.playsInline = true; camVideo.muted = true;

function setStageSize(w, h) {
  w = Math.round(w); h = Math.round(h);
  const cap = 1920 / Math.max(w, h);
  if (cap < 1) { w = Math.round(w * cap); h = Math.round(h * cap); }
  w -= w % 2; h -= h % 2;
  if (stage.width === w && stage.height === h) return;
  stage.width = w; stage.height = h;
  glCanvas.width = w; glCanvas.height = h;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

// ---------------------------------------------------------------- avatar (VRM)
async function loadAvatar(url) {
  status("loading avatar…");
  const loader = new GLTFLoader();
  loader.register(p => new VRMLoaderPlugin(p));
  const gltf = await loader.loadAsync(url);
  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error("that file is not a VRM avatar");
  VRMUtils.rotateVRM0(vrm);               // VRM0 models face away — turn them around
  if (S.vrm) { scene.remove(S.vrm.scene); VRMUtils.deepDispose(S.vrm.scene); }
  S.vrm = vrm; S.avatarUrl = url;
  scene.add(vrm.scene);
  status("avatar ready");
  $("#stageEmpty").style.display = "none";
  drawIdle();
}

// ---------------------------------------------------------------- trackers
async function ensureTrackers() {
  if (!S.fileset) {
    status("loading tracking engine…");
    S.fileset = await FilesetResolver.forVisionTasks("/static/vendor/mediapipe/wasm");
  }
  const mk = async (cls, model, opts) => {
    const base = { baseOptions: { modelAssetPath: model, delegate: "GPU" },
                   runningMode: "VIDEO", ...opts };
    try { return await cls.createFromOptions(S.fileset, base); }
    catch (e) {                              // 4GB card busy? fall back to CPU wasm
      base.baseOptions.delegate = "CPU";
      return await cls.createFromOptions(S.fileset, base);
    }
  };
  const mirror = S.mode === "mirror";        // Avatar Creator needs only the face
  if (!mirror && S.track.body && !S.trackers.pose) {
    status("loading body model…");
    S.trackers.pose = await mk(PoseLandmarker,
      "/static/models/mediapipe/pose_landmarker_full.task", { numPoses: 1 });
  }
  if (S.track.face && !S.trackers.face) {
    status("loading face model…");
    S.trackers.face = await mk(FaceLandmarker,
      "/static/models/mediapipe/face_landmarker.task", { numFaces: 1 });
  }
  if (!mirror && S.track.hands && !S.trackers.hand) {
    status("loading hand model…");
    S.trackers.hand = await mk(HandLandmarker,
      "/static/models/mediapipe/hand_landmarker.task", { numHands: 2 });
  }
  status("tracking ready");
}

// ---------------------------------------------------------------- rigging
function boneName(kalidoKey) {
  // Kalidokit "LeftThumbProximal" → three-vrm normalized "leftThumbMetacarpal":
  // kalidokit speaks VRM0 thumb names, the normalized humanoid speaks VRM1.
  let n = kalidoKey.charAt(0).toLowerCase() + kalidoKey.slice(1);
  return n.replace("ThumbProximal", "ThumbMetacarpal")
          .replace("ThumbIntermediate", "ThumbProximal");
}

const _euler = new THREE.Euler();
const _quat = new THREE.Quaternion();
const _vec = new THREE.Vector3();

function rigRotation(name, rot, damp = 1, amt = S.snap) {
  if (!S.vrm || !rot) return;
  const node = S.vrm.humanoid.getNormalizedBoneNode(boneName(name));
  if (!node) return;
  _euler.set(rot.x * damp, rot.y * damp, rot.z * damp, rot.rotationOrder || "XYZ");
  _quat.setFromEuler(_euler);
  node.quaternion.slerp(_quat, amt);
}

function rigPosition(name, pos, damp = 1, amt = 0.07) {
  if (!S.vrm || !pos) return;
  const node = S.vrm.humanoid.getNormalizedBoneNode(boneName(name));
  if (!node) return;
  _vec.set(pos.x * damp, pos.y * damp, pos.z * damp);
  node.position.lerp(_vec, amt);
}

function rigFace(rf) {
  if (!rf) return;
  rigRotation("Neck", rf.head, 0.7);
  const em = S.vrm && S.vrm.expressionManager;
  if (!em) return;
  const prev = em.getValue("blink") || 0;
  let eye = {
    l: K.Vector.lerp(K.Utils.clamp(1 - rf.eye.l, 0, 1), prev, 0.5),
    r: K.Vector.lerp(K.Utils.clamp(1 - rf.eye.r, 0, 1), prev, 0.5),
  };
  eye = K.Face.stabilizeBlink(eye, rf.head.y);
  em.setValue("blink", eye.l);
  const m = rf.mouth.shape;
  em.setValue("aa", K.Vector.lerp(m.A, em.getValue("aa") || 0, 0.4));
  em.setValue("ih", K.Vector.lerp(m.I, em.getValue("ih") || 0, 0.4));
  em.setValue("ou", K.Vector.lerp(m.U, em.getValue("ou") || 0, 0.4));
  em.setValue("ee", K.Vector.lerp(m.E, em.getValue("ee") || 0, 0.4));
  em.setValue("oh", K.Vector.lerp(m.O, em.getValue("oh") || 0, 0.4));
}

function rigPose(rp) {
  if (!rp) return;
  rigRotation("Hips", rp.Hips.rotation, 0.7);
  rigPosition("Hips",
    { x: rp.Hips.position.x, y: rp.Hips.position.y + 1, z: -rp.Hips.position.z }, 1, 0.07);
  rigRotation("Chest", rp.Spine, 0.25, S.snap * 0.8);
  rigRotation("Spine", rp.Spine, 0.45, S.snap * 0.8);
  rigRotation("RightUpperArm", rp.RightUpperArm);
  rigRotation("RightLowerArm", rp.RightLowerArm);
  rigRotation("LeftUpperArm", rp.LeftUpperArm);
  rigRotation("LeftLowerArm", rp.LeftLowerArm);
  rigRotation("RightUpperLeg", rp.RightUpperLeg);
  rigRotation("RightLowerLeg", rp.RightLowerLeg);
  rigRotation("LeftUpperLeg", rp.LeftUpperLeg);
  rigRotation("LeftLowerLeg", rp.LeftLowerLeg);
}

const FINGERS = ["Wrist", "ThumbProximal", "ThumbIntermediate", "ThumbDistal",
  "IndexProximal", "IndexIntermediate", "IndexDistal",
  "MiddleProximal", "MiddleIntermediate", "MiddleDistal",
  "RingProximal", "RingIntermediate", "RingDistal",
  "LittleProximal", "LittleIntermediate", "LittleDistal"];

function rigHand(side, rh, rp) {
  if (!rh) return;
  const wrist = rh[side + "Wrist"];
  const poseHand = rp && rp[side + "Hand"];
  if (wrist) {
    rigRotation(side + "Hand",
      { x: wrist.x, y: wrist.y, z: poseHand ? poseHand.z : wrist.z });
  }
  for (const f of FINGERS) {
    if (f === "Wrist") continue;
    rigRotation(side + f, rh[side + f]);
  }
}

// ---------------------------------------------------------------- track loop
function activeVideo() { return S.mode === "live" ? camVideo : srcVideo; }

function trackFrame(v) {
  if (S.mode === "video" && v.paused) return;    // hold the pose between plays
  const ts = Math.max(S.lastTs + 1, Math.round(performance.now()));
  S.lastTs = ts;
  if (S.mode === "mirror") {                     // live sticker preview: face only
    try {
      const f = S.trackers.face && S.trackers.face.detectForVideo(v, ts);
      S.lastFace = f && f.faceLandmarks && f.faceLandmarks[0] || null;
    } catch (e) { /* one bad frame */ }
    return;
  }
  let pose = null, face = null, hands = null;
  try {
    if (S.track.body && S.trackers.pose) pose = S.trackers.pose.detectForVideo(v, ts);
    if (S.track.face && S.trackers.face) face = S.trackers.face.detectForVideo(v, ts);
    if (S.track.hands && S.trackers.hand) hands = S.trackers.hand.detectForVideo(v, ts);
  } catch (e) { return; }

  let riggedPose = null;
  try {
    if (pose && pose.worldLandmarks && pose.worldLandmarks[0]) {
      riggedPose = K.Pose.solve(pose.worldLandmarks[0], pose.landmarks[0],
        { runtime: "mediapipe", video: v });
      rigPose(riggedPose);
    }
  } catch (e) { /* one bad frame */ }
  try {
    if (face && face.faceLandmarks && face.faceLandmarks[0]) {
      rigFace(K.Face.solve(face.faceLandmarks[0], { runtime: "mediapipe", video: v }));
    }
  } catch (e) { /* one bad frame */ }
  if (hands && hands.landmarks) {
    for (let i = 0; i < hands.landmarks.length; i++) {
      try {
        const side = hands.handedness[i] && hands.handedness[i][0]
          ? hands.handedness[i][0].categoryName : null;   // "Left" | "Right"
        if (!side) continue;
        rigHand(side, K.Hand.solve(hands.landmarks[i], side), riggedPose);
      } catch (e) { /* one bad frame */ }
    }
  }
}

function drawComposite(v, ready) {
  const W = stage.width, H = stage.height;
  if (S.mode === "mirror" && P.ready && P.neutral) {
    // the moving image IS the stage; your camera floats above it as a DOM
    // overlay (#camPip) so it never bakes into an instant recording
    ctx.fillStyle = "#0b0e14"; ctx.fillRect(0, 0, W, H);
    ctx.drawImage(glCanvas, 0, 0);
    return;
  }
  // background layer
  if (S.bg === "green") { ctx.fillStyle = "#00b140"; ctx.fillRect(0, 0, W, H); }
  else if (S.bg === "color") { ctx.fillStyle = S.bgColor; ctx.fillRect(0, 0, W, H); }
  else if (ready) {
    ctx.save();
    if (S.mode === "live" && S.mirror) { ctx.translate(W, 0); ctx.scale(-1, 1); }
    ctx.drawImage(v, 0, 0, W, H);
    ctx.restore();
  } else { ctx.fillStyle = "#0b0e14"; ctx.fillRect(0, 0, W, H); }
  if (S.mode === "mirror") { drawFaceSticker(W, H); return; }
  // avatar layer — scaled from the bottom-center, dragged by av.x/y
  const s = S.av.scale, Ws = W * s, Hs = H * s;
  ctx.drawImage(glCanvas, (W - Ws) / 2 + S.av.x, (H - Hs) + S.av.y, Ws, Hs);
}

// ---------------------------------------------------------------- live puppet
// Avatar Creator's real-time layer: the uploaded image itself MOVES with the
// user — head, brows, lids, lips — via moving-least-squares warping (Schaefer
// 2006, affine form). The image's face landmarks are control points; each
// frame their targets shift by the user's live landmark deltas, and a textured
// grid re-renders on the GPU. ~60fps on anything. The neural render on Stop is
// the studio-quality version; this is the mirror.

const PUPPET_IDX = [   // curated MediaPipe 468 indices used as control points
  10, 297, 284, 389, 454, 361, 397, 379, 400, 152, 176, 150, 172, 132, 234, 162, 54, 67,
  70, 105, 107, 336, 334, 300,                 // brows
  33, 133, 159, 145, 362, 263, 386, 374,       // eyes
  1,                                           // nose tip
  61, 291, 0, 17, 37, 267, 84, 314,            // outer lips
  13, 14, 78, 308,                             // inner lips
];
const PUPPET_GRID = 40;

const P = {   // puppet state
  ready: false, imgFor: null,
  imgW: 0, imgH: 0,
  ctrl: [],           // control point base positions in image px (landmarks + border anchors)
  nLm: 0,             // how many of ctrl are landmarks (rest are fixed border anchors)
  coeff: null,        // Float32Array [nVerts * ctrl.length] MLS coefficients
  scene: null, cam: null, mesh: null,
  neutral: null, neutralAcc: null, neutralN: 0, lostFrames: 0,
  refFaceW: 1,
};

async function initPuppet() {
  if (!S.personImg || S.mode !== "mirror") return;
  if (P.ready && P.imgFor === S.personImg.src) return;
  P.ready = false;
  try {
    const img = S.personImg;
    if (!(img.complete && img.naturalWidth)) {   // decode() stalls in hidden tabs
      await new Promise((res, rej) => {
        img.onload = res;
        img.onerror = () => rej(new Error("avatar image failed to load"));
      });
    }
    if (!S.fileset) {
      status("loading tracking engine…");
      S.fileset = await FilesetResolver.forVisionTasks("/static/vendor/mediapipe/wasm");
    }
    if (!S.trackers.faceImg) {
      status("reading the avatar's face…");
      S.trackers.faceImg = await FaceLandmarker.createFromOptions(S.fileset, {
        baseOptions: { modelAssetPath: "/static/models/mediapipe/face_landmarker.task",
                       delegate: "GPU" },
        runningMode: "IMAGE", numFaces: 1 });
    }
    const det = S.trackers.faceImg.detect(img);
    const lm = det.faceLandmarks && det.faceLandmarks[0];
    if (!lm) { status("no face found in that image — the preview will use the simple overlay"); return; }

    const W = img.naturalWidth, H = img.naturalHeight;
    P.imgW = W; P.imgH = H;
    P.ctrl = PUPPET_IDX.map(i => [lm[i].x * W, lm[i].y * H]);
    P.nLm = P.ctrl.length;
    for (const t of [0, 0.25, 0.5, 0.75, 1]) {       // border anchors pin the frame
      P.ctrl.push([t * W, 0], [t * W, H]);
      if (t > 0 && t < 1) P.ctrl.push([0, t * H], [W, t * H]);
    }
    P.refFaceW = Math.hypot(lm[454].x * W - lm[234].x * W, lm[454].y * H - lm[234].y * H) || 1;

    // MLS-affine coefficients, precomputed per grid vertex
    const n = P.ctrl.length, g = PUPPET_GRID, verts = (g + 1) * (g + 1);
    P.coeff = new Float32Array(verts * n);
    const w = new Float64Array(n), ph = new Float64Array(n * 2);
    for (let vy = 0; vy <= g; vy++) for (let vx = 0; vx <= g; vx++) {
      const vi = vy * (g + 1) + vx;
      const x = vx / g * W, y = vy / g * H;
      let sw = 0, px = 0, py = 0;
      for (let j = 0; j < n; j++) {
        const dx = P.ctrl[j][0] - x, dy = P.ctrl[j][1] - y;
        w[j] = 1 / (dx * dx + dy * dy + 1e-6);
        sw += w[j]; px += w[j] * P.ctrl[j][0]; py += w[j] * P.ctrl[j][1];
      }
      px /= sw; py /= sw;
      let m00 = 0, m01 = 0, m11 = 0;
      for (let j = 0; j < n; j++) {
        const ax = P.ctrl[j][0] - px, ay = P.ctrl[j][1] - py;
        ph[j * 2] = ax; ph[j * 2 + 1] = ay;
        m00 += w[j] * ax * ax; m01 += w[j] * ax * ay; m11 += w[j] * ay * ay;
      }
      const det2 = m00 * m11 - m01 * m01 || 1e-9;
      const vxp = x - px, vyp = y - py;
      const r0 = (vxp * m11 - vyp * m01) / det2, r1 = (vyp * m00 - vxp * m01) / det2;
      for (let j = 0; j < n; j++) {
        P.coeff[vi * n + j] = w[j] / sw + w[j] * (r0 * ph[j * 2] + r1 * ph[j * 2 + 1]);
      }
    }

    // textured grid in its own ortho scene
    if (P.mesh) { P.scene.remove(P.mesh); P.mesh.geometry.dispose(); P.mesh.material.map.dispose(); P.mesh.material.dispose(); }
    P.scene = new THREE.Scene();
    P.cam = new THREE.OrthographicCamera(-1, 1, 1, -1, -10, 10);
    const geo = new THREE.PlaneGeometry(1, 1, g, g);
    const tex = new THREE.Texture(img);
    tex.needsUpdate = true;
    tex.colorSpace = THREE.SRGBColorSpace;
    P.mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex }));
    P.scene.add(P.mesh);
    P.neutral = null; P.neutralAcc = null; P.neutralN = 0;
    P.imgFor = img.src;
    P.ready = true;
    if (S.mode === "mirror" && S.camStream) setStageSize(P.imgW, P.imgH);
    status("avatar face locked — start the camera");
  } catch (e) { status("puppet init failed — using the simple overlay"); }
}

function updatePuppet(v) {
  if (!P.ready) return false;
  const lm = S.lastFace;
  if (!lm) { if (++P.lostFrames > 45) { P.neutral = null; P.neutralAcc = null; P.neutralN = 0; } return true; }
  P.lostFrames = 0;
  const vw = v.videoWidth, vh = v.videoHeight;
  if (!P.neutral) {                       // calibrate on ~8 steady frames
    if (!P.neutralAcc) P.neutralAcc = new Float64Array(PUPPET_IDX.length * 2);
    for (let j = 0; j < PUPPET_IDX.length; j++) {
      P.neutralAcc[j * 2] += lm[PUPPET_IDX[j]].x * vw;
      P.neutralAcc[j * 2 + 1] += lm[PUPPET_IDX[j]].y * vh;
    }
    if (++P.neutralN >= 8) {
      P.neutral = Float64Array.from(P.neutralAcc, a => a / P.neutralN);
      status("locked on — you're driving the avatar");
    }
    return true;
  }
  const liveFaceW = Math.hypot(
    (lm[454].x - lm[234].x) * vw, (lm[454].y - lm[234].y) * vh) || 1;
  const s = P.refFaceW / liveFaceW;
  const n = P.ctrl.length, nLm = P.nLm;
  const q = new Float64Array(n * 2);
  let mx = 0, my = 0;
  const d = new Float64Array(nLm * 2);
  for (let j = 0; j < nLm; j++) {
    let dx = (lm[PUPPET_IDX[j]].x * vw - P.neutral[j * 2]) * s;
    let dy = (lm[PUPPET_IDX[j]].y * vh - P.neutral[j * 2 + 1]) * s;
    if (S.mirror) dx = -dx;
    d[j * 2] = dx; d[j * 2 + 1] = dy; mx += dx; my += dy;
  }
  mx /= nLm; my /= nLm;
  const capT = P.refFaceW * 0.18;         // damp whole-head translation, keep expression
  const tx = Math.max(-capT, Math.min(capT, mx * 0.5));
  const ty = Math.max(-capT, Math.min(capT, my * 0.5));
  for (let j = 0; j < n; j++) {
    if (j < nLm) {
      q[j * 2] = P.ctrl[j][0] + (d[j * 2] - mx) + tx;
      q[j * 2 + 1] = P.ctrl[j][1] + (d[j * 2 + 1] - my) + ty;
    } else {
      q[j * 2] = P.ctrl[j][0]; q[j * 2 + 1] = P.ctrl[j][1];
    }
  }
  // apply MLS: vertex = Σ coeff_j q_j, mapped into the plane's -0.5..0.5 space
  const pos = P.mesh.geometry.attributes.position;
  const g = PUPPET_GRID;
  for (let vi = 0; vi < (g + 1) * (g + 1); vi++) {
    let x = 0, y = 0;
    const base = vi * n;
    for (let j = 0; j < n; j++) {
      const c = P.coeff[base + j];
      x += c * q[j * 2]; y += c * q[j * 2 + 1];
    }
    pos.setXYZ(vi, x / P.imgW - 0.5, 0.5 - y / P.imgH, 0);
  }
  pos.needsUpdate = true;
  return true;
}

const _rsz = new THREE.Vector2();
function renderPuppet() {
  const W = stage.width, H = stage.height;
  renderer.getSize(_rsz);
  if (_rsz.x !== W || _rsz.y !== H) {       // camera may not have sized us yet
    glCanvas.width = W; glCanvas.height = H;
    renderer.setSize(W, H, false);
  }
  const ca = W / H, ia = P.imgW / P.imgH;
  P.mesh.scale.set(ia, 1, 1);               // un-squash the normalized square
  let hw, hh;                                // contain-fit the image in the stage
  if (ia > ca) { hw = ia / 2; hh = hw / ca; }
  else { hh = 0.5; hw = hh * ca; }
  P.cam.left = -hw; P.cam.right = hw; P.cam.top = hh; P.cam.bottom = -hh;
  P.cam.updateProjectionMatrix();
  renderer.render(P.scene, P.cam);
}

// Fallback sticker preview (used when the uploaded image has no detectable face):
// position, tilt and size follow the tracker in real time. The photoreal
// version renders after Stop; this is the "it sees me" feedback layer.
function drawFaceSticker(W, H) {
  const lm = S.lastFace, img = S.personImg;
  if (!lm || !img || !img.complete || !img.naturalWidth) return;
  let xs = 1, ys = 1, cx = 0, cy = 0;
  let minX = 1, maxX = 0, minY = 1, maxY = 0;
  for (const p of lm) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  cx = (minX + maxX) / 2; cy = (minY + maxY) / 2;
  const mirror = S.mirror;
  if (mirror) cx = 1 - cx;
  const rEye = lm[33], lEye = lm[263];
  let roll = Math.atan2(lEye.y - rEye.y, lEye.x - rEye.x);
  if (mirror) roll = -roll;
  const fw = (maxX - minX) * W;
  const w = fw * (2.2 * S.av.scale);
  const h = w * (img.naturalHeight / img.naturalWidth);
  ctx.save();
  ctx.translate(cx * W + S.av.x, cy * H + S.av.y);
  ctx.rotate(roll);
  ctx.globalAlpha = 0.92;
  ctx.drawImage(img, -w / 2, -h * 0.45, w, h);
  ctx.restore();
  ctx.globalAlpha = 1;
}

function loop() {
  if (!S.running) return;
  requestAnimationFrame(loop);
  const v = activeVideo();
  const ready = v && v.readyState >= 2;
  if (S.mode === "mirror") {
    if (ready) trackFrame(v);
    if (ready && P.ready) { updatePuppet(v); renderPuppet(); }
  } else {
    if (ready && S.vrm) trackFrame(v);
    if (S.vrm) S.vrm.update(clock.getDelta());
    renderer.render(scene, camera);
  }
  drawComposite(v, ready);
}

function startLoop() {
  if (S.running) return;
  S.running = true;
  $("#stageEmpty").style.display = "none";
  clock.getDelta();
  loop();
}

function drawIdle() {                     // one static frame before anything runs
  if (S.running) return;
  if (S.vrm) S.vrm.update(0.016);
  renderer.render(scene, camera);
  drawComposite(activeVideo(), false);
}

// ---------------------------------------------------------------- recording
function startRecorder() {
  let stream;
  if (S.mode === "mirror" && S.instant) {
    // ⚡ Instant: record the live moving avatar exactly as you see it + mic
    stream = stage.captureStream(30);
    if ($("#micChk").checked && S.camStream) {
      for (const t of S.camStream.getAudioTracks()) stream.addTrack(t);
    }
  } else if (S.mode === "mirror") {
    // 💎 Studio: record the RAW camera (the neural transfer wants your real
    // face, not the preview) + the mic
    const tracks = [...S.camStream.getVideoTracks()];
    if ($("#micChk").checked) tracks.push(...S.camStream.getAudioTracks());
    stream = new MediaStream(tracks);
  } else {
    stream = stage.captureStream(30);
    if (S.mode === "live" && $("#micChk").checked && S.camStream) {
      for (const t of S.camStream.getAudioTracks()) stream.addTrack(t);
    }
  }
  S.recMode = (S.mode === "mirror" && S.instant) ? "mirror-live" : S.mode;
  const mime = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
    ? "video/webm;codecs=vp9" : "video/webm";
  S.recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 10_000_000 });
  S.chunks = [];
  S.recorder.ondataavailable = e => { if (e.data && e.data.size) S.chunks.push(e.data); };
  S.recorder.onstop = onRecorded;
  S.recorder.start(250);
  S.recording = true;
  $("#recLight").classList.add("on");
  $("#btnRecord").disabled = true;
  $("#btnStop").disabled = false;
  status("recording…");
}

function stopRecorder() {
  if (S.recorder && S.recorder.state !== "inactive") S.recorder.stop();
  S.recording = false;
  $("#recLight").classList.remove("on");
  $("#btnRecord").disabled = false;
  $("#btnStop").disabled = true;
}

async function onRecorded() {
  const blob = new Blob(S.chunks, { type: "video/webm" });
  S.chunks = [];
  if (blob.size < 4096) { status("nothing captured"); return; }
  status(`uploading take (${VS.fmtSize(blob.size)})…`);
  const fd = new FormData();
  fd.append("file", blob, "rec.webm");
  try {
    let j;
    if (S.recMode === "mirror") {
      fd.append("person", S.person);
      j = await VS.api("/api/mocap/avatar-take", { method: "POST", body: fd });
      status("transferring your performance onto the avatar (free, local GPU)…");
    } else if (S.recMode === "mirror-live") {
      fd.append("mode", "live");             // plain finalize — the live look IS the take
      j = await VS.api("/api/mocap/rec", { method: "POST", body: fd });
      status("finalizing your instant take…");
    } else {
      fd.append("mode", S.recMode);
      if (S.recMode === "video" && S.source) fd.append("source", S.source.name);
      j = await VS.api("/api/mocap/rec", { method: "POST", body: fd });
      status("finalizing mp4…");
    }
    watchJob(j.job_id);
  } catch (e) { status("upload failed: " + e.message); }
}

async function watchJob(jobId) {
  const log = $("#mcLog");
  log.classList.add("on"); log.textContent = "";
  let off = 0, st = "running";
  while (st === "running") {
    await new Promise(r => setTimeout(r, 1500));
    let j;
    try { j = await VS.api(`/api/job/${jobId}?offset=${off}`); } catch (e) { continue; }
    for (const line of (j.lines || [])) {
      log.textContent += line + "\n"; log.scrollTop = log.scrollHeight;
    }
    off = j.next_offset; st = j.status;
  }
  status(st === "done" ? "✅ take delivered" : "❌ finalize failed — see the log");
  if (st === "done") { VS.toast("Motion Capture take ready"); loadGallery(); }
}

// ---------------------------------------------------------------- modes
async function startCamera() {
  if (S.camStream) return;
  status("starting camera…");
  const deviceId = $("#camSel").value;
  S.camStream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: deviceId ? { exact: deviceId } : undefined,
             width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: true,     // grabbed even if mic is off — the toggle decides at record time
  });
  camVideo.srcObject = S.camStream;
  await camVideo.play();
  if (S.mode === "mirror") {
    // stage takes the AVATAR IMAGE's shape (that's what gets recorded);
    // your camera floats as a corner overlay
    if (P.ready) setStageSize(P.imgW, P.imgH);
    else setStageSize(camVideo.videoWidth || 1280, camVideo.videoHeight || 720);
    const pip = $("#camPip");
    pip.srcObject = S.camStream;
    pip.style.display = "block";
    pip.play().catch(() => {});
  } else {
    setStageSize(camVideo.videoWidth || 1280, camVideo.videoHeight || 720);
  }
  const cams = (await navigator.mediaDevices.enumerateDevices())
    .filter(d => d.kind === "videoinput");
  $("#camSel").innerHTML = cams.map(c =>
    `<option value="${VS.esc(c.deviceId)}"${c.deviceId === deviceId ? " selected" : ""}>` +
    `${VS.esc(c.label || "camera")}</option>`).join("") || '<option value="">default camera</option>';
  $("#camInfo").textContent = `${camVideo.videoWidth}×${camVideo.videoHeight}`;
  await ensureTrackers();
  startLoop();
  status(S.mode === "mirror"
    ? "camera live — the avatar is locked to your face; hit Record when ready"
    : "camera live — tracking");
}

function stopCamera() {
  if (S.camStream) { for (const t of S.camStream.getTracks()) t.stop(); }
  S.camStream = null; camVideo.srcObject = null;
  const pip = $("#camPip");
  pip.srcObject = null; pip.style.display = "none";
}

async function previewVideo() {
  if (!S.source) { VS.toast("upload a source video first"); return; }
  await ensureTrackers();
  startLoop();
  srcVideo.currentTime = 0;
  await srcVideo.play();
  status("preview — tracking the footage");
}

async function recordTake() {
  if (S.mode === "mirror") {
    if (!S.person) { VS.toast("pick or upload an avatar image first"); return; }
    if (S.instant && !P.ready) {
      VS.toast("instant mode needs a face in the image — use Studio, or try another photo");
      return;
    }
    await startCamera();
    startRecorder();
    return;
  }
  if (!S.vrm) { VS.toast("pick an avatar first"); return; }
  if (S.mode === "video") {
    if (!S.source) { VS.toast("upload a source video first"); return; }
    await ensureTrackers();
    startLoop();
    srcVideo.pause();
    srcVideo.currentTime = 0;
    await new Promise(r => { srcVideo.onseeked = r; });
    startRecorder();
    await srcVideo.play();                 // ends → auto-stop (wired below)
  } else {
    await startCamera();
    startRecorder();
  }
}

srcVideo.addEventListener("ended", () => { if (S.recording) stopRecorder(); });

function stopAll() {
  if (S.recording) { stopRecorder(); srcVideo.pause(); return; }
  srcVideo.pause();
  if (S.mode === "live" || S.mode === "mirror") { stopCamera(); status("camera stopped"); }
  else status("stopped");
}

// ---------------------------------------------------------------- source upload
async function uploadSource(file) {
  $("#srcInfo").textContent = `uploading ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const j = await VS.api("/api/upload", { method: "POST", body: fd });
    S.source = { name: j.name, url: `/media/uploads/${encodeURIComponent(j.name)}` };
    srcVideo.src = S.source.url;
    await new Promise((res, rej) => {
      srcVideo.onloadedmetadata = res; srcVideo.onerror = rej;
    });
    setStageSize(srcVideo.videoWidth, srcVideo.videoHeight);
    $("#srcInfo").textContent =
      `${j.name} — ${srcVideo.videoWidth}×${srcVideo.videoHeight}, ${VS.fmtT(srcVideo.duration)}`;
    srcVideo.currentTime = 0.01;           // paint the first frame
    srcVideo.onseeked = () => { $("#stageEmpty").style.display = "none"; drawIdle(); srcVideo.onseeked = null; };
    status("source ready — hit Preview or Record");
  } catch (e) { $("#srcInfo").textContent = "upload failed: " + e.message; }
}

// ---------------------------------------------------------------- real person (AI)
async function loadPersons() {
  const box = $("#personList");
  try {
    const j = await VS.api("/api/mocap/persons");
    box.innerHTML = j.persons.map(p =>
      `<img class="mc-person${p.name === S.person ? " on" : ""}" src="${VS.esc(p.url)}"` +
      ` data-name="${VS.esc(p.name)}" title="${VS.esc(p.name)}">`).join("");
    const pick = (name) => {
      S.person = name;
      S.personImg = new Image();           // the live-preview puppet/sticker source
      S.personImg.src = `/media/output/mocap/refs/${encodeURIComponent(name)}`;
      if (S.mode === "mirror") initPuppet();
    };
    for (const el of box.querySelectorAll(".mc-person")) {
      el.onclick = () => {
        pick(el.dataset.name);
        for (const x of box.querySelectorAll(".mc-person")) x.classList.remove("on");
        el.classList.add("on");
      };
    }
    if (!S.person && j.persons[0]) {
      pick(j.persons[0].name);
      const first = box.querySelector(".mc-person");
      if (first) first.classList.add("on");
    } else if (S.person && !S.personImg) {
      pick(S.person);
    }
  } catch (e) { /* panel stays empty */ }
}

async function uploadPerson(file) {
  $("#realStatus").textContent = "uploading photo…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const j = await VS.api("/api/mocap/person", { method: "POST", body: fd });
    S.person = j.name;
    S.personImg = null;                    // loadPersons re-picks with a fresh image
    await loadPersons();
    $("#realStatus").textContent = "photo ready";
  } catch (e) { $("#realStatus").textContent = "photo upload failed: " + e.message; }
}

async function runRealPerson() {
  if (!S.source) { VS.toast("upload a source video first"); return; }
  if (!S.person) { VS.toast("add a photo of the person first"); return; }
  const [engine, resolution] = $("#realEngine").value.split("|");
  const body = { source: S.source.name, person: S.person, engine, resolution };
  const seed = parseInt($("#realSeed").value, 10);
  if (Number.isFinite(seed) && seed >= 0) body.seed = seed;
  const testS = parseFloat($("#realTest").value);
  if (Number.isFinite(testS) && testS > 0) body.test_seconds = testS;
  body.no_split = $("#realOneCall").checked;
  const rs = $("#realStatus");
  $("#btnReal").disabled = true;
  try {
    let j;
    try {
      j = await VS.api("/api/mocap/animate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
    } catch (e) {
      if (e.status === 402 && e.body && e.body.estimate) {
        const est = e.body.estimate;
        if (!confirm(`${est.summary}\n\nSpend ~$${est.usd} on fal.ai?`)) {
          rs.textContent = "cancelled"; return;
        }
        j = await VS.api("/api/mocap/animate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, confirm_cost: true }) });
      } else throw e;
    }
    if (j.seed != null) $("#realSeed").value = j.seed;   // reusable: same seed = same look
    rs.textContent = `rendering ~$${j.estimate.usd} · seed ${j.seed} · watch the log below`;
    status("real-person render running…");
    await watchJob(j.job_id);
    rs.textContent = j.seed != null ? `done · seed ${j.seed} (reuse it to keep this look)` : "";
  } catch (e) { rs.textContent = "failed: " + e.message; }
  finally { $("#btnReal").disabled = false; }
}

async function runLivePortrait() {
  if (!S.source) { VS.toast("upload a source video first"); return; }
  if (!S.person) { VS.toast("add a photo of the person first"); return; }
  const rs = $("#realStatus");
  $("#btnLP").disabled = true;
  try {
    const j = await VS.post("/api/mocap/liveportrait",
      { source: S.source.name, person: S.person });
    rs.textContent = "rendering on the local GPU (free) · watch the log below";
    status("LivePortrait render running…");
    await watchJob(j.job_id);
    rs.textContent = "";
  } catch (e) { rs.textContent = "failed: " + e.message; }
  finally { $("#btnLP").disabled = false; }
}

// ---------------------------------------------------------------- avatars UI
async function loadAvatars() {
  const box = $("#avList");
  try {
    const j = await VS.api("/api/mocap/avatars");
    if (!j.avatars.length) {
      box.innerHTML = '<span style="color:var(--dim);font-size:13px">no avatars yet — upload a .vrm</span>';
      return;
    }
    box.innerHTML = j.avatars.map(a =>
      `<div class="mc-av" data-url="${VS.esc(a.url)}" title="${VS.esc(a.name)}">${VS.esc(a.name.replace(/\.vrm$/i, ""))}</div>`
    ).join("");
    const first = box.querySelector(".mc-av");
    for (const el of box.querySelectorAll(".mc-av")) {
      el.onclick = async () => {
        for (const x of box.querySelectorAll(".mc-av")) x.classList.remove("on");
        el.classList.add("on");
        try { await loadAvatar(el.dataset.url); } catch (e) { status("avatar failed: " + e.message); }
      };
    }
    if (first && !S.vrm) first.click();
  } catch (e) {
    box.innerHTML = '<span style="color:var(--bad);font-size:13px">could not list avatars</span>';
  }
}

// ---------------------------------------------------------------- gallery
async function loadGallery() {
  const g = $("#gallery");
  try {
    const j = await VS.api("/api/mocap/list");
    if (!j.takes.length) {
      g.innerHTML = '<span style="color:var(--dim);font-size:13px">no takes yet — record one</span>';
      return;
    }
    g.innerHTML = j.takes.map(t => `
      <div class="mc-take" data-name="${VS.esc(t.name)}">
        <video controls preload="metadata" src="${VS.esc(t.url)}"></video>
        <div class="meta"><span>${VS.esc(t.name)}</span><span>${VS.fmtSize(t.size)}</span></div>
        <div class="acts">
          <button class="vs-btn ghost act-src" type="button" title="drive the video-mode engines with this take">🎬 Source</button>
          <button class="vs-btn ghost act-desk" type="button">📤 Desktop</button>
          <button class="vs-btn ghost act-del" type="button">🗑</button>
        </div>
      </div>`).join("");
    for (const el of g.querySelectorAll(".mc-take")) {
      const name = el.dataset.name;
      el.querySelector(".act-src").onclick = async () => {
        try {
          const r = await VS.post("/api/mocap/use-as-source", { name });
          S.source = { name: r.name, url: r.url };
          srcVideo.src = r.url;
          setMode("video");
          $("#srcInfo").textContent = r.name + " — from your takes";
          status("take loaded as source — run Real person (AI) or Preview");
          VS.toast("take is now the source video");
        } catch (e) { VS.toast("failed: " + e.message); }
      };
      el.querySelector(".act-desk").onclick = async () => {
        try { const r = await VS.post("/api/mocap/export", { name });
              VS.toast("exported → " + r.exported); }
        catch (e) { VS.toast("export failed: " + e.message); }
      };
      el.querySelector(".act-del").onclick = async () => {
        if (!confirm(`delete ${name}?`)) return;
        try { await VS.post("/api/mocap/delete", { name }); loadGallery(); }
        catch (e) { VS.toast("delete failed: " + e.message); }
      };
    }
  } catch (e) {
    g.innerHTML = '<span style="color:var(--bad);font-size:13px">could not list takes</span>';
  }
}

// ---------------------------------------------------------------- UI wiring
function status(msg) { $("#mcStatus").textContent = msg; }

function setMode(mode) {
  if (S.recording) return;
  S.mode = mode;
  $("#modeVideo").classList.toggle("on", mode === "video");
  $("#modeLive").classList.toggle("on", mode === "live");
  $("#modeMirror").classList.toggle("on", mode === "mirror");
  document.body.classList.toggle("mode-mirror", mode === "mirror");
  $("#panelVideo").style.display = mode === "video" ? "" : "none";
  $("#panelReal").style.display = mode !== "live" ? "" : "none";
  $("#panelLive").style.display = mode !== "video" ? "" : "none";
  $("#panelTrack").style.display = mode !== "mirror" ? "" : "none";
  $("#panelVrm").style.display = mode !== "mirror" ? "" : "none";
  $("#realTitle").textContent = mode === "mirror" ? "🪞 Your avatar face" : "🧑 Real person (AI)";
  $("#btnPreview").textContent = mode === "video" ? "▶ Preview" : "📷 Start camera";
  $("#modeHint").textContent = mode === "video"
    ? "Upload footage of a person — the avatar copies their moves and lips, stuck right on the video."
    : mode === "live"
      ? "Your webcam drives the 3D avatar in real time — record a take with mic audio."
      : "Upload a face image, hit Record, perform to the camera — the image delivers "
        + "your exact performance (head, expressions, lips, your voice). Free, local GPU.";
  const bgFootage = $("#bgSel").options[0];
  bgFootage.textContent = mode === "video" ? "the footage" : "the camera";
  S.running = false;                       // stop the loop; restart on demand
  srcVideo.pause();
  if (mode === "video") stopCamera();
  ctx.fillStyle = "#0b0e14";               // clear whatever the last mode drew
  ctx.fillRect(0, 0, stage.width, stage.height);
  if (mode !== "mirror" && S.vrm) drawIdle();
  $("#stageEmpty").style.display = "";
  $("#stageEmpty").innerHTML = mode === "mirror"
    ? "<b>Avatar Creator</b><span>pick a face on the left, then Start camera</span>"
    : "<b>nothing on stage yet</b><span>upload a source video or start the camera</span>";
  status(mode === "video" ? "upload a source video"
    : mode === "live" ? "hit Start camera"
    : "pick an avatar image, then hit Start camera");
  if (mode === "mirror" && S.personImg) initPuppet();
}

$("#modeVideo").onclick = () => setMode("video");
$("#modeLive").onclick = () => setMode("live");
$("#modeMirror").onclick = () => setMode("mirror");

$("#srcDrop").onclick = () => $("#srcFile").click();
$("#srcFile").onchange = e => { if (e.target.files[0]) uploadSource(e.target.files[0]); };
$("#srcDrop").ondragover = e => { e.preventDefault(); $("#srcDrop").classList.add("over"); };
$("#srcDrop").ondragleave = () => $("#srcDrop").classList.remove("over");
$("#srcDrop").ondrop = e => {
  e.preventDefault(); $("#srcDrop").classList.remove("over");
  if (e.dataTransfer.files[0]) uploadSource(e.dataTransfer.files[0]);
};

$("#personDrop").onclick = () => $("#personFile").click();
$("#personFile").onchange = e => { if (e.target.files[0]) uploadPerson(e.target.files[0]); };
$("#personDrop").ondragover = e => { e.preventDefault(); $("#personDrop").classList.add("over"); };
$("#personDrop").ondragleave = () => $("#personDrop").classList.remove("over");
$("#personDrop").ondrop = e => {
  e.preventDefault(); $("#personDrop").classList.remove("over");
  if (e.dataTransfer.files[0]) uploadPerson(e.dataTransfer.files[0]);
};
$("#btnReal").onclick = runRealPerson;
$("#btnLP").onclick = runLivePortrait;
$("#recInstant").onclick = () => {
  S.instant = true;
  $("#recInstant").classList.add("on"); $("#recStudio").classList.remove("on");
  $("#recModeHint").textContent =
    "Instant records the live avatar exactly as you see it — done seconds after you stop.";
};
$("#recStudio").onclick = () => {
  S.instant = false;
  $("#recStudio").classList.add("on"); $("#recInstant").classList.remove("on");
  $("#recModeHint").textContent =
    "Studio records your real face and renders the flawless version on the GPU after you stop (a few minutes).";
};
$("#btnXR").onclick = async () => {
  try {
    await VS.post("/api/mocap/xr-launch", {});
    VS.toast("XR Animator launching — check your desktop");
  } catch (e) { VS.toast("launch failed: " + e.message); }
};

$("#avUploadBtn").onclick = () => $("#avFile").click();
$("#avFile").onchange = async e => {
  const f = e.target.files[0];
  if (!f) return;
  status("uploading avatar…");
  const fd = new FormData();
  fd.append("file", f);
  try {
    await VS.api("/api/mocap/avatar", { method: "POST", body: fd });
    await loadAvatars();
    status("avatar uploaded");
  } catch (err) { status("avatar upload failed: " + err.message); }
};

$("#tBody").onchange = e => { S.track.body = e.target.checked; };
$("#tHands").onchange = e => { S.track.hands = e.target.checked; };
$("#tFace").onchange = e => { S.track.face = e.target.checked; };
for (const id of ["tBody", "tHands", "tFace", "mirrorChk", "micChk"]) {
  const el = $("#" + id);
  el.addEventListener("change", () => el.closest(".mc-toggle").classList.toggle("off", !el.checked));
}
$("#mirrorChk").onchange = e => { S.mirror = e.target.checked; };
$("#smoothRange").oninput = e => { S.snap = 1 - e.target.value / 100; };
$("#scaleRange").oninput = e => { S.av.scale = e.target.value / 100; if (!S.running) drawIdle(); };
$("#bgSel").onchange = e => {
  S.bg = e.target.value;
  $("#bgColor").style.display = S.bg === "color" ? "" : "none";
  if (!S.running) drawIdle();
};
$("#bgColor").oninput = e => { S.bgColor = e.target.value; if (!S.running) drawIdle(); };
$("#camSel").onchange = () => { if (S.camStream) { stopCamera(); startCamera(); } };

$("#btnPreview").onclick = () =>
  (S.mode === "video" ? previewVideo() : startCamera()).catch(e => status("failed: " + e.message));
$("#btnRecord").onclick = () => recordTake().catch(e => status("failed: " + e.message));
$("#btnStop").onclick = stopAll;

// drag to move the avatar, wheel to resize
let drag = null;
stage.addEventListener("pointerdown", e => {
  drag = { x: e.clientX, y: e.clientY };
  stage.classList.add("dragging");
  stage.setPointerCapture(e.pointerId);
});
stage.addEventListener("pointermove", e => {
  if (!drag) return;
  const k = stage.width / stage.getBoundingClientRect().width;
  S.av.x += (e.clientX - drag.x) * k;
  S.av.y += (e.clientY - drag.y) * k;
  drag = { x: e.clientX, y: e.clientY };
  if (!S.running) drawIdle();
});
const endDrag = () => { drag = null; stage.classList.remove("dragging"); };
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("wheel", e => {
  e.preventDefault();
  S.av.scale = Math.min(3, Math.max(0.25, S.av.scale * (e.deltaY < 0 ? 1.06 : 0.94)));
  $("#scaleRange").value = Math.round(S.av.scale * 100);
  if (!S.running) drawIdle();
}, { passive: false });

window.addEventListener("beforeunload", () => stopCamera());

// ---------------------------------------------------------------- boot
if (!K) {
  status("Kalidokit failed to load — check /static/vendor/kalidokit/");
} else {
  setMode("video");
  loadAvatars();
  loadPersons();
  loadGallery();
}
window.MC_DEBUG = { S, P, initPuppet, updatePuppet, renderPuppet, drawComposite };   // field debugging
