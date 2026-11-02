import { test, expect } from "@playwright/test";
import { readFileSync, existsSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";

test("password change and operator reset revoke existing sessions", async ({page, request}) => {
  await login(page);
  const path = config();
  const original = readFileSync(path + "/auth.json");
  const initial = readFileSync(path + "/initial-admin.txt");
  const current = initial.toString().split("\n")[1].split(": ")[1];
  const generated = randomUUID();
  const cookieHeader = (cookies: any[]) => cookies.map(c => c.name + "=" + c.value).join("; ");
  const oldCookie = cookieHeader(await page.context().cookies());
  const originalKey = auth().apiKey;
  try {
    const changed = await page.request.post("/api/v1/auth/password", {
      headers: {"X-ASMarr-Request": "1"},
      data: {currentPassword: current, password: generated}
    });
    expect(changed.status()).toBe(200);
    expect((await request.get("/api/v1/auth", {headers: {Cookie: oldCookie}})).status()).toBe(401);
    const signedIn = await page.request.post("/api/v1/auth/login", {
      headers: {"X-ASMarr-Request": "1"}, data: {username: "admin", password: generated}
    });
    expect(signedIn.status()).toBe(200);
    const newerCookie = cookieHeader(await page.context().cookies());
    expect((await page.request.get("/api/v1/auth")).status()).toBe(200);
    // Root-only recovery uses the same hash replacement. Never print its result.
    execFileSync("python3", ["-c", "import importlib.util,pathlib,sys; s=importlib.util.spec_from_file_location('admin','../../ops/admin_login.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.reset_login(pathlib.Path(sys.argv[1]))", path]);
    expect((await request.get("/api/v1/auth", {headers: {Cookie: newerCookie}})).status()).toBe(401);
    expect(auth().apiKey).toBe(originalKey);
    expect((await request.get("/api/v1/auth", {headers: {"X-Api-Key": originalKey}})).status()).toBe(200);
    expect(existsSync(path + "/initial-admin.txt")).toBe(false);
  } finally {
    writeFileSync(path + "/auth.json", original, {mode: 0o600});
    writeFileSync(path + "/initial-admin.txt", initial, {mode: 0o600});
    verifiedSession = null;
  }
});
const config = () =>
  JSON.parse(readFileSync("./runtime/session.json", "utf8")).config;
const auth = () => JSON.parse(readFileSync(config() + "/auth.json", "utf8"));
let verifiedSession: any[] | null = null;
async function login(page: any) {
  // Exercise form login once, then reuse that verified session in isolated page
  // contexts. Repeated workflow setup must not bypass or exhaust the real
  // five-logins-per-minute production limiter.
  if (verifiedSession) {
    await page.context().addCookies(verifiedSession);
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Welcome to your quiet corner." }),
    ).toBeVisible();
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
  await expect
    .poll(async () => {
      const response = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await response.json()).find((c: any) => c.id === 1)?.monitored;
    })
    .toBe(0);
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
  await page
    .getByRole("button", { name: "Quiet Creator", exact: true })
    .click();
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("complete known history");
    await dialog.accept();
  });
  await page.getByLabel("Monitor Videos", { exact: true }).click();
  await expect(
    page.getByLabel("Monitor Videos", { exact: true }),
  ).toBeChecked();
  await expect(page.getByLabel("Video quality profile")).toHaveValue("1");
  await expect
    .poll(async () => {
      const creators = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await creators.json()).find((c: any) => c.id === 1)
        ?.monitor_video;
    })
    .toBe(1);
  await expect
    .poll(async () => {
      const commands = await request.get("/api/v1/commands", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await commands.json()).some(
        (c: any) =>
          c.name === "video-backfill" &&
          ["completed", "failed"].includes(c.state),
      );
    }, { timeout: 20000 })
    .toBe(true);
  const wantedVideo = (
    await (
      await request.get("/api/v1/video/wanted", {
        headers: { "X-Api-Key": auth().apiKey },
      })
    ).json()
  ).find((row: any) => row.key === "fixture:bedtime");
  expect(wantedVideo).toMatchObject({
    media_kind: "Video",
    video_state: "wanted",
    audio_state: "wanted",
  });
  await page.getByLabel("Video quality profile").selectOption("4");
  await expect
    .poll(async () => {
      const creators = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await creators.json()).find((c: any) => c.id === 1)
        ?.video_quality_profile_id;
    })
    .toBe(4);
  await page.getByLabel("Monitor Videos", { exact: true }).uncheck();
  await expect
    .poll(async () => {
      const detail = await request.get(
        "/api/v1/recordings/detail?key=fixture%3Abedtime",
        {
          headers: { "X-Api-Key": auth().apiKey },
        },
      );
      return (await detail.json()).media.find(
        (m: any) => m.media_kind === "Video",
      )?.state;
    })
    .toBe("cancelled");
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByLabel("Monitor Videos", { exact: true }).click();
  await expect
    .poll(async () => {
      const creators = await request.get("/api/v1/creators", {
        headers: { "X-Api-Key": auth().apiKey },
      });
      return (await creators.json()).find((c: any) => c.id === 1)
        ?.monitor_video;
    })
    .toBe(1);
  await expect(
    page.getByLabel("Monitor Videos", { exact: true }),
  ).toBeChecked();
  await expect
    .poll(async () => {
      const detail = await request.get(
        "/api/v1/recordings/detail?key=fixture%3Abedtime",
        { headers: { "X-Api-Key": auth().apiKey } },
      );
      const media = (await detail.json()).media;
      return media.find((m: any) => m.media_kind === "Video")?.state;
    })
    .toBe("wanted");
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  const videoTabs = page.locator(".tabs");
  await expect(
    videoTabs.getByRole("button", { name: "Wanted", exact: true }),
  ).toBeVisible();
  await expect(
    videoTabs.getByRole("button", { name: "Queue", exact: true }),
  ).toBeVisible();
  await expect(
    videoTabs.getByRole("button", { name: "History", exact: true }),
  ).toBeVisible();
  await expect(
    videoTabs.getByRole("button", {
      name: "Interactive Search",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Visual copies are tracked independently from audio."),
  ).toBeVisible();
});
test("wanted recording details and interactive search submission", async ({
  page,
  request,
}) => {
  await login(page);
  await page.getByRole("button", { name: "Wanted", exact: true }).click();
  await page
    .getByText("A quiet bedtime recording", { exact: true })
    .first()
    .click();
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
    page.getByRole("button", { name: "Suppress audio", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry video", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Suppress video", exact: true })
    .click();
  await expect
    .poll(async () => {
      const detail = await request.get(
        "/api/v1/recordings/detail?key=fixture%3Abedtime",
        {
          headers: { "X-Api-Key": auth().apiKey },
        },
      );
      return (await detail.json()).media.find(
        (m: any) => m.media_kind === "Video",
      )?.state;
    })
    .toBe("suppressed");
  await page.getByRole("button", { name: "Retry video", exact: true }).click();
  await expect
    .poll(async () => {
      const detail = await request.get(
        "/api/v1/recordings/detail?key=fixture%3Abedtime",
        {
          headers: { "X-Api-Key": auth().apiKey },
        },
      );
      return (await detail.json()).media.find(
        (m: any) => m.media_kind === "Video",
      )?.state;
    }, { timeout: 10000 })
    .toBe("wanted");
});
test("per-asset SignalR events refresh video lifecycle state", async ({
  page,
  request,
}) => {
  const headers = { "X-Api-Key": auth().apiKey };
  await login(page);
  await expect(page.getByLabel("Live updates connected")).toBeVisible({
    timeout: 10000,
  });
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  await expect(
    page.getByText("A quiet bedtime recording", { exact: true }),
  ).toBeVisible();
  const suppressed = await request.post("/api/v1/recordings/action", {
    headers,
    data: {
      key: "fixture:bedtime",
      action: "suppress",
      mediaKind: "Video",
    },
  });
  expect(suppressed.ok()).toBe(true);
  await expect(
    page.getByText("A quiet bedtime recording", { exact: true }),
  ).not.toBeVisible({ timeout: 5000 });
  const retried = await request.post("/api/v1/recordings/action", {
    headers,
    data: {
      key: "fixture:bedtime",
      action: "retry",
      mediaKind: "Video",
    },
  });
  expect(retried.ok()).toBe(true);
  await expect(
    page.getByText("A quiet bedtime recording", { exact: true }),
  ).toBeVisible({ timeout: 5000 });
});
test("video workspace exposes manual-only interactive search and grab", async ({
  page,
}) => {
  let grabbed: any = null;
  await page.route("**/api/v1/system/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, mode: "production" } });
  });
  await page.route("**/api/v1/video/interactive-search", (route) =>
    route.fulfill({ json: { id: "fixture-video-search" } }),
  );
  await page.route("**/api/v1/commands", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON();
    if (body.name === "grab") {
      grabbed = body;
      return route.fulfill({ json: { id: "fixture-video-grab" } });
    }
    return route.continue();
  });
  await page.route("**/api/v1/commands/fixture-video-search", (route) =>
    route.fulfill({
      json: {
        id: "fixture-video-search",
        state: "completed",
        result: JSON.stringify({
          status: "ok",
          candidates: [
            {
              guid: "manual-video-result",
              title: "Quiet Creator Bedtime 1080p",
              indexer: "Fixture Indexer",
              resolution: 1080,
              interactiveOnly: true,
              autoGrab: false,
            },
          ],
        }),
      },
    }),
  );
  await login(page);
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  await page
    .getByRole("button", { name: "Interactive Search", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Interactive video search" }),
  ).toBeVisible();
  await page.getByText("A quiet bedtime recording", { exact: true }).click();
  await page.getByRole("button", { name: "Search video", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Video search results" }),
  ).toBeVisible();
  await expect(
    page.getByText("Prowlarr video releases are manual-only"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Grab", exact: true }).click();
  await expect.poll(() => grabbed?.arguments?.mediaKind).toBe("Video");
  expect(grabbed.arguments.candidate.guid).toBe("manual-video-result");
  expect(grabbed.arguments.candidate.autoGrab).toBe(false);
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
  await page
    .getByRole("button", { name: "Configure soundgasm", exact: true })
    .click();
  await page
    .getByLabel("Source configuration", { exact: true })
    .fill('{"soundgasm_creators":["Quiet_Creator"]}');
  await page
    .getByRole("button", { name: "Save source configuration", exact: true })
    .click();
  await expect
    .poll(async () => {
      const result = await request.get(
        "/api/v1/connectors/soundgasm/configuration",
        { headers: { "X-Api-Key": auth().apiKey } },
      );
      return (await result.json()).configuration.soundgasm_creators;
    })
    .toEqual(["Quiet_Creator"]);
  await expect(
    page.getByRole("heading", { name: "Rate limits", exact: true }),
  ).toBeVisible();
  const denied = await request.put("/api/v1/connectors/youtube/configuration", {
    headers: { "X-Api-Key": auth().apiKey },
    data: { yt_dlp: "/tmp/untrusted" },
  });
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
    }, { timeout: 10000 })
    .toBe(true);
});
test("dedicated Plex video binding preserves protected integration fields", async ({
  page,
  request,
}) => {
  const headers = { "X-Api-Key": auth().apiKey };
  await login(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Video", exact: true }).click();
  const section = page.getByLabel("Plex Other Videos section ID");
  await expect(section).toHaveValue("9");
  await section.fill("12");
  await page
    .getByRole("button", { name: "Save Plex video binding", exact: true })
    .click();
  await expect
    .poll(async () => {
      const response = await request.get("/api/v1/video/plex-binding", {
        headers,
      });
      return (await response.json()).sectionId;
    })
    .toBe(12);
  const saved = JSON.parse(
    readFileSync(config() + "/integrations.json", "utf8"),
  ).plex;
  expect(saved.video.section_id).toBe(12);
  expect(saved.section_id).toBe(4);
  expect(saved.token).toBe("fixture-secret");
  const bypass = await request.put("/api/v1/integrations/plex", {
    headers,
    data: {
      url: saved.url,
      section_id: saved.section_id,
      token: saved.token,
      video: { section_id: 2 },
    },
  });
  expect(bypass.status()).toBe(400);
  expect((await bypass.json()).error).toContain("dedicated validated video binding");
  expect(
    (
      await (
        await request.get("/api/v1/video/plex-binding", { headers })
      ).json()
    ).sectionId,
  ).toBe(12);
  const wrongLibrary = await request.put("/api/v1/video/plex-binding", {
    headers,
    data: { sectionId: 2 },
  });
  expect(wrongLibrary.status()).toBe(400);
  expect((await wrongLibrary.json()).error).toContain(
    "plex_video_library_must_be_other_videos",
  );
  const rejected = await request.put("/api/v1/video/plex-binding", {
    headers,
    data: { sectionId: 0 },
  });
  expect(rejected.status()).toBe(400);
  expect(
    (
      await (
        await request.get("/api/v1/video/plex-binding", { headers })
      ).json()
    ).sectionId,
  ).toBe(12);
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
  await page.route("**/api/v1/video/history", (route) =>
    route.fulfill({
      json: {
        backfills: [],
        assets: [
          {
            key: "fixture:failed-video",
            title: "Failed visual copy",
            creator: "Quiet Creator",
            audio_state: "complete",
            video_state: "failed",
            state: "complete",
          },
          {
            key: "fixture:imported-video",
            title: "Successful visual copy",
            creator: "Quiet Creator",
            audio_state: "complete",
            video_state: "imported",
            state: "complete",
          },
        ],
      },
    }),
  );
  await login(page);
  await page.getByRole("button", { name: "System", exact: true }).click();
  await page.getByRole("button", { name: "Status", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "System status", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Runtime", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Updates", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Application updates" }),
  ).toBeVisible();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Videos", exact: true }).click();
  await page.getByRole("button", { name: "Failed", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Failed or unavailable videos" }),
  ).toBeVisible();
  await expect(
    page.getByText("Failed visual copy", { exact: true }),
  ).toBeVisible();
  const failedRow = page
    .getByRole("row")
    .filter({ has: page.getByText("Failed visual copy", { exact: true }) });
  await expect(failedRow.getByText("complete", { exact: true })).toBeVisible();
  await expect(failedRow.getByText("failed", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Successful visual copy", { exact: true }),
  ).not.toBeVisible();
});

test("creator profile tags filters and invalid batch rollback", async ({
  page,
  request,
}) => {
  const headers = { "X-Api-Key": auth().apiKey };
  const created = await request.post("/api/v1/profiles", {
    headers,
    data: { name: "Mass sleep profile", settings: { minimumDuration: 0 } },
  });
  expect(created.ok()).toBe(true);
  const profiles = await (
    await request.get("/api/v1/profiles", { headers })
  ).json();
  const profile = profiles.find((p: any) => p.name === "Mass sleep profile");
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await page.getByLabel("Select Quiet Creator").check();
  await page.getByLabel("Select Soft Voice").check();
  await page
    .getByLabel("Mass acquisition profile")
    .selectOption(String(profile.id));
  await page.getByLabel("Mass creator tags").fill("sleep, focus");
  await page
    .getByRole("button", { name: "Apply creator edits", exact: true })
    .click();
  await expect
    .poll(async () => {
      const rows = await (
        await request.get("/api/v1/creators", { headers })
      ).json();
      return rows.every(
        (c: any) =>
          c.profile_id === profile.id && JSON.parse(c.tags).includes("sleep"),
      );
    })
    .toBe(true);
  const rejected = await request.post("/api/v1/creators/mass-edit", {
    headers,
    data: { ids: [1, 99999], monitored: false },
  });
  expect(rejected.status()).toBe(400);
  const unchanged = await (
    await request.get("/api/v1/creators", { headers })
  ).json();
  expect(unchanged.find((c: any) => c.id === 1).monitored).toBe(1);
  await page.getByLabel("Creator tag filter").fill("absent-tag");
  await expect(
    page.getByRole("button", { name: "Quiet Creator", exact: true }),
  ).not.toBeVisible();
  await page.getByLabel("Creator tag filter").fill("sleep");
  await page
    .getByRole("button", { name: "Quiet Creator", exact: true })
    .click();
  await page
    .locator(".monitor-controls")
    .getByLabel("Monitored", { exact: true })
    .uncheck();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByLabel("Creator monitoring filter")
    .selectOption("unmonitored");
  await expect(
    page.getByRole("button", { name: "Quiet Creator", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Soft Voice", exact: true }),
  ).not.toBeVisible();
});

test("manual creator onboarding preserves audio-only defaults", async ({
  page,
  request,
}) => {
  const headers = { "X-Api-Key": auth().apiKey };
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await page.getByRole("button", { name: "Add creator", exact: true }).click();
  await page.getByLabel("New creator name").fill("New Sleep Creator");
  await page.getByLabel("New creator aliases").fill("Sleep Alias");
  await page.getByLabel("New creator tags").fill("sleep");
  await page
    .getByLabel("New creator source", { exact: true })
    .selectOption("soundgasm");
  await page.getByLabel("New creator source handle").fill("SleepAccount");
  await page
    .getByRole("button", { name: "Create creator", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "New Sleep Creator", exact: true }),
  ).toBeVisible();
  const rows = await (
    await request.get("/api/v1/creators", { headers })
  ).json();
  const added = rows.find((c: any) => c.name === "New Sleep Creator");
  expect(added.monitor_video).toBe(0);
  expect(JSON.parse(added.aliases)).toEqual(["Sleep Alias"]);
  const detail = await (
    await request.get("/api/v1/creators/" + added.id, { headers })
  ).json();
  expect(detail.identities.map((i: any) => i.handle)).toEqual(["SleepAccount"]);
  const denied = await request.post("/api/v1/creators", {
    headers,
    data: { name: "../Escape", profileId: 1 },
  });
  expect(denied.status()).toBe(400);
  expect(
    (await (await request.get("/api/v1/creators", { headers })).json()).length,
  ).toBe(rows.length);
  await page.getByRole("button", { name: "Add creator", exact: true }).click();
  await page.getByLabel("New creator name").fill("Visual Sleep Creator");
  await page
    .getByLabel("New creator source", { exact: true })
    .selectOption("youtube");
  await page.getByLabel("New creator source handle").fill("VisualChannel");
  await page.getByLabel("Monitor new creator videos").check();
  await page.getByLabel("New creator video quality profile").selectOption("4");
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("Currently known candidates: 0");
    await dialog.accept();
  });
  await page
    .getByRole("button", { name: "Create creator", exact: true })
    .click();
  await expect
    .poll(async () => {
      const creators = await request.get("/api/v1/creators", { headers });
      return (await creators.json()).find(
        (creator: any) => creator.name === "Visual Sleep Creator",
      );
    })
    .toMatchObject({ monitor_video: 1, video_quality_profile_id: 4 });
  const visual = (
    await (await request.get("/api/v1/creators", { headers })).json()
  ).find((creator: any) => creator.name === "Visual Sleep Creator");
  await expect
    .poll(async () => {
      const commands = await request.get("/api/v1/commands", { headers });
      return (await commands.json()).some(
        (command: any) =>
          command.name === "video-backfill" &&
          JSON.parse(command.arguments).creatorId === visual.id,
      );
    })
    .toBe(true);
});
test("creator removal defaults to keeping files, blocks rediscovery, reconnects and deletes only tracked media", async ({ page, request }) => {
  test.setTimeout(90000);
  const manifest = JSON.parse(readFileSync("./runtime/session.json", "utf8"));
  const headers = { "X-Api-Key": auth().apiKey };
  const name = "Removal Test Reader";
  const added = await request.post("/api/v1/creators", { headers, data: { name, profileId: 1 } });
  expect(added.status()).toBe(201);
  const id = (await added.json()).id;
  const file = execFileSync("python3", ["-c", `
import sqlite3,sys
from pathlib import Path
state=Path(sys.argv[1]);assert state.name=='state' and state.parent.name.startswith('session-')
root=state.parent/'library';root.mkdir(exist_ok=True)
file=root/'removal-test.mp3';file.write_bytes(b'isolated removal recording')
with sqlite3.connect(state/'asmarr.db',timeout=30) as db:
 db.execute("UPDATE settings SET value='production' WHERE key='mode'")
 db.execute("UPDATE settings SET value=? WHERE key='root'",(str(root),))
 db.execute("UPDATE tasks SET enabled=0")
 db.execute("UPDATE commands SET state='cancelled' WHERE state='queued'")
 db.execute("INSERT INTO assets(key,url,targets,source,creator,title,state,saved_path) VALUES('removal:fixture','https://example.invalid/test','[]','fixture',?,'Removal recording','complete',?)",(sys.argv[2],str(file)))
print(file)
`, manifest.state, name], { encoding: "utf8" }).trim();
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Remove creator…", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("radio", { name: /Remove creator, keep files/ })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Prevent automatic re-addition" })).toBeChecked();
  await page.screenshot({ path: "runtime/removal-preview.png", fullPage: true });
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(existsSync(file)).toBe(true);
  expect((await (await request.get("/api/v1/creators/" + id, { headers })).json()).creator.name).toBe(name);
  await page.getByRole("button", { name: "Remove creator…", exact: true }).click();
  await dialog.getByRole("button", { name: "Remove creator, keep files", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Creator removed: " + name })).toBeVisible({ timeout: 45000 });
  await expect(dialog.getByText("Plex: unchanged. Download-client originals were kept.")).toBeVisible();
  expect(existsSync(file)).toBe(true);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  const hidden = await (await request.get("/api/v1/recordings?q=Removal", { headers })).json();
  expect(hidden.some((row: any) => row.key === "removal:fixture")).toBe(false);
  expect((await request.post("/api/v1/creators", { headers, data: { name, profileId: 1 } })).status()).toBe(400);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Removed creators", exact: true }).click();
  await expect(page.getByText(name, { exact: true })).toBeVisible();
  page.once("dialog", d => d.accept());
  await page.getByRole("button", { name: "Allow re-addition", exact: true }).click();
  await expect(page.getByText(name, { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await page.getByRole("button", { name: "Add creator", exact: true }).click();
  await page.getByLabel("New creator name").fill(name);
  await page.getByRole("button", { name: "Create creator", exact: true }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await expect(page.getByText("Removal recording", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Remove creator…", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: /Remove creator and delete files/ }).check();
  const removeButton = dialog.getByRole("button", { name: "Remove and delete files", exact: true });
  await expect(removeButton).toBeDisabled();
  await dialog.getByLabel("Confirm creator name").fill("wrong");
  await expect(removeButton).toBeDisabled();
  await dialog.getByLabel("Confirm creator name").fill(name);
  await removeButton.click();
  await expect(dialog.getByRole("heading", { name: "Creator removed: " + name })).toBeVisible({ timeout: 45000 });
  await expect(dialog.getByText(/1 files deleted/)).toBeVisible();
  expect(existsSync(file)).toBe(false);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  execFileSync("python3", ["-c", "import sqlite3,sys;db=sqlite3.connect(sys.argv[1]+'/asmarr.db');db.execute(\"UPDATE settings SET value='shadow' WHERE key='mode'\");db.commit()", manifest.state]);
});
test("selected creator toolbar supports single and bulk keep/delete removal without touching unselected creators", async ({ page, request }) => {
  test.setTimeout(90000);
  const manifest = JSON.parse(readFileSync("./runtime/session.json", "utf8"));
  const headers = { "X-Api-Key": auth().apiKey };
  const names = ["Bulk Reader Alpha", "Bulk Reader Beta", "Unselected Reader"];
  for (const name of names) {
    expect((await request.post("/api/v1/creators", { headers, data: { name, profileId: 1, monitored: false } })).status()).toBe(201);
  }
  const files = JSON.parse(execFileSync("python3", ["-c", `
import json,sqlite3,sys
from pathlib import Path
state=Path(sys.argv[1]);assert state.name=='state' and state.parent.name.startswith('session-')
root=state.parent/'library';root.mkdir(exist_ok=True)
files=[]
with sqlite3.connect(state/'asmarr.db',timeout=30) as db:
 db.execute("UPDATE settings SET value='production' WHERE key='mode'")
 db.execute("UPDATE settings SET value=? WHERE key='root'",(str(root),))
 db.execute("UPDATE tasks SET enabled=0")
 db.execute("UPDATE commands SET state='cancelled' WHERE state='queued'")
 for i,name in enumerate(json.loads(sys.argv[2])):
  file=root/('bulk-removal-'+str(i)+'.mp3');file.write_bytes(b'isolated batch recording');files.append(str(file))
  db.execute("INSERT INTO assets(key,url,targets,source,creator,title,state,saved_path) VALUES(?,?,'[]','fixture',?,?,'complete',?)",('bulk:fixture:'+str(i),'https://example.invalid/bulk/'+str(i),name,'Batch recording '+str(i),str(file)))
print(json.dumps(files))
`, manifest.state, JSON.stringify(names)], { encoding: "utf8" }));
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  await expect(page.getByRole("button", { name: "Remove Creator", exact: true })).toHaveCount(0);
  await page.getByLabel("Select " + names[0], { exact: true }).check();
  await page.getByRole("button", { name: "Remove Creator", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Remove creator: " + names[0] })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Select " + names[0], { exact: true })).toBeChecked();
  await page.getByLabel("Select " + names[1], { exact: true }).check();
  await page.getByRole("button", { name: "Remove Creators (2)", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("radio", { name: /Remove creators, keep files/ })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Prevent automatic re-addition" })).toBeChecked();
  await expect(dialog.getByText(names[0], { exact: true })).toBeVisible();
  await expect(dialog.getByText(names[1], { exact: true })).toBeVisible();
  await expect(dialog.getByText(names[2], { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "runtime/bulk-removal-preview.png", fullPage: true });
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Select " + names[0], { exact: true })).toBeChecked();
  await page.getByRole("button", { name: "Remove Creators (2)", exact: true }).click();
  await dialog.getByRole("button", { name: "Remove creators, keep files", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Creators removed", exact: true })).toBeVisible({ timeout: 45000 });
  expect(files.every((file: string) => existsSync(file))).toBe(true);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("button", { name: names[2], exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: names[0], exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Remove Creators (2)", exact: true })).toHaveCount(0);
  for (const name of names.slice(0, 2)) {
    await request.delete("/api/v1/creator-exclusions/" + encodeURIComponent(name), { headers });
    expect((await request.post("/api/v1/creators", { headers, data: { name, profileId: 1, monitored: false } })).status()).toBe(201);
  }
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  for (const name of names.slice(0, 2)) await page.getByLabel("Select " + name, { exact: true }).check();
  await page.getByRole("button", { name: "Remove Creators (2)", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: /Remove creators and delete files/ }).check();
  const remove = dialog.getByRole("button", { name: "Remove creators and delete files", exact: true });
  await expect(remove).toBeDisabled();
  await dialog.getByLabel("Confirm bulk removal").fill("REMOVE ALL");
  await expect(remove).toBeDisabled();
  await dialog.getByLabel("Confirm bulk removal").fill("REMOVE 2 CREATORS");
  await remove.click();
  await expect(dialog.getByRole("heading", { name: "Creators removed", exact: true })).toBeVisible({ timeout: 45000 });
  expect(existsSync(files[0])).toBe(false);
  expect(existsSync(files[1])).toBe(false);
  expect(existsSync(files[2])).toBe(true);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("button", { name: names[2], exact: true })).toBeVisible();
  await expect(page.getByLabel("Select " + names[2], { exact: true })).not.toBeChecked();
  execFileSync("python3", ["-c", "import sqlite3,sys;db=sqlite3.connect(sys.argv[1]+'/asmarr.db');db.execute(\"UPDATE settings SET value='shadow' WHERE key='mode'\");db.commit()", manifest.state]);
});
test("bulk removal rejects stale selection and reports partial queue failure without losing remaining selection", async ({ page, request }) => {
  test.setTimeout(60000);
  const manifest = JSON.parse(readFileSync("./runtime/session.json", "utf8"));
  const headers = { "X-Api-Key": auth().apiKey };
  const names = ["Partial Batch Alpha", "Partial Batch Beta"];
  const ids: number[] = [];
  for (const name of names) {
    const added = await request.post("/api/v1/creators", { headers, data: { name, profileId: 1, monitored: false } });
    expect(added.status()).toBe(201); ids.push((await added.json()).id);
  }
  execFileSync("python3", ["-c", "import sqlite3,sys;db=sqlite3.connect(sys.argv[1]+'/asmarr.db');db.execute(\"UPDATE settings SET value='production' WHERE key='mode'\");db.execute('UPDATE tasks SET enabled=0');db.commit()", manifest.state]);
  await login(page);
  await page.getByRole("button", { name: "Creators", exact: true }).click();
  for (const name of names) await page.getByLabel("Select " + name, { exact: true }).check();
  await page.getByRole("button", { name: "Remove Creators (2)", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(names[0], { exact: true })).toBeVisible();
  let submitted = 0;
  page.on("request", req => { if (req.method() === "POST" && /\/creators\/\d+\/remove$/.test(req.url())) submitted++; });
  const previewUrl = `**/api/v1/creators/${ids[0]}/removal-preview`;
  await page.route(previewUrl, async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), planToken: "0".repeat(64) } });
  });
  await dialog.getByRole("button", { name: "Remove creators, keep files", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("nothing was queued");
  expect(submitted).toBe(0);
  await page.unroute(previewUrl);
  await dialog.getByRole("button", { name: "Refresh previews", exact: true }).click();
  await expect(dialog.getByText(names[0], { exact: true })).toBeVisible();
  await page.route(`**/api/v1/creators/${ids[1]}/remove`, route => route.fulfill({ status: 409, json: { error: "Fixture queue conflict" } }));
  await dialog.getByRole("button", { name: "Remove creators, keep files", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Removal results", exact: true })).toBeVisible({ timeout: 30000 });
  await expect(dialog.getByText("No queue confirmation received. Check System → Tasks before retrying; an interrupted response can leave a removal running.")).toBeVisible();
  await expect(dialog.getByRole("alert").first()).toContainText("Fixture queue conflict");
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("button", { name: names[0], exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Select " + names[1], { exact: true })).toBeChecked();
  await page.getByRole("button", { name: "Clear selection", exact: true }).click();
  await expect(page.getByLabel("Select " + names[1], { exact: true })).not.toBeChecked();
  execFileSync("python3", ["-c", "import sqlite3,sys;db=sqlite3.connect(sys.argv[1]+'/asmarr.db');db.execute(\"UPDATE settings SET value='shadow' WHERE key='mode'\");db.commit()", manifest.state]);
});
