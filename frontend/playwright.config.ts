import { defineConfig, devices } from "@playwright/test";
import { randomUUID } from "node:crypto";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const python = process.env.VERDICT_E2E_PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
process.env.VERDICT_E2E_DATABASE = process.env.VERDICT_E2E_DATABASE || path.join(root, ".cache", "browser-tests", `${randomUUID()}.db`);
process.env.VERDICT_E2E_PYTHON = python;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:3011",
    channel: process.env.VERDICT_E2E_CHANNEL || "chrome",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["iPhone 13"], defaultBrowserType: "chromium" } },
  ],
  webServer: [
    {
      command: `"${python}" ../scripts/browser_fixture.py serve`,
      url: "http://127.0.0.1:8011/health/ready",
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: "node node_modules/next/dist/bin/next build && node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3011",
      url: "http://127.0.0.1:3011",
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        VERDICT_NEXT_DIST_DIR: ".next-e2e",
        VERDICT_API_URL: "http://127.0.0.1:8011",
        NEXT_PUBLIC_VERDICT_WS_URL: "ws://127.0.0.1:8011/v1/stream",
      },
    },
  ],
});
