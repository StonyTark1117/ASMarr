import { useEffect, useState } from "react";
import { Activity, ChevronRight } from "lucide-react";
import "./system-health.css";

type Row = Record<string, any>;
type Snapshot = { health: Row; tasks: Row[]; settings: Row[] };
const date = (value: unknown) => {
  if (!value) return "Never";
  const parsed = new Date(typeof value === "number" ? value * 1000 : String(value));
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : "Unknown";
};
const space = (disk: Row | undefined) =>
  disk && Number.isFinite(disk.available) && disk.total > 0
    ? `${(disk.available / 1024 ** 3).toFixed(1)} GiB free`
    : "Unavailable";

export default function SystemHealth({ status, tick, read, open }: {
  status: Row;
  tick: number;
  read: (path: string) => Promise<any>;
  open: (tab: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let current = true;
    Promise.all([read("health"), read("tasks"), read("settings")])
      .then(([health, tasks, settings]) => {
        if (!Array.isArray(health?.database) || !Array.isArray(health?.sources) ||
            !Array.isArray(health?.failedCommands) || !Array.isArray(tasks) || !Array.isArray(settings))
          throw new Error("Invalid health response");
        if (current) { setSnapshot({ health, tasks, settings }); setFailed(false); }
      })
      .catch(() => { if (current) { setSnapshot(null); setFailed(true); } });
    return () => { current = false; };
  }, [tick, read]);

  const sources: Row[] = snapshot?.health.sources ?? [];
  const sourceIssues = sources.filter((s) => !["healthy", "ok"].includes(s.status));
  const databaseOk = !!snapshot?.health.database.length &&
    snapshot.health.database.every((r: Row) => Object.values(r).every((v) => v === "ok") && Object.keys(r).length > 0);
  const tasks = snapshot?.tasks ?? [];
  const enabled = tasks.filter((t) => t.enabled === 1 || t.enabled === true).length;
  const discovery = tasks.find((t) => t.name === "discovery");
  const backup = tasks.find((t) => t.name === "backup");
  let backupResult: Row = {};
  try { backupResult = JSON.parse(backup?.last_result || "{}"); } catch { /* Report unknown, never success. */ }
  const encrypted = backupResult?.encrypted === true;
  const backupTime = new Date(backup?.last_run ?? "").getTime();
  const backupStale = !!backup && Number.isFinite(backupTime) && backup.interval_seconds > 0 &&
    Date.now() - backupTime > backup.interval_seconds * 2000;
  const reserveValue = snapshot?.settings.find((s) => s.key === "video.free_space_gib")?.value;
  const reserve = reserveValue == null || reserveValue === "" ? NaN : Number(reserveValue);
  const knownReserve = Number.isFinite(reserve) && reserve >= 0;
  const disksKnown = space(status.disk) !== "Unavailable" && space(status.videoDisk) !== "Unavailable";
  const lowSpace = disksKnown && knownReserve &&
    (status.videoDisk.available < reserve * 1024 ** 3 || status.disk.available <= 0);
  const mediaKnown = Array.isArray(status.mediaCounts);
  const media: Row[] = mediaKnown ? status.mediaCounts : [];
  const count = (kind: string, states: string[]) => media
    .filter((m) => m.media_kind === kind && states.includes(m.state))
    .reduce((total, m) => total + Number(m.count), 0);
  const failures = count("Audio", ["failed"]) + count("Video", ["failed"]);
  const commandFailures = snapshot?.health.failedCommands.length ?? 0;
  const attention = !!snapshot && (!databaseOk || sourceIssues.length > 0 || lowSpace ||
    failures > 0 || commandFailures > 0 || enabled < tasks.length || backupStale);
  const incomplete = !sources.length || !tasks.length || !Number.isFinite(backupTime) ||
    !encrypted || !disksKnown || !knownReserve || !mediaKnown;
  const label = failed ? "Unavailable" : !snapshot ? "Loading" : attention ? "Needs attention" : incomplete ? "Checks incomplete" : "Healthy";

  return <section className="panel system-health" aria-labelledby="system-health-title">
    <div className="section-heading">
      <h2 id="system-health-title">System Health</h2>
      <span className={`badge ${failed || attention ? "bad" : snapshot && !incomplete ? "good" : "neutral"}`}>{label}</span>
    </div>
    {!snapshot ? <p role="status">{failed ? "Health checks could not be loaded. Refresh or open System for details." : "Loading current health checks…"}</p> : <>
      <dl className="health-checks">
        <div><dt>Database</dt><dd>{databaseOk ? "Integrity check passed" : "Integrity needs attention"}</dd></div>
        <div><dt>Sources</dt><dd>{sources.length ? `${sources.length - sourceIssues.length} / ${sources.length} last reported healthy` : "Not checked yet"}
          {sourceIssues.length > 0 && <small>{sourceIssues.length} need review</small>}</dd></div>
        <div><dt>Scheduled tasks</dt><dd>{enabled} / {tasks.length} enabled<small>Last successful discovery: {date(discovery?.last_run)}</small></dd></div>
        <div><dt>Audio / video work</dt><dd>{mediaKnown ? <>{count("Audio", ["wanted", "pending", "queued", "downloading"])} audio · {count("Video", ["wanted", "queued", "downloading"])} video pending<small>{failures} failed assets</small></> : "Unavailable"}</dd></div>
        <div><dt>Library space</dt><dd>Audio: {space(status.disk)}<small>Video: {space(status.videoDisk)}</small><small>{knownReserve ? `${reserve} GiB video reserve${lowSpace ? " · Low space" : ""}` : "Video reserve unknown"}</small></dd></div>
        <div><dt>Last successful backup</dt><dd>{date(backup?.last_run)}<small>{!Number.isFinite(backupTime) ? "No completed backup recorded" : encrypted ? "Encrypted recovery bundle" : "Full encrypted recovery not verified"}{backupStale ? " · Overdue" : ""}</small></dd></div>
      </dl>
      {commandFailures > 0 && <p className="health-warning">{commandFailures} recent command failures in history. Review System Health for details.</p>}
      <p className="muted">Source results reflect their last checks. Off-host backup copies are verified separately.</p>
    </>}
    <div className="health-actions">
      <button className="text-button" onClick={() => open("Health")}><Activity size={15} /> View health <ChevronRight size={15} /></button>
      <button className="text-button" onClick={() => open("Tasks")}>Scheduled tasks <ChevronRight size={15} /></button>
    </div>
  </section>;
}
