import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const config = "./runtime/config";
const auth = () => JSON.parse(readFileSync(config + "/auth.json", "utf8"));
async function login(page: any) {
  const password = readFileSync(config + "/initial-admin.txt", "utf8")
    .split("\n")[1]
    .split(": ")[1];
  await page.goto("/");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome to your quiet corner." }),
  ).toBeVisible();
}
test("form authentication and protected resources", async ({
  page,
  request,
}) => {
  const denied = await request.get("/api/v1/creators");
  expect(denied.status()).toBe(401);
  await login(page);
  const cookies = await page.context().cookies();
  const session = cookies.find((c) => c.name === "__Host-ASMarr");
  expect(session?.secure).toBe(true);
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Strict");
  await page.getByTitle("Sign out").click();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeVisible();
});
test("creator monitoring, identity detail and mass editing", async ({
  page,
  request,
}) => {
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await page
    .getByRole("button", { name: "Quiet Creator", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Linked identities" }),
  ).toBeVisible();
  const monitored = page.getByLabel("Monitored", { exact: true });
  await monitored.uncheck();
  await expect(monitored).not.toBeChecked();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByLabel("Select Quiet Creator").check();
  await page.getByLabel("Select Soft Voice").check();
  await page.getByRole("button", { name: "Monitor 2", exact: true }).click();
  await expect
    .poll(async () => {
      const r = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await r.json()).every((c: any) => c.monitored === 1);
    })
    .toBe(true);
});
test("wanted recording details and interactive search submission", async ({
  page,
  request,
}) => {
  await login(page);
  await page.getByRole("button", { name: "Wanted", exact: true }).click();
  await page.getByText("A quiet bedtime recording", { exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Recording details" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Interactive search", exact: true })
    .click();
  await expect
    .poll(async () => {
      const r = await request.get("/api/v1/commands", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await r.json()).some((c: any) => c.name === "search");
    })
    .toBe(true);
  await expect(
    page.getByRole("button", { name: "Suppress", exact: true }),
  ).toBeVisible();
});
test("settings source toggle and task execution", async ({ page, request }) => {
  await login(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page
    .locator(".source-row")
    .filter({ has: page.getByText("reddit", { exact: true }) })
    .first();
  await row.getByLabel("Enabled").uncheck();
  await expect(row.getByLabel("Enabled")).not.toBeChecked();
  await page.getByRole("button", { name: "System", exact: true }).click();
  const task = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: "health", exact: true }) })
    .first();
  await task.getByRole("button", { name: "Run", exact: true }).click();
  await expect
    .poll(async () => {
      const r = await request.get("/api/v1/commands", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await r.json()).some(
        (c: any) => c.name === "health" && c.state === "completed",
      );
    })
    .toBe(true);
});
test("SignalR pushes live command state to queue screen", async ({
  page,
  request,
}) => {
  let queueRefreshes = 0;
  const errors: string[] = [];
  page.on("response", (response) => {
    if (response.url().endsWith("/api/v1/queue")) queueRefreshes++;
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await login(page);
  await expect(
    page.getByLabel("Live updates connected"),
    errors.join("\n"),
  ).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "Queue", exact: true }).click();
  await expect(
    page.getByText("No commands running. Your library is up to date."),
  ).toBeVisible();
  const baseline = queueRefreshes;
  await request.post("/api/v1/commands", {
    headers: { "X-Api-Key": auth().apiKey },
    data: { name: "health", arguments: {} },
  });
  // The five-second assertion precedes the fifteen-second polling fallback and
  // accepts WebSockets or Server-Sent Events negotiated by SignalR.
  await expect
    .poll(() => queueRefreshes, { timeout: 5000 })
    .toBeGreaterThan(baseline);
  await expect(
    page.getByText("No commands running. Your library is up to date."),
  ).toBeVisible();
});
