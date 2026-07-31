import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = resolve(siteRoot, "..", "assets");
const targetRoot = resolve(siteRoot, "public", "assets");
const assetNames = [
  "bench-cl-bench.png",
  "bench-osworld.png",
  "bench-terminal-bench.png",
  "evolution.png",
  "game-demo.png",
  "icon_1024.png",
  "og-preview.png",
  "skill-hub.png",
  "social-preview.png",
  "swarm.jpg"
];

await mkdir(targetRoot, { recursive: true });
await Promise.all(assetNames.map((name) => copyFile(resolve(sourceRoot, name), resolve(targetRoot, name))));
