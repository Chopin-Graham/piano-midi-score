import { build } from "esbuild";
import { readFile, rm, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(scriptDirectory, "..");
const outputDirectory = resolve(projectRoot, "dist");
const assetDirectory = resolve(outputDirectory, "assets");

await rm(outputDirectory, { recursive: true, force: true });

const result = await build({
  absWorkingDir: projectRoot,
  entryPoints: { app: "src/main.tsx" },
  outdir: assetDirectory,
  bundle: true,
  splitting: true,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  minify: true,
  sourcemap: true,
  metafile: true,
  entryNames: "[name]-[hash]",
  chunkNames: "chunk-[name]-[hash]",
  assetNames: "asset-[name]-[hash]",
  logLevel: "info",
});

const outputs = Object.entries(result.metafile.outputs);
const entry = outputs.find(([, metadata]) => metadata.entryPoint?.endsWith("src/main.tsx"));
if (!entry) throw new Error("Could not locate the frontend entry bundle");

const [entryPath, entryMetadata] = entry;
const tags = [];
if (entryMetadata.cssBundle) {
  tags.push(`<link rel="stylesheet" href="/${relative(outputDirectory, resolve(projectRoot, entryMetadata.cssBundle)).replaceAll("\\", "/")}" />`);
}
tags.push(`<script type="module" src="/${relative(outputDirectory, resolve(projectRoot, entryPath)).replaceAll("\\", "/")}"></script>`);

const template = await readFile(resolve(projectRoot, "index.html"), "utf8");
await writeFile(
  resolve(outputDirectory, "index.html"),
  template.replace("<!-- APP_ASSETS -->", tags.join("\n    ")),
  "utf8",
);
