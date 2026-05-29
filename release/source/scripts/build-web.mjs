import { build } from "esbuild";

await build({
  entryPoints: ["src/web/server.ts"],
  bundle: true,
  platform: "node",
  target: "node20",
  format: "esm",
  external: ["openai"],
  outfile: "dist/web/server.bundle.js",
  banner: {
    js: [
      "import { createRequire as __tindaCreateRequire } from 'node:module';",
      "const require = __tindaCreateRequire(import.meta.url);"
    ].join("\n")
  }
});
