import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/meta_research/web_dist",
    emptyOutDir: true,
    sourcemap: false,
    assetsDir: "assets",
  },
});
