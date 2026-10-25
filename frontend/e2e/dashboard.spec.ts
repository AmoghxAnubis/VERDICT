import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";

const baseline = "replay:adv-synthetic-baseline";
const short = (id: string) => `${id.slice(0, 7)}…${id.slice(-5)}`;

async function replay(page: Page, dataset = baseline) {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Synthetic replay", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Synthetic replay", exact: true }).click();
  await page.getByLabel("Replay scenario").selectOption(dataset);
  await expect(page.locator(".connection")).toContainText("Connected");
  await expect(page.locator(".chart-footer")).toContainText("25 retained rounds");
}

test("live unavailable state, replay evidence, source details, graph and export", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator(".connection")).toContainText("Connected");
  await expect(page.getByText("No diagnostic candidate yet", { exact: true })).toBeVisible();
  await expect(page.getByText("Usable feed value withheld")).toBeVisible();
  await page.getByRole("button", { name: "Synthetic replay", exact: true }).click();
  await page.getByLabel("Replay scenario").selectOption(baseline);
  await expect(page.locator(".metric-card").filter({ hasText: "Diagnostic candidate" })).toContainText("$100.00");
  await expect(page.getByText("Usable feed value withheld")).toBeVisible();
  await page.getByRole("button", { name: "View Coinbase details" }).click();
  const dialog = page.getByRole("dialog", { name: "Coinbase source details" });
  await expect(dialog.getByRole("heading", { name: "Coinbase", exact: true })).toBeVisible();
  await expect(dialog.getByText("Allocated voting weight")).toBeVisible();
  await expect(dialog.locator(".id-list")).not.toContainText("No assessment");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await page.getByRole("navigation").getByRole("button", { name: "Dependencies" }).click();
  await page.getByRole("button", { name: "Inspect source:coinbase", exact: true }).click();
  await expect(page.locator(".graph-inspection strong")).toHaveText("source:coinbase");
  await page.getByRole("navigation").getByRole("button", { name: /^Alerts/ }).click();
  await expect(page.getByRole("heading", { name: "Availability checks", exact: true })).toBeVisible();
  await expect(page.locator(".gate-list")).toContainText("Independent");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: /Export evidence/ }).click();
  expect((await download).suggestedFilename()).toMatch(/^verdict-[a-f0-9]{64}\.json$/);
  await page.getByRole("navigation").getByRole("button", { name: "Overview" }).click();
  await page.screenshot({ path: testInfo.outputPath("baseline.png"), fullPage: true });
  expect(errors).toEqual([]);
});

test("history pins evidence and replay shows attack quarantine and recovery", async ({ page }, testInfo) => {
  await replay(page, "replay:adv-synthetic-single_spike");
  const response = await page.request.get("/api/verdict/dashboard?dataset=replay%3Aadv-synthetic-single_spike");
  const data = await response.json();
  const attack = data.history.find((round: { cutoff: string }) => round.cutoff.endsWith("00:02:00Z"));
  expect(attack).toBeTruthy();
  await page.getByRole("navigation").getByRole("button", { name: "History" }).click();
  await page.getByLabel("Filter round history").fill(attack.report_id);
  await page.getByRole("button", { name: /Inspect round/ }).click();
  await expect(page.locator(".connection")).toContainText("Historical round");
  await page.getByRole("button", { name: "View Coinbase details" }).click();
  await expect(page.getByRole("dialog")).toContainText("Quarantined", { ignoreCase: true });
  await expect(page.getByRole("dialog").locator(".drawer-price")).toContainText("$135.00");
  await page.keyboard.press("Escape");
  await page.screenshot({ path: testInfo.outputPath("quarantine.png"), fullPage: true });
  await page.getByRole("button", { name: /Return to latest round/ }).click();
  await page.getByRole("button", { name: "View Coinbase details" }).click();
  await expect(page.getByRole("dialog").locator(".badge")).toHaveText("Active", { ignoreCase: true });
  await page.keyboard.press("Escape");
  await page.getByRole("navigation").getByRole("button", { name: "Simulations" }).click();
  await page.getByLabel("Replay round").fill("0");
  await expect(page.locator(".pinned-bar")).toContainText("Inspecting retained round");
  await page.getByRole("button", { name: "Play replay" }).click();
  await expect(page.locator(".playback .mono")).not.toHaveText("1 / 25");
  await page.getByRole("button", { name: "Pause replay" }).click();
  await page.getByRole("button", { name: "Live observations", exact: true }).click();
  await expect(page.locator(".replay-banner")).toHaveCount(0);
  await expect(page.locator(".pinned-bar")).toHaveCount(0);
  await expect(page.getByText("No diagnostic candidate yet", { exact: true })).toBeVisible();
});

test("service failure displays an error and retry recovers", async ({ page }, testInfo) => {
  await page.route("**/api/verdict/dashboard?*", route => route.fulfill({ status: 503, body: "unavailable" }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("evidence service is unavailable");
  await expect(page.locator(".connection")).toContainText("Disconnected");
  await page.screenshot({ path: testInfo.outputPath("unavailable.png"), fullPage: true });
  await page.unroute("**/api/verdict/dashboard?*");
  await page.getByRole("button", { name: "Retry now" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".connection")).toContainText("Connected");
  await expect(page.getByText("Usable feed value withheld")).toBeVisible();
});

test("websocket reconnect resumes its cursor and loads a committed assessment", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop", "One real append per isolated test database");
  const sockets: WebSocketRoute[] = [];
  await page.routeWebSocket("**/v1/stream?*", socket => {
    sockets.push(socket);
    socket.connectToServer();
  });
  await replay(page);
  const response = await page.request.get(`/api/verdict/dashboard?dataset=${baseline}`);
  const before = await response.json();
  const current = sockets[sockets.length - 1];
  await current.close({ code: 1012, reason: "browser verification reconnect" });
  const result = JSON.parse(execFileSync(process.env.VERDICT_E2E_PYTHON!, [path.resolve("../scripts/browser_fixture.py"), "append"], { encoding: "utf8" }));
  expect(result.inserted).toBe(true);
  await expect(page.locator(".metric-card").filter({ hasText: "Diagnostic candidate" })).toContainText(short(result.report_id));
  await expect(page.locator(".connection")).toContainText("Connected");
  expect(sockets.some(socket => {
    const url = new URL(socket.url());
    return socket !== current && url.searchParams.get("dataset") === baseline && url.searchParams.get("after") === String(before.cursor);
  })).toBe(true);
  await expect(page.getByText("Usable feed value withheld")).toBeVisible();
});
