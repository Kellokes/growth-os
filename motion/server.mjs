// Serviço motion: recebe uma composição HyperFrames (JSON) e devolve MP4 (/render) ou PNG (/still).
// Roda numa rede interna do compose, sem internet. Um job por vez; tudo é apagado ao final.
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawn } from "node:child_process";

const PORT = 3000;
const MAX_BODY = 60_000_000; // vídeo de fonte de até 25 MB vira ~34 MB em base64
const JOB_TIMEOUT_MS = 200_000;
const ROOT = path.resolve("node_modules");
const CLI = path.join(ROOT, "hyperframes", "dist", "cli.js");
const GSAP = path.join(ROOT, "gsap", "dist", "gsap.min.js");
const QUALITY = new Set(["draft", "standard", "delivery"]);
const SAFE_PATH = /^(index\.html|assets\/[A-Za-z0-9._-]+)$/;

let busy = false;

// O CLI abre o Chromium e vários filhos. Matar só o node deixa o Chrome órfão rodando: depois de um tempo esgotado os
// processos e a memória do container se acumulam e o próximo render cai com "pthread_create: Resource temporarily unavailable"
// (SIGTRAP). Por isso o CLI roda no próprio grupo de processos e o grupo inteiro é encerrado ao fim, com sucesso ou não.
function killGroup(p) {
  try { process.kill(-p.pid, "SIGKILL"); } catch { /* grupo já encerrado */ }
}

function run(args, cwd) {
  return new Promise((resolve, reject) => {
    const p = spawn("node", [CLI, ...args], { cwd, env: process.env, stdio: ["ignore", "pipe", "pipe"], detached: true });
    let out = "";
    const take = (d) => { out = (out + d).slice(-4000); };
    p.stdout.on("data", take);
    p.stderr.on("data", take);
    const timer = setTimeout(() => { killGroup(p); reject(new Error("tempo esgotado")); }, JOB_TIMEOUT_MS);
    p.on("error", (e) => { clearTimeout(timer); killGroup(p); reject(e); });
    p.on("close", (code) => { clearTimeout(timer); killGroup(p); code === 0 ? resolve(out) : reject(new Error(out.trim().split("\n").slice(-6).join("\n"))); });
  });
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const c of req) {
    size += c.length;
    if (size > MAX_BODY) throw Object.assign(new Error("corpo grande demais"), { status: 413 });
    chunks.push(c);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function makeJob(body) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "job-"));
  const files = body.files ?? {};
  const binary = body.binary ?? {};
  if (typeof files["index.html"] !== "string") throw Object.assign(new Error("falta index.html"), { status: 400 });
  await fs.mkdir(path.join(dir, "lib"));
  await fs.mkdir(path.join(dir, "assets"));
  await fs.copyFile(GSAP, path.join(dir, "lib", "gsap.min.js"));
  await fs.writeFile(path.join(dir, "hyperframes.json"), "{}");
  for (const [name, text] of Object.entries(files)) {
    if (!SAFE_PATH.test(name)) throw Object.assign(new Error(`caminho recusado: ${name}`), { status: 400 });
    await fs.writeFile(path.join(dir, name), String(text));
  }
  for (const [name, b64] of Object.entries(binary)) {
    if (!SAFE_PATH.test(name) || name === "index.html") throw Object.assign(new Error(`caminho recusado: ${name}`), { status: 400 });
    await fs.writeFile(path.join(dir, name), Buffer.from(String(b64), "base64"));
  }
  return dir;
}

async function handle(req, res) {
  const url = req.url.split("?")[0];
  if (req.method === "GET" && url === "/health") return send(res, 200, "ok", "text/plain");
  if (req.method !== "POST" || (url !== "/render" && url !== "/still")) return send(res, 404, JSON.stringify({ error: "não encontrado" }), "application/json");
  if (busy) return send(res, 429, JSON.stringify({ error: "o serviço de vídeo está ocupado; tente em instantes" }), "application/json");
  busy = true;
  let dir;
  try {
    const body = await readBody(req);
    dir = await makeJob(body);
    if (url === "/render") {
      const quality = QUALITY.has(body.quality) ? body.quality : "standard";
      const out = path.join(dir, "out.mp4");
      await run(["render", "--quality", quality, "-o", out], dir);
      return send(res, 200, await fs.readFile(out), "video/mp4");
    }
    const at = Math.min(Math.max(Number(body.at) || 5.5, 0), 60);
    const snap = path.join(dir, "snap");
    await run(["snapshot", "--at", String(at), "--no-end", "--keep", "-o", snap], dir);
    const png = (await fs.readdir(snap)).find((f) => f.endsWith(".png"));
    if (!png) throw new Error("o quadro não foi gerado");
    return send(res, 200, await fs.readFile(path.join(snap, png)), "image/png");
  } catch (e) {
    return send(res, e.status ?? 500, JSON.stringify({ error: String(e.message ?? e).slice(0, 500) }), "application/json");
  } finally {
    busy = false;
    if (dir) fs.rm(dir, { recursive: true, force: true }).catch(() => {});
  }
}

function send(res, status, data, type) {
  res.writeHead(status, { "Content-Type": type, "Content-Length": Buffer.byteLength(data) });
  res.end(data);
}

http.createServer((req, res) => { handle(req, res).catch(() => { try { res.destroy(); } catch {} }); }).listen(PORT, "0.0.0.0");
