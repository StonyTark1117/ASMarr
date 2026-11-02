import { test, expect, Page } from "@playwright/test";

async function dashboard(page: Page, variant: "healthy" | "attention" | "empty" | "error" | "held") {
  const now = new Date().toISOString();
  const disk = { available: 100 * 1024 ** 3, total: 200 * 1024 ** 3 };
  const status = { mode: "production", maintenanceHeld: variant === "held", counts: [], creators: [{count: 2}], sources: [{count: 1}],
    disk, videoDisk: variant === "attention" ? {...disk, available: 1024 ** 3} : disk,
    mediaCounts: [{media_kind: "Audio", state: "wanted", count: 2},
      {media_kind: "Video", state: "queued", count: 3},
      {media_kind: "Video", state: "failed", count: variant === "attention" ? 1 : 0}] };
  const health = { database: [{quick_check: "ok"}],
    sources: variant === "empty" ? [] : [{name: "Fixture", status: variant === "attention" ? "degraded" : "healthy"}],
    failedCommands: variant === "attention" ? [{id: "fixture-failure"}] : [] };
  const tasks = [
    {name: "discovery", enabled: 1, last_run: now, interval_seconds: 86400},
    {name: "backup", enabled: variant === "attention" ? 0 : 1,
      last_run: variant === "empty" ? null : variant === "attention" ? "2020-01-01T00:00:00Z" : now,
      last_result: variant === "empty" ? null : JSON.stringify({encrypted: true}), interval_seconds: 86400},
  ];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1/", "");
    if (path.startsWith("events")) return route.abort();
    if (path === "health" && variant === "error") return route.fulfill({status: 503, body: "Fixture unavailable"});
    const responses: Record<string, unknown> = {auth: {username: "fixture"}, "system/status": status,
      health, tasks, settings: [{key: "video.free_space_gib", value: "20"}], "video/profiles": [], profiles: []};
    return route.fulfill({json: responses[path] ?? {}});
  });
  await page.goto("/");
  return page.getByRole("region", {name: "System Health"});
}

test("System Health replaces rollout with current operational checks and navigation", async ({page}) => {
  const tile = await dashboard(page, "healthy");
  await expect(tile.getByText("Healthy", {exact: true})).toBeVisible();
  await expect(page.getByText("Rollout progress")).toHaveCount(0);
  await expect(page.getByText("Three daily shadow comparisons")).toHaveCount(0);
  await expect(tile.getByText("Integrity check passed")).toBeVisible();
  await expect(tile.getByText("1 / 1 last reported healthy")).toBeVisible();
  await expect(tile.getByText("2 audio · 3 video pending")).toBeVisible();
  await expect(tile.getByText("Encrypted recovery bundle")).toBeVisible();
  await tile.getByRole("button", {name: "View health"}).click();
  await expect(page.getByRole("heading", {name: "System health", exact: true})).toBeVisible();
});

test("System Health exposes failed work, disabled schedules, stale backups and low space", async ({page}) => {
  const tile = await dashboard(page, "attention");
  await expect(tile.getByText("Needs attention", {exact: true})).toBeVisible();
  await expect(tile.getByText("1 need review")).toBeVisible();
  await expect(tile.getByText("1 failed assets")).toBeVisible();
  await expect(tile.getByText("20 GiB video reserve · Low space")).toBeVisible();
  await expect(tile.getByText("Encrypted recovery bundle · Overdue")).toBeVisible();
  await expect(tile.getByText(/recent command failures in history/)).toBeVisible();
  await page.setViewportSize({width: 390, height: 844});
  await expect(tile).toBeVisible();
  expect(await tile.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await tile.screenshot({path: "test-results/system-health-mobile.png"});
  await page.setViewportSize({width: 1440, height: 1000});
  await tile.screenshot({path: "test-results/system-health-desktop.png"});
  await tile.getByRole("button", {name: "Scheduled tasks"}).click();
  await expect(page.getByRole("heading", {name: "Scheduled tasks"})).toBeVisible();
});

test("System Health does not claim healthy before sources and backups have been checked", async ({page}) => {
  const tile = await dashboard(page, "empty");
  await expect(tile.getByText("Checks incomplete", {exact: true})).toBeVisible();
  await expect(tile.getByText("Not checked yet")).toBeVisible();
  await expect(tile.getByText("No completed backup recorded")).toBeVisible();
});

test("System Health fails visibly when its checks cannot be loaded", async ({page}) => {
  const tile = await dashboard(page, "error");
  await expect(tile.getByText("Unavailable", {exact: true})).toBeVisible();
  await expect(tile.getByRole("status")).toContainText("could not be loaded");
  await expect(tile.getByText("Healthy", {exact: true})).toHaveCount(0);
});

test("System Health does not claim healthy while provider work is held", async ({page}) => {
  const tile = await dashboard(page, "held");
  await expect(tile.getByText("Needs attention", {exact: true})).toBeVisible();
  await expect(tile.getByText(/Provider work is held/)).toBeVisible();
});
