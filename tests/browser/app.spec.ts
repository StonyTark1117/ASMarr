import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const config = () => JSON.parse(readFileSync("./runtime/session.json", "utf8")).config;
const auth = () => JSON.parse(readFileSync(config() + "/auth.json", "utf8"));
let verifiedSession: any[] | null = null;
async function login(page: any) {
  // Exercise form login once, then reuse that verified session in isolated page
  // contexts. Repeated workflow setup must not bypass or exhaust the real
  // five-logins-per-minute production limiter.
  if (verifiedSession) {
    await page.context().addCookies(verifiedSession);
    await page.goto("/");
    await expect(page.getByRole("heading", {name:"Welcome to your quiet corner."})).toBeVisible();
    return;
  }
  const password = readFileSync(config() + "/initial-admin.txt", "utf8")
    .split("\n")[1]
    .split(": ")[1];
  await page.goto("/");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Welcome to your quiet corner." }),
  ).toBeVisible();
  verifiedSession = await page.context().cookies();
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
  await page.getByRole("button", { name: "Quiet Creator", exact: true }).click();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("complete known history");
    await dialog.accept();
  });
  await page.getByLabel("Monitor Videos", { exact: true }).click();
  await expect(page.getByLabel("Monitor Videos", { exact: true })).toBeChecked();
  await expect(page.getByLabel("Video quality profile")).toHaveValue("1");
  await expect
    .poll(async () => {
      const creators = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await creators.json()).find((c: any) => c.id === 1)?.monitor_video;
    })
    .toBe(1);
  await expect
    .poll(async () => {
      const commands = await request.get("/api/v1/commands", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await commands.json()).some(
        (c: any) => c.name === "video-backfill" && ["completed", "failed"].includes(c.state),
      );
    })
    .toBe(true);
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  const videoTabs = page.locator(".tabs");
  await expect(videoTabs.getByRole("button", { name: "Wanted", exact: true })).toBeVisible();
  await expect(videoTabs.getByRole("button", { name: "Queue", exact: true })).toBeVisible();
  await expect(videoTabs.getByRole("button", { name: "History", exact: true })).toBeVisible();
  await expect(page.getByText("Visual copies are tracked independently from audio.")).toBeVisible();
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
  await page.getByRole("button", { name: "Configure soundgasm", exact: true }).click();
  await page.getByLabel("Source configuration", { exact: true }).fill('{"soundgasm_creators":["Quiet_Creator"]}');
  await page.getByRole("button", {name:"Save source configuration",exact:true}).click();
  await expect.poll(async () => {
    const result = await request.get("/api/v1/connectors/soundgasm/configuration", {headers:{"X-Api-Key":auth().apiKey}});
    return (await result.json()).configuration.soundgasm_creators;
  }).toEqual(["Quiet_Creator"]);
  await expect(page.getByRole("heading", {name:"Rate limits",exact:true})).toBeVisible();
  const denied = await request.put("/api/v1/connectors/youtube/configuration", {headers:{"X-Api-Key":auth().apiKey},data:{yt_dlp:"/tmp/untrusted"}});
  expect(denied.status()).toBe(400);
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

test("system status updates and failed video workspace", async ({ page }) => {
  await page.route("**/api/v1/video/history", route => route.fulfill({
    json: { backfills: [], assets: [
      { key: "fixture:failed-video", title: "Failed visual copy", creator: "Quiet Creator", audio_state: "complete", video_state: "failed", state: "complete" },
      { key: "fixture:imported-video", title: "Successful visual copy", creator: "Quiet Creator", audio_state: "complete", video_state: "imported", state: "complete" }
    ] }
  }));
  await login(page);
  await page.getByRole("button", { name: "System", exact: true }).click();
  await page.getByRole("button", { name: "Status", exact: true }).click();
  await expect(page.getByRole("heading", { name: "System status", exact: true })).toBeVisible();
  await expect(page.getByText("Runtime", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Updates", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Application updates" })).toBeVisible();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  await page.getByRole("button", { name: "Failed", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Failed or unavailable videos" })).toBeVisible();
  await expect(page.getByText("Failed visual copy", { exact: true })).toBeVisible();
  await expect(page.getByText("Successful visual copy", { exact: true })).not.toBeVisible();
});

test("creator profile tags filters and invalid batch rollback", async ({ page, request }) => {
  const headers = { "X-Api-Key": auth().apiKey };
  const created = await request.post("/api/v1/profiles", { headers, data: {name:"Mass sleep profile",settings:{minimumDuration:0}} });
  expect(created.ok()).toBe(true);
  const profiles = await (await request.get("/api/v1/profiles", { headers })).json();
  const profile = profiles.find((p: any) => p.name === "Mass sleep profile");
  await login(page);
  await page.getByRole("button", {name:"Creators",exact:true}).click();
  await page.getByLabel("Select Quiet Creator").check();
  await page.getByLabel("Select Soft Voice").check();
  await page.getByLabel("Mass acquisition profile").selectOption(String(profile.id));
  await page.getByLabel("Mass creator tags").fill("sleep, focus");
  await page.getByRole("button", {name:"Apply creator edits",exact:true}).click();
  await expect.poll(async () => {
    const rows = await (await request.get("/api/v1/creators",{headers})).json();
    return rows.every((c: any) => c.profile_id === profile.id && JSON.parse(c.tags).includes("sleep"));
  }).toBe(true);
  const rejected = await request.post("/api/v1/creators/mass-edit",{headers,data:{ids:[1,99999],monitored:false}});
  expect(rejected.status()).toBe(400);
  const unchanged = await (await request.get("/api/v1/creators",{headers})).json();
  expect(unchanged.find((c: any) => c.id === 1).monitored).toBe(1);
  await page.getByLabel("Creator tag filter").fill("absent-tag");
  await expect(page.getByRole("button",{name:"Quiet Creator",exact:true})).not.toBeVisible();
  await page.getByLabel("Creator tag filter").fill("sleep");
  await page.getByRole("button",{name:"Quiet Creator",exact:true}).click();
  await page.locator(".monitor-controls").getByLabel("Monitored",{exact:true}).uncheck();
  await page.getByRole("button",{name:"Back",exact:true}).click();
  await page.getByLabel("Creator monitoring filter").selectOption("unmonitored");
  await expect(page.getByRole("button",{name:"Quiet Creator",exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"Soft Voice",exact:true})).not.toBeVisible();
});
