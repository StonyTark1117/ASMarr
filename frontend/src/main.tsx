import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { HubConnectionBuilder, LogLevel } from "@microsoft/signalr";
import {
  Activity,
  Users,
  Headphones,
  Search,
  ListMusic,
  History,
  CalendarDays,
  Settings,
  Server,
  LogOut,
  RefreshCw,
  ChevronRight,
  Check,
  Radio,
  Shield,
  HardDrive,
  Plus,
  Play,
  ArrowLeft,
  AlertCircle,
  Download,
  Clock,
  Clapperboard,
} from "lucide-react";
import "./style.css";

type Row = Record<string, any>;
const routes = [
  ["Dashboard", Activity],
  ["Creators", Users],
  ["Recordings", Headphones],
  ["Videos", Clapperboard],
  ["Wanted", Search],
  ["Queue", ListMusic],
  ["History", History],
  ["Calendar", CalendarDays],
  ["Settings", Settings],
  ["System", Server],
] as const;
async function api(path: string, method = "GET", body?: unknown) {
  const r = await fetch("/api/v1/" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-ASMarr-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(
      r.status === 401
        ? "Your session has expired. Sign in to continue."
        : text || `Request failed (${r.status})`,
    );
  }
  return r.status === 204
    ? null
    : await r.text().then((t) => (t ? JSON.parse(t) : null));
}
const when = (v: any) =>
  v
    ? (typeof v === "number"
        ? new Date(v * 1000)
        : new Date(v)
      ).toLocaleString()
    : "Never";
const bytes = (v: number) =>
  v > 1099511627776
    ? `${(v / 1099511627776).toFixed(1)} TB`
    : `${(v / 1073741824).toFixed(1)} GB`;
const initials = (v: string) =>
  v
    .split(/[ _-]/)
    .map((s) => s[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
function Badge({ value }: { value: string }) {
  return (
    <span
      className={
        "badge " +
        (value === "healthy" ||
        value === "complete" ||
        value === "completed" ||
        value === "ok"
          ? "good"
          : value === "failed" || value === "degraded"
            ? "bad"
            : "neutral")
      }
    >
      {value}
    </span>
  );
}
function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Headphones size={28} />
      <p>{text}</p>
    </div>
  );
}
function App() {
  const [auth, setAuth] = useState<boolean | null>(null),
    [page, setPage] = useState("Dashboard"),
    [data, setData] = useState<any>(null),
    [status, setStatus] = useState<Row>({}),
    [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [live, setLive] = useState(false),
    [loading, setLoading] = useState(false),
    [tick, setTick] = useState(0),
    [selected, setSelected] = useState<number[]>([]),
    [creator, setCreator] = useState<Row | null>(null),
    [recording, setRecording] = useState<Row | null>(null),
    [query, setQuery] = useState(""),
    [tab, setTab] = useState("Sources"),
    [username, setUsername] = useState("admin"),
    [password, setPassword] = useState(""),
    [candidates, setCandidates] = useState<Row[] | null>(null),
    [candidateKind, setCandidateKind] = useState("Audio"),
    [videoProfiles, setVideoProfiles] = useState<Row[]>([]),
    [plexVideoBinding, setPlexVideoBinding] = useState<Row>({}),
    [audioProfiles, setAudioProfiles] = useState<Row[]>([]),
    [creatorFilter, setCreatorFilter] = useState("all"),
    [creatorTag, setCreatorTag] = useState(""),
    [batchProfile, setBatchProfile] = useState(""),
    [batchTags, setBatchTags] = useState(""),
    [batchTagMode, setBatchTagMode] = useState("add"),
    [form, setForm] = useState(""),
    [sourcePanel, setSourcePanel] = useState<Row | null>(null),
    [sourceForm, setSourceForm] = useState(""),
    [addingCreator, setAddingCreator] = useState(false),
    [integration, setIntegration] = useState("prowlarr");
  useEffect(() => {
    api("auth")
      .then(() => setAuth(true))
      .catch(() => setAuth(false));
  }, []);
  useEffect(() => {
    if (!auth) return;
    const hub = new HubConnectionBuilder()
      .withUrl("/api/v1/events", { headers: { "X-ASMarr-Request": "1" } })
      .withAutomaticReconnect()
      .configureLogging(LogLevel.Error)
      .build();
    hub.on("status", () => setTick((n) => n + 1));
    hub.on("assetState", () => setTick((n) => n + 1));
    hub.on("backfillProgress", () => setTick((n) => n + 1));
    hub.onreconnecting(() => setLive(false));
    hub.onreconnected(() => setLive(true));
    hub.onclose(() => setLive(false));
    hub
      .start()
      .then(() => setLive(true))
      .catch(() => setLive(false));
    const timer = setInterval(() => setTick((n) => n + 1), 15000);
    return () => {
      clearInterval(timer);
      hub.stop();
    };
  }, [auth]);
  useEffect(() => {
    if (!auth) return;
    let current = true;
    setLoading(true);
    let path = (
      {
        Dashboard: "system/status",
        Creators: "creators",
        Recordings: "recordings?limit=1000",
        Videos:
          tab === "Queue"
            ? "video/queue"
            : tab === "Interactive Search"
              ? "recordings?limit=1000"
              : tab === "History" || tab === "Failed"
                ? "video/history"
                : "video/wanted",
        Wanted: "wanted",
        Queue: "queue",
        History: "history",
        Calendar: "calendar",
        Settings:
          tab === "Sources"
            ? "connectors"
            : tab === "Profiles"
              ? "profiles"
              : tab === "General" || tab === "Video"
                ? "settings"
                : tab === "Integrations"
                  ? "integrations"
                  : "auth",
        System:
          tab === "Status"
            ? "system/status"
            : tab === "Updates"
              ? "system/updates"
              : tab === "Logs"
                ? "logs"
                : tab === "Backups"
                  ? "backups"
                  : tab === "Shadow cycles"
                    ? "shadow-cycles"
                    : tab === "Health"
                      ? "health"
                      : "tasks",
      } as Record<string, string>
    )[page];
    Promise.all([api(path), api("system/status")])
      .then(([d, s]) => {
        if (current) {
          setData(d);
          setStatus(s);
          setError("");
        }
      })
      .catch((e) => {
        if (current) setError(e.message);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [auth, page, tab, tick]);
  useEffect(() => {
    if (!auth) return;
    api("video/profiles")
      .then(setVideoProfiles)
      .catch(() => {});
    api("video/plex-binding")
      .then(setPlexVideoBinding)
      .catch(() => {});
    api("profiles")
      .then(setAudioProfiles)
      .catch(() => {});
  }, [auth, tick]);
  const notify = (s: string) => {
    setToast(s);
    setTimeout(() => setToast(""), 6000);
  };
  const act = async (path: string, method = "POST", body?: unknown) => {
    try {
      const r = await api(path, method, body);
      setTick((n) => n + 1);
      return r;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  };
  const command = async (name: string, args: Row = {}) => {
    const r = await act("commands", "POST", { name, arguments: args });
    if (r) notify(`${name.replaceAll("-", " ")} queued`);
    return r;
  };
  const navigate = (p: string) => {
    setPage(p);
    setData(null);
    setCreator(null);
    setRecording(null);
    setCandidates(null);
    setQuery("");
    setSelected([]);
    setTab(p === "System" ? "Tasks" : p === "Videos" ? "Wanted" : "Sources");
  };
  const openCreator = async (c: Row) => {
    const d = await act("creators/" + c.id, "GET");
    if (d) setCreator(d);
  };
  const openRecording = async (r: Row) => {
    const d = await act(
      "recordings/detail?key=" + encodeURIComponent(r.key),
      "GET",
    );
    if (d) {
      setRecording(d);
      setCandidates(null);
    }
  };
  const search = async (mediaKind = "Audio") => {
    if (!recording) return;
    const c =
      mediaKind === "Video"
        ? await act("video/interactive-search", "POST", {
            key: recording.recording.key,
          })
        : await command("search", {
            key: recording.recording.key,
            mediaKind,
          });
    if (!c) return;
    notify("Searching configured indexers…");
    for (let i = 0; i < 45; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const result = await api("commands/" + c.id);
      if (result.state === "failed") {
        setError(result.error);
        break;
      }
      if (result.state === "completed") {
        const d = JSON.parse(result.result);
        setCandidates(d.candidates);
        setCandidateKind(mediaKind);
        notify(
          d.status === "not_configured"
            ? "Configure Prowlarr in Settings to search."
            : `${d.candidates.length} candidates found`,
        );
        break;
      }
    }
  };
  const toggle = async (c: Row, value: boolean) => {
    if (creator)
      setCreator({
        ...creator,
        creator: { ...creator.creator, monitored: value ? 1 : 0 },
      });
    setData((rows: any) =>
      Array.isArray(rows)
        ? rows.map((row: Row) =>
            row.id === c.id ? { ...row, monitored: value ? 1 : 0 } : row,
          )
        : rows,
    );
    await act("creators/" + c.id, "PUT", {
      monitored: value,
      profileId: c.profile_id,
      tags: JSON.parse(c.tags || "[]"),
    });
  };
  const updateVideo = async (
    c: Row,
    monitorVideo: boolean,
    videoQualityProfileId = c.video_quality_profile_id || 1,
  ) => {
    if (monitorVideo && !c.monitor_video) {
      const preview = await api(`creators/${c.id}/video-backfill-preview`);
      if (
        !window.confirm(
          `Enable video monitoring? ASMarr will scan the creator's complete known history. ${preview.knownCandidates} candidates are already known.`,
        )
      )
        return;
    }
    if (creator) {
      setCreator({
        ...creator,
        creator: {
          ...creator.creator,
          monitor_video: monitorVideo ? 1 : 0,
          video_quality_profile_id: videoQualityProfileId,
        },
      });
    }
    await act(`creators/${c.id}`, "PUT", {
      monitored: !!c.monitored,
      profileId: c.profile_id,
      tags: JSON.parse(c.tags || "[]"),
      monitorVideo,
      videoQualityProfileId,
    });
    await openCreator(c);
    notify(
      monitorVideo
        ? "Full-history video scan queued"
        : "Future video discovery stopped; imported files were retained",
    );
  };
  const recordingTable = (rows: Row[]) =>
    rows.length ? (
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Recording</th>
              <th>Creator</th>
              <th>Source</th>
              <th>Audio</th>
              <th>Video</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {rows
              .filter((r) =>
                (r.title + " " + r.creator)
                  .toLowerCase()
                  .includes(query.toLowerCase()),
              )
              .map((r) => (
                <tr key={r.key} onClick={() => openRecording(r)}>
                  <td className="title-cell">{r.title}</td>
                  <td>{r.creator}</td>
                  <td>
                    <span className="source-label">
                      {r.source?.split(":")[0]}
                    </span>
                  </td>
                  <td>
                    <Badge
                      value={
                        r.media_kind === "Audio"
                          ? r.media_state
                          : r.audio_state || r.state
                      }
                    />
                  </td>
                  <td>
                    <Badge
                      value={
                        r.media_kind === "Video"
                          ? r.media_state
                          : r.video_state || "not monitored"
                      }
                    />
                  </td>
                  <td className="muted">{when(r.published || r.acquired)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    ) : (
      <Empty text="No recordings in this view." />
    );
  if (auth === null)
    return (
      <div className="login">
        <Radio />
        <p>Opening your library…</p>
      </div>
    );
  if (!auth)
    return (
      <div className="login">
        <div className="login-card">
          <div className="brand">
            <span className="brand-icon">
              <Headphones />
            </span>
            ASM<span>arr</span>
          </div>
          <h1>Your quiet corner.</h1>
          <p className="muted">Sign in to your listening library.</p>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api("auth/login", "POST", { username, password });
                setAuth(true);
                setPassword("");
                setError("");
              } catch {
                setError("Unable to sign in. Check your credentials.");
              }
            }}
          >
            <label>
              Username
              <input
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label>
              Password
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && <div className="error">{error}</div>}
            <button className="primary wide">
              Sign in <ChevronRight size={16} />
            </button>
          </form>
          <small>
            <Shield size={13} /> Protected local administrator session
          </small>
        </div>
      </div>
    );
  return (
    <div className="app">
      <aside>
        <div className="brand">
          <span className="brand-icon">
            <Headphones />
          </span>
          ASM<span>arr</span>
        </div>
        <div className="nav-label">YOUR LIBRARY</div>
        <nav>
          {routes.map(([p, Icon]) => (
            <button
              key={p}
              aria-label={p}
              className={page === p ? "active" : ""}
              onClick={() => navigate(p)}
            >
              <Icon size={19} />
              {p}
              {p === "Wanted" &&
                status.counts
                  ?.filter((r: Row) => r.state === "pending")
                  .map((r: Row) => (
                    <span key={r.state} className="nav-count">
                      {r.count}
                    </span>
                  ))}
            </button>
          ))}
        </nav>
        <div
          className="aside-footer"
          aria-label={
            live ? "Live updates connected" : "Live updates reconnecting"
          }
        >
          <span className="pulse" />
          <span>{live ? "Connected to CT130" : "Connecting to CT130"}</span>
          <small>ASMarr v0.2.0</small>
        </div>
      </aside>
      <div className="workspace">
        <header>
          <div>
            <span className="muted">Library</span>
            <ChevronRight size={13} />
            <span>{page}</span>
          </div>
          <div>
            <Badge value={status.mode || "shadow"} />
            <button
              className="icon-button"
              title="Refresh"
              onClick={() => setTick((n) => n + 1)}
            >
              <RefreshCw size={17} className={loading ? "spin" : ""} />
            </button>
            <button
              className="icon-button"
              title="Sign out"
              onClick={async () => {
                await act("auth/logout");
                setAuth(false);
              }}
            >
              <LogOut size={17} />
            </button>
            <span className="avatar admin">A</span>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {page === "Dashboard"
                  ? "A LITTLE MORE PEACE, ALL IN ONE PLACE"
                  : "YOUR LISTENING LIBRARY"}
              </div>
              <h1>
                {recording
                  ? "Recording details"
                  : creator
                    ? creator.creator.name
                    : page === "Dashboard"
                      ? "Welcome to your quiet corner."
                      : page}
              </h1>
              <p className="muted">
                {page === "Dashboard"
                  ? "Discover, collect, and settle in. Your library is right here."
                  : page === "Creators"
                    ? "Keep up with the voices you love."
                    : page === "Wanted"
                      ? "Eligible recordings waiting to join your library."
                      : page === "Settings"
                        ? "Make ASMarr work for your library."
                        : page === "System"
                          ? "Keep your library running smoothly."
                          : ""}
              </p>
            </div>
            <div>
              {(creator || recording) && (
                <button
                  onClick={() => {
                    setCreator(null);
                    setRecording(null);
                    setCandidates(null);
                  }}
                >
                  <ArrowLeft size={15} /> Back
                </button>
              )}
              {page === "Dashboard" && (
                <button
                  className="primary"
                  onClick={() => command("discovery")}
                >
                  <RefreshCw size={16} /> Poll sources
                </button>
              )}
            </div>
          </div>
          {error && (
            <div className="error">
              <AlertCircle size={17} />
              {error}
              <button className="icon-button" onClick={() => setError("")}>
                ×
              </button>
            </div>
          )}
          {status.mode === "shadow" && (
            <div className="shadow-banner">
              <Shield size={18} />
              <div>
                <b>Shadow mode is active</b>
                <span>
                  Discovery previews are enabled. Media acquisition and Plex
                  changes unlock after rollout verification.
                </span>
              </div>
              <button
                onClick={() => {
                  navigate("System");
                  setTab("Shadow cycles");
                }}
              >
                View rollout <ChevronRight size={14} />
              </button>
            </div>
          )}
          {recording ? (
            <>
              <div className="panel recording-detail">
                <div className="large-avatar">
                  <Headphones size={35} />
                </div>
                <div>
                  <h2>{recording.recording.title}</h2>
                  <p>{recording.recording.creator}</p>
                  <div className="media-statuses">
                    {(recording.media || []).map((m: Row) => (
                      <span key={m.media_kind}>
                        <b>{m.media_kind}</b> <Badge value={m.state} />
                      </span>
                    ))}
                  </div>
                  <p className="muted">
                    {recording.recording.source} ·{" "}
                    {when(
                      recording.recording.published ||
                        recording.recording.acquired,
                    )}
                  </p>
                  <code>
                    {recording.recording.saved_path || recording.recording.url}
                  </code>
                </div>
              </div>
              <div className="toolbar">
                <button className="primary" onClick={() => search("Audio")}>
                  <Search size={16} /> Interactive search
                </button>
                <button onClick={() => search("Video")}>
                  <Clapperboard size={16} /> Search video
                </button>
                <button
                  onClick={() =>
                    act("recordings/action", "POST", {
                      key: recording.recording.key,
                      action: "retry",
                      mediaKind: "Audio",
                    })
                  }
                >
                  Retry audio
                </button>
                <button
                  onClick={() =>
                    act("recordings/action", "POST", {
                      key: recording.recording.key,
                      action: "suppress",
                      mediaKind: "Audio",
                    })
                  }
                >
                  Suppress audio
                </button>
                {(recording.media || []).some(
                  (m: Row) => m.media_kind === "Video",
                ) && (
                  <>
                    <button
                      onClick={async () => {
                        await act("recordings/action", "POST", {
                          key: recording.recording.key,
                          action: "retry",
                          mediaKind: "Video",
                        });
                        await openRecording(recording.recording);
                      }}
                    >
                      Retry video
                    </button>
                    <button
                      onClick={async () => {
                        await act("recordings/action", "POST", {
                          key: recording.recording.key,
                          action: "suppress",
                          mediaKind: "Video",
                        });
                        await openRecording(recording.recording);
                      }}
                    >
                      Suppress video
                    </button>
                  </>
                )}
                <button
                  onClick={() =>
                    act("recordings/action", "POST", {
                      key: recording.recording.key,
                      action: "blocklist",
                    })
                  }
                >
                  Blocklist
                </button>
                <button
                  onClick={() =>
                    command("rename-preview", { key: recording.recording.key })
                  }
                >
                  Naming preview
                </button>
              </div>
              {recording.recording.error && (
                <div className="error">{recording.recording.error}</div>
              )}
              {candidates && (
                <section className="panel">
                  <h2>{candidateKind} search results</h2>
                  {candidateKind === "Video" && (
                    <p className="muted">
                      Prowlarr video releases are manual-only and are never
                      automatically grabbed.
                    </p>
                  )}
                  {candidates.length ? (
                    <table>
                      <thead>
                        <tr>
                          <th>Release</th>
                          <th>Indexer</th>
                          <th>
                            {candidateKind === "Video"
                              ? "Resolution"
                              : "Confidence"}
                          </th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {candidates.map((c, i) => (
                          <tr key={i}>
                            <td>{c.title}</td>
                            <td>{c.indexer}</td>
                            <td>
                              {candidateKind === "Video"
                                ? c.resolution
                                  ? `${c.resolution}p`
                                  : "Unknown"
                                : `${Math.round(c.confidence * 100)}%`}
                            </td>
                            <td>
                              <button
                                disabled={status.mode === "shadow"}
                                onClick={() =>
                                  command("grab", {
                                    key: recording.recording.key,
                                    candidate: c,
                                    mediaKind: candidateKind,
                                  })
                                }
                              >
                                <Download size={15} /> Grab
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <Empty text="No matching candidates." />
                  )}
                </section>
              )}
              <section className="panel">
                <h2>Alternate source candidates</h2>
                {JSON.parse(recording.recording.targets || "[]").map(
                  (c: string[], i: number) => (
                    <div key={i} className="source-row">
                      <Badge value={c[0]} />
                      <a href={c[1]} target="_blank" rel="noreferrer">
                        {c[1]}
                      </a>
                    </div>
                  ),
                )}
              </section>
            </>
          ) : creator ? (
            <>
              <section className="panel">
                <div className="section-heading">
                  <h2>Linked identities</h2>
                  <div className="monitor-controls">
                    <label className="switch-label">
                      <input
                        aria-label="Monitored"
                        type="checkbox"
                        checked={!!creator.creator.monitored}
                        onChange={(e) =>
                          toggle(creator.creator, e.target.checked)
                        }
                      />{" "}
                      Monitor audio
                    </label>
                    <label className="switch-label">
                      <input
                        aria-label="Monitor Videos"
                        type="checkbox"
                        checked={!!creator.creator.monitor_video}
                        onChange={(e) =>
                          updateVideo(creator.creator, e.target.checked)
                        }
                      />{" "}
                      Monitor Videos
                    </label>
                    <label>
                      Video quality
                      <select
                        aria-label="Video quality profile"
                        value={creator.creator.video_quality_profile_id || 1}
                        disabled={!creator.creator.monitor_video}
                        onChange={(e) =>
                          updateVideo(
                            creator.creator,
                            true,
                            Number(e.target.value),
                          )
                        }
                      >
                        {videoProfiles.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                </div>
                {creator.backfill && (
                  <div className="backfill-progress">
                    <div>
                      <b>Full-history video scan</b>
                      <Badge value={creator.backfill.state} />
                    </div>
                    <progress
                      value={creator.backfill.eligible || 0}
                      max={Math.max(creator.backfill.total || 1, 1)}
                    />
                    <span>
                      {creator.backfill.eligible || 0} eligible ·{" "}
                      {creator.backfill.discovered || 0} inspected
                    </span>
                  </div>
                )}
                {creator.identities.map((i: Row) => (
                  <div className="source-row" key={i.id}>
                    <Radio size={17} />
                    <b>{i.kind}</b>
                    <span>{i.handle}</span>
                    <label>
                      <input
                        type="checkbox"
                        checked={!!i.enabled}
                        onChange={async (e) => {
                          const enabled = e.target.checked;
                          setCreator({
                            ...creator,
                            identities: creator.identities.map(
                              (identity: Row) =>
                                identity.id === i.id
                                  ? { ...identity, enabled: enabled ? 1 : 0 }
                                  : identity,
                            ),
                          });
                          await act("identities/" + i.id, "PUT", {
                            enabled,
                          });
                          await openCreator(creator.creator);
                        }}
                      />{" "}
                      Enabled
                    </label>
                  </div>
                ))}
                <form
                  className="inline-form"
                  onSubmit={async (e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    await act("identities", "POST", {
                      creatorId: creator.creator.id,
                      kind: f.get("kind"),
                      handle: f.get("handle"),
                      enabled: true,
                    });
                    await openCreator(creator.creator);
                  }}
                >
                  <select name="kind">
                    <option>reddit</option>
                    <option>soundgasm</option>
                    <option>youtube</option>
                  </select>
                  <input
                    name="handle"
                    placeholder="Account or channel ID"
                    required
                  />
                  <button>
                    <Plus size={15} /> Link identity
                  </button>
                </form>
                <p className="muted">
                  Library path: <code>{creator.creator.path}</code>
                </p>
              </section>
              {recordingTable(creator.recordings)}
            </>
          ) : page === "Dashboard" ? (
            <>
              <div className="stats-grid">
                {[
                  [
                    Headphones,
                    "Recordings",
                    status.counts?.reduce(
                      (a: number, r: Row) => a + Number(r.count),
                      0,
                    ) || 0,
                    "Across all sources",
                  ],
                  [
                    Users,
                    "Creators",
                    status.creators?.[0]?.count || 0,
                    "Voices in your library",
                  ],
                  [
                    Radio,
                    "Source endpoints",
                    status.sources?.[0]?.count || 0,
                    "Direct discovery first",
                  ],
                  [
                    HardDrive,
                    "Free space",
                    bytes(status.disk?.available || 0),
                    "On your library volume",
                  ],
                ].map(([Icon, label, value, sub]: any) => (
                  <div className="stat-card" key={label}>
                    <div className="stat-top">
                      <span>{label}</span>
                      <Icon size={20} />
                    </div>
                    <strong>{value}</strong>
                    <small>{sub}</small>
                  </div>
                ))}
              </div>
              <div className="dashboard-grid">
                <section className="panel">
                  <div className="section-heading">
                    <h2>Library at a glance</h2>
                    <span className="muted">Current inventory</span>
                  </div>
                  <div className="state-list">
                    {status.counts?.map((r: Row) => (
                      <div key={r.state}>
                        <span>
                          <span className={"dot " + r.state} />
                          {r.state}
                        </span>
                        <strong>{r.count}</strong>
                      </div>
                    ))}
                  </div>
                  <div className="soft-card">
                    <Headphones size={24} />
                    <div>
                      <b>A home for every recording.</b>
                      <p>
                        Direct sources, a durable library, and your favorite
                        voices.
                      </p>
                    </div>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => navigate("Creators")}
                  >
                    Explore your creators <ChevronRight size={15} />
                  </button>
                </section>
                <section className="panel">
                  <div className="section-heading">
                    <h2>Rollout progress</h2>
                    <Badge value={status.mode} />
                  </div>
                  <div className="rollout">
                    <div>
                      <Check size={17} />
                      <span>Production state imported</span>
                    </div>
                    <div>
                      <Clock size={17} />
                      <span>Three daily shadow comparisons</span>
                      <b>{status.qualifiedShadowDays || 0} / 3</b>
                    </div>
                    <div>
                      <Shield size={17} />
                      <span>Production acceptance & cutover</span>
                    </div>
                  </div>
                  <p className="muted">
                    The existing scraper remains active during observation.
                  </p>
                  <button
                    className="text-button"
                    onClick={() => {
                      navigate("System");
                      setTab("Tasks");
                    }}
                  >
                    Scheduled tasks <ChevronRight size={15} />
                  </button>
                </section>
              </div>
              <section className="panel">
                <div className="section-heading">
                  <h2>Keep your library in sync</h2>
                  <span className="muted">Quick actions</span>
                </div>
                <div className="quick-actions">
                  {[
                    ["disk-scan", "Rescan library", HardDrive],
                    ["plex-verify", "Verify Plex", Radio],
                    ["backup", "Create backup", Shield],
                    ["playlists", "Preview playlists", ListMusic],
                  ].map(([name, label, Icon]: any) => (
                    <button key={name} onClick={() => command(name)}>
                      <Icon size={20} />
                      <span>{label}</span>
                      <ChevronRight size={15} />
                    </button>
                  ))}
                </div>
              </section>
            </>
          ) : page === "Creators" ? (
            <>
              <button onClick={() => setAddingCreator(!addingCreator)}>
                <Plus size={15} /> Add creator
              </button>
              {addingCreator && (
                <section className="panel">
                  <h2>Add a creator</h2>
                  <form
                    className="inline-form"
                    onSubmit={async (e) => {
                      e.preventDefault();
                      const values = new FormData(e.currentTarget);
                      const split = (key: string) =>
                        String(values.get(key) || "")
                          .split(",")
                          .map((x) => x.trim())
                          .filter(Boolean);
                      const handle = String(values.get("handle") || "").trim();
                      const monitorVideo = values.get("monitorVideo") === "on";
                      if (
                        monitorVideo &&
                        !window.confirm(
                          "Enabling Monitor Videos queues the creator's complete known history. Currently known candidates: 0. Continue?",
                        )
                      )
                        return;
                      if (
                        await act("creators", "POST", {
                          name: values.get("name"),
                          profileId: Number(values.get("profile")),
                          aliases: split("aliases"),
                          tags: split("tags"),
                          monitored: values.get("monitored") === "on",
                          monitorVideo,
                          videoQualityProfileId: Number(
                            values.get("videoProfile"),
                          ),
                          identities: handle
                            ? [
                                {
                                  kind: values.get("kind"),
                                  handle,
                                  enabled: true,
                                },
                              ]
                            : [],
                        })
                      ) {
                        setAddingCreator(false);
                        notify("Creator added");
                      }
                    }}
                  >
                    <label>
                      Name
                      <input
                        aria-label="New creator name"
                        name="name"
                        maxLength={100}
                        required
                      />
                    </label>
                    <label>
                      Aliases
                      <input
                        aria-label="New creator aliases"
                        name="aliases"
                        placeholder="Comma-separated aliases"
                      />
                    </label>
                    <label>
                      Tags
                      <input
                        aria-label="New creator tags"
                        name="tags"
                        placeholder="Comma-separated tags"
                      />
                    </label>
                    <label>
                      Acquisition profile
                      <select
                        aria-label="New creator acquisition profile"
                        name="profile"
                        required
                      >
                        {audioProfiles.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Initial source
                      <select aria-label="New creator source" name="kind">
                        <option>soundgasm</option>
                        <option>reddit</option>
                        <option>youtube</option>
                      </select>
                    </label>
                    <label>
                      Account or channel ID
                      <input
                        aria-label="New creator source handle"
                        name="handle"
                        maxLength={100}
                      />
                    </label>
                    <label>
                      <input
                        aria-label="Monitor new creator audio"
                        name="monitored"
                        type="checkbox"
                        defaultChecked
                      />{" "}
                      Monitor audio
                    </label>
                    <label>
                      <input
                        aria-label="Monitor new creator videos"
                        name="monitorVideo"
                        type="checkbox"
                      />{" "}
                      Monitor videos
                    </label>
                    <label>
                      Video quality profile
                      <select
                        aria-label="New creator video quality profile"
                        name="videoProfile"
                        defaultValue="1"
                      >
                        {videoProfiles.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button className="primary" type="submit">
                      Create creator
                    </button>
                  </form>
                  <p className="muted">
                    The library path is derived from the configured root. This
                    creates database records only and no directory or download.
                    Video monitoring is optional; enabling it authorizes a
                    resumable complete-history scan. Link more identities from
                    the creator detail page.
                  </p>
                </section>
              )}
              <div className="toolbar">
                <div className="search">
                  <Search size={17} />
                  <input
                    placeholder="Search creators…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </div>
                <span className="muted">{data?.length || 0} creators</span>
                <select
                  aria-label="Creator monitoring filter"
                  value={creatorFilter}
                  onChange={(e) => setCreatorFilter(e.target.value)}
                >
                  <option value="all">All creators</option>
                  <option value="monitored">Monitored</option>
                  <option value="unmonitored">Unmonitored</option>
                  <option value="video">Video monitored</option>
                </select>
                <input
                  aria-label="Creator tag filter"
                  placeholder="Filter by tag…"
                  value={creatorTag}
                  onChange={(e) => setCreatorTag(e.target.value)}
                />
                {selected.length > 0 && (
                  <>
                    <button
                      onClick={async () => {
                        await act("creators/mass-edit", "POST", {
                          ids: selected,
                          monitored: true,
                        });
                        setSelected([]);
                      }}
                    >
                      Monitor {selected.length}
                    </button>
                    <button
                      onClick={async () => {
                        await act("creators/mass-edit", "POST", {
                          ids: selected,
                          monitored: false,
                        });
                        setSelected([]);
                      }}
                    >
                      Unmonitor
                    </button>
                  </>
                )}
              </div>
              {selected.length > 0 && (
                <section className="panel">
                  <h2>Edit {selected.length} selected creators</h2>
                  <div className="toolbar">
                    <select
                      aria-label="Mass acquisition profile"
                      value={batchProfile}
                      onChange={(e) => setBatchProfile(e.target.value)}
                    >
                      <option value="">Keep acquisition profiles</option>
                      {audioProfiles.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                    <select
                      aria-label="Mass tag action"
                      value={batchTagMode}
                      onChange={(e) => setBatchTagMode(e.target.value)}
                    >
                      <option value="add">Add tags</option>
                      <option value="replace">Replace tags</option>
                    </select>
                    <input
                      aria-label="Mass creator tags"
                      placeholder="Comma-separated tags"
                      value={batchTags}
                      onChange={(e) => setBatchTags(e.target.value)}
                    />
                    <button
                      className="primary"
                      disabled={
                        !batchProfile &&
                        !batchTags.trim() &&
                        batchTagMode !== "replace"
                      }
                      onClick={async () => {
                        const tags = batchTags
                          .split(",")
                          .map((tag) => tag.trim())
                          .filter(Boolean);
                        const edit: Row = { ids: selected };
                        if (batchProfile) edit.profileId = Number(batchProfile);
                        if (batchTagMode === "replace") edit.tags = tags;
                        else if (tags.length) edit.addTags = tags;
                        if (await act("creators/mass-edit", "POST", edit)) {
                          setSelected([]);
                          setBatchProfile("");
                          setBatchTags("");
                          notify("Creator edits saved");
                        }
                      }}
                    >
                      Apply creator edits
                    </button>
                  </div>
                  <p className="muted">
                    Adding tags preserves existing labels. Replacing with an
                    empty list clears tags. Audio profile edits do not enable
                    video monitoring.
                  </p>
                </section>
              )}
              <div className="creator-grid">
                {Array.isArray(data) &&
                  data
                    .filter(
                      (c: Row) =>
                        [
                          c.name,
                          ...JSON.parse(c.aliases || "[]"),
                          ...JSON.parse(c.tags || "[]"),
                        ]
                          .join(" ")
                          .toLowerCase()
                          .includes(query.toLowerCase()) &&
                        (creatorFilter === "all" ||
                          (creatorFilter === "monitored" && !!c.monitored) ||
                          (creatorFilter === "unmonitored" && !c.monitored) ||
                          (creatorFilter === "video" && !!c.monitor_video)) &&
                        (!creatorTag ||
                          JSON.parse(c.tags || "[]").some((tag: string) =>
                            tag
                              .toLowerCase()
                              .includes(creatorTag.toLowerCase()),
                          )),
                    )
                    .map((c: Row, i: number) => (
                      <article className="creator-card" key={c.id}>
                        <div
                          className="creator-art"
                          style={
                            {
                              "--hue": (i * 37 + 160) % 360,
                            } as React.CSSProperties
                          }
                          onClick={() => openCreator(c)}
                        >
                          <div className="art-orbit" />
                          <span>{initials(c.name)}</span>
                          <input
                            aria-label={"Select " + c.name}
                            type="checkbox"
                            checked={selected.includes(c.id)}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) =>
                              setSelected(
                                e.target.checked
                                  ? [...selected, c.id]
                                  : selected.filter((id) => id !== c.id),
                              )
                            }
                          />
                          <span className="art-badge">
                            <Headphones size={12} /> {c.recordings}
                          </span>
                        </div>
                        <div className="creator-info">
                          <button onClick={() => openCreator(c)}>
                            {c.name}
                          </button>
                          <div>
                            <span>
                              {c.audio_completed || 0} audio ·{" "}
                              {c.video_completed || 0} video
                            </span>
                            <label title="Toggle monitoring">
                              <input
                                type="checkbox"
                                checked={!!c.monitored}
                                onChange={(e) => toggle(c, e.target.checked)}
                              />
                              <span>
                                {c.monitored ? "Monitored" : "Paused"}
                              </span>
                            </label>
                          </div>
                        </div>
                      </article>
                    ))}
              </div>
            </>
          ) : page === "Videos" ? (
            <>
              <div className="tabs">
                {[
                  "Wanted",
                  "Queue",
                  "History",
                  "Failed",
                  "Interactive Search",
                ].map((t) => (
                  <button
                    className={tab === t ? "active" : ""}
                    onClick={() => setTab(t)}
                    key={t}
                  >
                    {t}
                  </button>
                ))}
              </div>
              <div className="toolbar">
                <span className="muted">
                  Visual copies are tracked independently from audio.
                </span>
                {tab === "Wanted" && (
                  <button
                    disabled={status.mode === "shadow"}
                    onClick={() => command("video-queue")}
                  >
                    <Play size={15} /> Process one video
                  </button>
                )}
                <button onClick={() => command("plex-video-verify")}>
                  <Radio size={15} /> Validate Plex video library
                </button>
              </div>
              {tab === "Wanted" && Array.isArray(data) && recordingTable(data)}
              {tab === "Queue" && (
                <>
                  {data?.backfills?.map((job: Row) => (
                    <div className="source-row" key={job.id}>
                      <RefreshCw size={16} />
                      <b>History scan</b>
                      <Badge value={job.state} />
                      <span>
                        {job.eligible} eligible / {job.discovered} inspected
                      </span>
                    </div>
                  ))}
                  {data?.direct && recordingTable(data.direct)}
                  {data?.downloads?.map((job: Row) => (
                    <div className="source-row" key={job.id}>
                      <Download size={16} />
                      <b>{job.provider}</b>
                      <Badge value={job.state} />
                    </div>
                  ))}
                </>
              )}
              {tab === "History" && (
                <>
                  {data?.backfills?.map((job: Row) => (
                    <div className="source-row" key={job.id}>
                      <Clock size={16} />
                      <b>Full-history scan</b>
                      <Badge value={job.state} />
                      <span>{when(job.finished || job.updated)}</span>
                    </div>
                  ))}
                  {data?.assets && recordingTable(data.assets)}
                </>
              )}
              {tab === "Failed" && (
                <>
                  <h2>Failed or unavailable videos</h2>
                  <p className="muted">
                    Failed transfers can be retried from recording details.
                    Deleted or unsupported releases remain visible without
                    blocking the remaining history scan.
                  </p>
                  {data?.assets &&
                    recordingTable(
                      data.assets.filter((asset: Row) =>
                        ["failed", "unavailable"].includes(
                          asset.video_state || asset.media_state || asset.state,
                        ),
                      ),
                    )}
                </>
              )}
              {tab === "Interactive Search" && (
                <>
                  <div className="toolbar">
                    <div className="search">
                      <Search size={17} />
                      <input
                        aria-label="Search recordings for video"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Choose a recording to search…"
                      />
                    </div>
                  </div>
                  <h2>Interactive video search</h2>
                  <p className="muted">
                    Choose a recording, then search Prowlarr for manual-only
                    video releases. Results are never grabbed automatically.
                  </p>
                  {Array.isArray(data) && recordingTable(data)}
                </>
              )}
            </>
          ) : page === "Recordings" || page === "Wanted" ? (
            <>
              <div className="toolbar">
                <div className="search">
                  <Search size={17} />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search recordings…"
                  />
                </div>
                <span className="muted">{data?.length || 0} recordings</span>
                {page === "Wanted" && (
                  <button
                    disabled={status.mode === "shadow"}
                    onClick={() => command("queue")}
                  >
                    <Play size={15} /> Process queue
                  </button>
                )}
              </div>
              {Array.isArray(data) && recordingTable(data)}
            </>
          ) : page === "Queue" ? (
            <>
              <section className="panel">
                <h2>Active commands</h2>
                {data?.commands?.length ? (
                  data.commands.map((c: Row) => (
                    <div className="source-row" key={c.id}>
                      <RefreshCw size={17} />
                      <b>{c.name}</b>
                      <Badge value={c.state} />
                      <span>{when(c.created)}</span>
                    </div>
                  ))
                ) : (
                  <Empty text="No commands running. Your library is up to date." />
                )}
              </section>
              <section className="panel">
                <h2>Acquisition queue</h2>
                {data?.direct && recordingTable(data.direct)}
                {data?.downloads?.map((c: Row) => (
                  <div className="source-row" key={c.id}>
                    <b>{c.provider}</b>
                    <Badge value={c.state} />
                    <span>{c.download_id}</span>
                  </div>
                ))}
              </section>
            </>
          ) : page === "History" ? (
            <>
              <section className="panel">
                <h2>Recent events</h2>
                {data?.events?.length ? (
                  data.events.map((r: Row) => (
                    <div className="source-row" key={r.id}>
                      <Activity size={16} />
                      <b>{r.event}</b>
                      <span>{when(r.at)}</span>
                      <code>{r.details}</code>
                    </div>
                  ))
                ) : (
                  <Empty text="Activity appears here as ASMarr runs." />
                )}
              </section>
              <section className="panel">
                <h2>Imported acquisition history</h2>
                <table>
                  <thead>
                    <tr>
                      <th>Recording</th>
                      <th>Creator</th>
                      <th>Acquired</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data?.imports?.map((r: Row) => (
                      <tr key={r.id}>
                        <td>{r.title}</td>
                        <td>{r.creator}</td>
                        <td>{when(r.ts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            </>
          ) : page === "Calendar" ? (
            <section className="panel">
              <h2>Publication calendar</h2>
              {Array.isArray(data) && data.length ? (
                Object.entries(
                  data.reduce((groups: Record<string, Row[]>, r: Row) => {
                    const day = new Date(r.published * 1000).toLocaleDateString(
                      undefined,
                      { month: "long", day: "numeric", year: "numeric" },
                    );
                    (groups[day] ??= []).push(r);
                    return groups;
                  }, {}),
                )
                  .slice(0, 45)
                  .map(([day, rows]: any) => (
                    <div className="calendar-day" key={day}>
                      <h3>{day}</h3>
                      {rows.map((r: Row) => (
                        <button key={r.key} onClick={() => openRecording(r)}>
                          <Headphones size={16} />
                          <span>
                            {r.creator}
                            <small>{r.title}</small>
                          </span>
                          <Badge value={r.state} />
                        </button>
                      ))}
                    </div>
                  ))
              ) : (
                <Empty text="Publication dates will appear here." />
              )}
            </section>
          ) : page === "Settings" ? (
            <>
              <div className="tabs">
                {[
                  "Sources",
                  "Profiles",
                  "General",
                  "Video",
                  "Integrations",
                  "Authentication",
                ].map((t) => (
                  <button
                    className={tab === t ? "active" : ""}
                    onClick={() => {
                      setTab(t);
                      setForm("");
                    }}
                    key={t}
                  >
                    {t}
                  </button>
                ))}
              </div>
              {tab === "Sources" ? (
                <>
                  <section className="panel">
                    <h2>Source connectors</h2>
                    {data?.configuration?.map((c: Row) => (
                      <div className="source-row" key={c.kind}>
                        <Radio size={18} />
                        <b>{c.kind}</b>
                        <span className="muted">Direct discovery</span>
                        <label>
                          <input
                            type="checkbox"
                            checked={c.enabled}
                            onChange={(e) => {
                              const enabled = e.target.checked;
                              setData({
                                ...data,
                                configuration: data.configuration.map(
                                  (source: Row) =>
                                    source.kind === c.kind
                                      ? { ...source, enabled }
                                      : source,
                                ),
                              });
                              void act("connectors/" + c.kind, "PUT", {
                                enabled,
                              });
                            }}
                          />{" "}
                          Enabled
                        </label>
                        <button
                          onClick={async () => {
                            try {
                              const panel = await api(
                                "connectors/" + c.kind + "/configuration",
                              );
                              setSourcePanel(panel);
                              setSourceForm(
                                JSON.stringify(panel.configuration, null, 2),
                              );
                            } catch (e) {
                              setError(String(e));
                            }
                          }}
                        >
                          Configure {c.kind}
                        </button>
                        <button
                          onClick={() =>
                            command("source-test", { kind: c.kind })
                          }
                        >
                          Test
                        </button>
                        <button
                          onClick={() => command("discovery", { kind: c.kind })}
                        >
                          Poll
                        </button>
                      </div>
                    ))}
                  </section>
                  {sourcePanel && (
                    <section className="panel">
                      <h2>{sourcePanel.kind} source configuration</h2>
                      <p className="muted">
                        Public discovery filters and polling limits only.
                        Credentials and pinned executable paths are kept in
                        protected server configuration. Changing filters can
                        invalidate shadow parity and requires review before
                        cutover.
                      </p>
                      <textarea
                        aria-label="Source configuration"
                        rows={12}
                        value={sourceForm}
                        onChange={(e) => setSourceForm(e.target.value)}
                      />
                      <button
                        onClick={async () => {
                          try {
                            await api(
                              "connectors/" +
                                sourcePanel.kind +
                                "/configuration",
                              "PUT",
                              JSON.parse(sourceForm),
                            );
                            setSourcePanel(
                              await api(
                                "connectors/" +
                                  sourcePanel.kind +
                                  "/configuration",
                              ),
                            );
                            setToast("Source configuration saved");
                          } catch (e) {
                            setError(String(e));
                          }
                        }}
                      >
                        Save source configuration
                      </button>
                      <button onClick={() => setSourcePanel(null)}>
                        Close source configuration
                      </button>
                      <h3>Checkpoints</h3>
                      <p>{sourcePanel.checkpointPolicy}</p>
                      <pre>
                        {JSON.stringify(sourcePanel.checkpoints, null, 2)}
                      </pre>
                      <h3>Rate limits</h3>
                      <pre>
                        {JSON.stringify(sourcePanel.rateLimits, null, 2)}
                      </pre>
                      <h3>Latest test result</h3>
                      <pre>
                        {JSON.stringify(
                          sourcePanel.latestTest || "No test recorded",
                          null,
                          2,
                        )}
                      </pre>
                    </section>
                  )}
                  <section className="panel">
                    <h2>Endpoint health</h2>
                    <table>
                      <thead>
                        <tr>
                          <th>Source</th>
                          <th>Health</th>
                          <th>Last success</th>
                          <th>Details & checkpoint</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data?.sources?.map((s: Row) => (
                          <tr key={s.name}>
                            <td>{s.name}</td>
                            <td>
                              <Badge value={s.status} />
                            </td>
                            <td>{when(s.last_success)}</td>
                            <td>
                              <details>
                                <summary>View report</summary>
                                <pre>
                                  {JSON.stringify(
                                    JSON.parse(s.details || "{}"),
                                    null,
                                    2,
                                  )}
                                </pre>
                              </details>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </section>
                </>
              ) : tab === "Profiles" ? (
                <>
                  <section className="panel">
                    <h2>Create acquisition profile</h2>
                    <form
                      onSubmit={async (e) => {
                        e.preventDefault();
                        const target = e.currentTarget;
                        const fields = new FormData(target);
                        try {
                          const saved = await act("profiles", "POST", {
                            name: fields.get("name"),
                            settings: JSON.parse(
                              String(fields.get("settings")),
                            ),
                          });
                          if (saved) {
                            target.reset();
                            notify("Profile created");
                          }
                        } catch {
                          setError("Invalid profile JSON");
                        }
                      }}
                    >
                      <label>
                        New profile name
                        <input name="name" required maxLength={100} />
                      </label>
                      <label>
                        New profile rules
                        <textarea
                          name="settings"
                          rows={6}
                          defaultValue={JSON.stringify(
                            {
                              minimumDuration: 0,
                              allowedFormats: [
                                ".m4a",
                                ".mp3",
                                ".aac",
                                ".opus",
                                ".flac",
                                ".wav",
                              ],
                              sourcePriorities: [
                                "soundgasm",
                                "youtube",
                                "reddit",
                              ],
                              directRetries: 3,
                              backlogLimit: 3,
                              fallback: true,
                            },
                            null,
                            2,
                          )}
                        />
                      </label>
                      <button className="primary">Create profile</button>
                    </form>
                  </section>
                  {Array.isArray(data) &&
                    data.map((p: Row) => (
                      <section className="panel" key={p.id}>
                        <h2>{p.name}</h2>
                        <form
                          onSubmit={(e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            try {
                              act("profiles/" + p.id, "PUT", {
                                name: f.get("name"),
                                settings: JSON.parse(String(f.get("settings"))),
                              });
                              notify("Profile saved");
                            } catch {
                              setError("Invalid profile JSON");
                            }
                          }}
                        >
                          <label>
                            Profile name
                            <input name="name" defaultValue={p.name} />
                          </label>
                          <label>
                            Acquisition rules
                            <textarea
                              name="settings"
                              rows={14}
                              defaultValue={JSON.stringify(
                                JSON.parse(p.settings),
                                null,
                                2,
                              )}
                            />
                          </label>
                          <button className="primary">Save profile</button>
                        </form>
                      </section>
                    ))}
                </>
              ) : tab === "Video" ? (
                <>
                  <section className="panel">
                    <h2>Video storage & transfers</h2>
                    <p className="muted">
                      Use a dedicated Plex Other Videos library. Video transfers
                      default to one at a time and pause below the free-space
                      threshold.
                    </p>
                    {Array.isArray(data) &&
                      data
                        .filter((s: Row) => s.key.startsWith("video."))
                        .map((s: Row) => (
                          <form
                            className="setting-form"
                            key={s.key}
                            onSubmit={(e) => {
                              e.preventDefault();
                              const f = new FormData(e.currentTarget);
                              act("settings/" + s.key, "PUT", {
                                value: f.get("value"),
                              });
                              notify("Video setting saved");
                            }}
                          >
                            <label>
                              {s.key}
                              <input name="value" defaultValue={s.value} />
                            </label>
                            <button>Save</button>
                          </form>
                        ))}
                    <form
                      className="setting-form"
                      onSubmit={async (e) => {
                        e.preventDefault();
                        const f = new FormData(e.currentTarget);
                        const saved = await act("video/plex-binding", "PUT", {
                          sectionId: Number(f.get("sectionId")),
                        });
                        if (saved) {
                          setPlexVideoBinding(saved);
                          notify("Plex video library binding saved");
                        }
                      }}
                    >
                      <label>
                        Plex Other Videos section ID
                        <input
                          key={String(plexVideoBinding.sectionId ?? "unset")}
                          name="sectionId"
                          type="number"
                          min={1}
                          required
                          defaultValue={plexVideoBinding.sectionId ?? ""}
                        />
                      </label>
                      <button>Save Plex video binding</button>
                    </form>
                    <p className="muted">
                      Create the dedicated library in Plex first. ASMarr stores
                      only its section ID here and never creates Plex libraries.
                    </p>
                    <button onClick={() => command("plex-video-verify")}>
                      <Radio size={15} /> Validate dedicated Plex library
                    </button>
                  </section>
                  <section className="panel">
                    <h2>Video quality profiles</h2>
                    {videoProfiles.map((p) => (
                      <form
                        className="setting-form"
                        key={p.id}
                        onSubmit={(e) => {
                          e.preventDefault();
                          const f = new FormData(e.currentTarget);
                          act(`video/profiles/${p.id}`, "PUT", {
                            name: f.get("name"),
                            resolution: f.get("resolution"),
                            settings: JSON.parse(p.settings || "{}"),
                          });
                          notify("Video profile saved");
                        }}
                      >
                        <label>
                          Name
                          <input name="name" defaultValue={p.name} />
                        </label>
                        <label>
                          Resolution
                          <select name="resolution" defaultValue={p.resolution}>
                            {[
                              "Any",
                              "2160p",
                              "1440p",
                              "1080p",
                              "720p",
                              "480p",
                            ].map((r) => (
                              <option key={r}>{r}</option>
                            ))}
                          </select>
                        </label>
                        <button>Save</button>
                      </form>
                    ))}
                  </section>
                </>
              ) : tab === "General" ? (
                <section className="panel">
                  <h2>Library & naming</h2>
                  {Array.isArray(data) &&
                    data
                      .filter((s: Row) => ["root", "naming"].includes(s.key))
                      .map((s: Row) => (
                        <form
                          className="setting-form"
                          key={s.key}
                          onSubmit={(e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            act("settings/" + s.key, "PUT", {
                              value: f.get("value"),
                            });
                            notify("Setting saved");
                          }}
                        >
                          <label>
                            {s.key === "root"
                              ? "Root folder"
                              : "Naming template"}
                            <input name="value" defaultValue={s.value} />
                          </label>
                          <button>Save</button>
                        </form>
                      ))}
                  <p className="muted">
                    Available tokens: {"{Creator}, {Title}, {SourceId}, {ext}"}.
                    Existing files are preserved during migration.
                  </p>
                  <button onClick={() => command("rename-preview")}>
                    Preview naming
                  </button>
                  <div className="divider" />
                  <h2>Manual import</h2>
                  <form
                    className="inline-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const f = new FormData(e.currentTarget);
                      command("manual-import", {
                        path: f.get("path"),
                        key: f.get("key"),
                      });
                    }}
                  >
                    <input
                      name="path"
                      placeholder="Completed download path"
                      required
                    />
                    <input
                      name="key"
                      placeholder="Recording canonical key"
                      required
                    />
                    <button disabled={status.mode === "shadow"}>Import</button>
                  </form>
                </section>
              ) : tab === "Integrations" ? (
                <section className="panel">
                  <h2>Connected services</h2>
                  {data &&
                    Object.entries(data).map(([k, v]: any) => (
                      <div className="source-row" key={k}>
                        <Server size={17} />
                        <b>{k}</b>
                        <span>{v.url || "Configured"}</span>
                        <button
                          onClick={() =>
                            command("integration-test", { kind: k })
                          }
                        >
                          Test
                        </button>
                      </div>
                    ))}
                  <div className="divider" />
                  <h2>Configure integration</h2>
                  <p className="muted">
                    Secrets are stored in protected configuration and are never
                    returned to the browser.
                  </p>
                  <select
                    value={integration}
                    onChange={(e) => {
                      setIntegration(e.target.value);
                      setForm("");
                    }}
                  >
                    <option>prowlarr</option>
                    <option>qbittorrent</option>
                    <option>plex</option>
                    <option>notifications</option>
                  </select>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      try {
                        act(
                          "integrations/" + integration,
                          "PUT",
                          JSON.parse(form),
                        );
                        setForm("");
                        notify("Integration configuration saved");
                      } catch {
                        setError("Enter valid configuration JSON");
                      }
                    }}
                  >
                    <label>
                      Configuration
                      <textarea
                        rows={8}
                        placeholder={
                          integration === "prowlarr"
                            ? '{"url":"http://localhost:9696","apiKey":"…","confidenceThreshold":0.92}'
                            : integration === "qbittorrent"
                              ? '{"url":"http://localhost:8080","username":"…","password":"…","retention":"remove-torrent"}'
                              : integration === "plex"
                                ? '{"url":"http://plex:32400","section_id":4,"video":{"section_id":9},"token":"…"}'
                                : '{"webhook":"https://…","discord":"https://discord.com/api/webhooks/…"}'
                        }
                        value={form}
                        onChange={(e) => setForm(e.target.value)}
                        required
                      />
                    </label>
                    <button className="primary">Save configuration</button>
                  </form>
                </section>
              ) : (
                <section className="panel">
                  <h2>Administrator password</h2>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      const f = new FormData(e.currentTarget);
                      act("auth/password", "POST", {
                        currentPassword: f.get("current"),
                        password: f.get("password"),
                      }).then((r) => {
                        if (r) notify("Administrator password updated");
                      });
                    }}
                  >
                    <label>
                      Current password
                      <input
                        name="current"
                        type="password"
                        autoComplete="current-password"
                        required
                      />
                    </label>
                    <label>
                      New password
                      <input
                        name="password"
                        type="password"
                        autoComplete="new-password"
                        minLength={12}
                        required
                      />
                    </label>
                    <button className="primary">Update password</button>
                  </form>
                  <p className="muted">
                    API clients authenticate through the X-Api-Key header. The
                    key is stored in protected server configuration.
                  </p>
                </section>
              )}
            </>
          ) : page === "System" ? (
            <>
              <div className="tabs">
                {[
                  "Status",
                  "Tasks",
                  "Logs",
                  "Health",
                  "Backups",
                  "Shadow cycles",
                  "Updates",
                ].map((t) => (
                  <button
                    className={tab === t ? "active" : ""}
                    onClick={() => setTab(t)}
                    key={t}
                  >
                    {t}
                  </button>
                ))}
              </div>
              <section className="panel">
                {tab === "Status" ? (
                  <>
                    <h2>System status</h2>
                    <div className="source-row">
                      <b>Version</b>
                      <span>{data?.version}</span>
                    </div>
                    <div className="source-row">
                      <b>Runtime</b>
                      <span>{data?.runtime}</span>
                    </div>
                    <div className="source-row">
                      <b>Execution mode</b>
                      <Badge value={data?.mode || "unknown"} />
                    </div>
                    <div className="source-row">
                      <b>Scheduler</b>
                      <span>{data?.scheduler}</span>
                    </div>
                    <div className="source-row">
                      <b>Audio root</b>
                      <code>{data?.root}</code>
                    </div>
                    <div className="source-row">
                      <b>Video root</b>
                      <code>{data?.videoRoot}</code>
                    </div>
                    <div className="source-row">
                      <b>Audio free space</b>
                      <span>
                        {((data?.disk?.available || 0) / 1073741824).toFixed(1)}{" "}
                        GiB
                      </span>
                    </div>
                    <div className="source-row">
                      <b>Qualified shadow days</b>
                      <span>{data?.qualifiedShadowDays || 0} / 3</span>
                    </div>
                  </>
                ) : tab === "Updates" ? (
                  <>
                    <h2>Application updates</h2>
                    <div className="source-row">
                      <b>Installed version</b>
                      <span>{data?.version}</span>
                    </div>
                    <div className="source-row">
                      <b>Release channel</b>
                      <span>{data?.channel}</span>
                    </div>
                    <div className="source-row">
                      <b>Automatic updates</b>
                      <span>{data?.automatic ? "Enabled" : "Disabled"}</span>
                    </div>
                    <p className="muted">{data?.message}</p>
                    <p>
                      Deployments require a verified build, a rollback backup,
                      and the rollout acceptance gates. This page does not
                      install or restart the application.
                    </p>
                  </>
                ) : tab === "Tasks" ? (
                  <>
                    <div className="section-heading">
                      <h2>Scheduled tasks</h2>
                      <span className="muted">
                        Database locks prevent overlapping runs
                      </span>
                    </div>
                    <table>
                      <thead>
                        <tr>
                          <th>Task</th>
                          <th>Interval</th>
                          <th>Next run</th>
                          <th>Last run</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {Array.isArray(data) &&
                          data.map((t: Row) => (
                            <tr key={t.name}>
                              <td>{t.name}</td>
                              <td>
                                {t.interval_seconds >= 86400
                                  ? "Daily"
                                  : `${Math.round(t.interval_seconds / 60)} min`}
                              </td>
                              <td>{when(t.next_run)}</td>
                              <td>{when(t.last_run)}</td>
                              <td>
                                <button onClick={() => command(t.name)}>
                                  <Play size={14} /> Run
                                </button>
                              </td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                    <h2>Recent commands</h2>
                    <CommandList tick={tick} />
                  </>
                ) : tab === "Logs" ? (
                  <>
                    <h2>Application log</h2>
                    <div className="log-list">
                      {Array.isArray(data) &&
                        data.map((r: Row) => (
                          <div key={r.id}>
                            <span>{when(r.at)}</span>
                            <Badge value={r.level} />
                            <code>{r.message}</code>
                          </div>
                        ))}
                    </div>
                  </>
                ) : tab === "Backups" ? (
                  <>
                    <div className="section-heading">
                      <h2>Database backups</h2>
                      <button
                        className="primary"
                        onClick={() => command("backup")}
                      >
                        Create backup
                      </button>
                    </div>
                    {Array.isArray(data) &&
                      data.map((b: Row) => (
                        <div className="source-row" key={b.name}>
                          <Shield size={17} />
                          <b>{b.name}</b>
                          <span>{(b.size / 1048576).toFixed(1)} MB</span>
                          <span>{when(b.created)}</span>
                        </div>
                      ))}
                  </>
                ) : tab === "Shadow cycles" ? (
                  <>
                    <h2>Daily shadow comparisons</h2>
                    <p className="muted">
                      A clean cycle requires source health, discovery and
                      eligibility parity, playlist agreement, and unchanged
                      production media and checkpoints.
                    </p>
                    {Array.isArray(data) && data.length ? (
                      data.map((c: Row) => (
                        <div className="cycle" key={c.id}>
                          <div className="section-heading">
                            <b>
                              Cycle {c.id} · {when(c.started)}
                            </b>
                            <Badge
                              value={
                                c.clean ? "complete" : "comparison pending"
                              }
                            />
                          </div>
                          <details>
                            <summary>Discovery report</summary>
                            <pre>
                              {JSON.stringify(JSON.parse(c.result), null, 2)}
                            </pre>
                          </details>
                          <details>
                            <summary>Comparison evidence</summary>
                            <pre>
                              {JSON.stringify(
                                JSON.parse(c.comparison),
                                null,
                                2,
                              )}
                            </pre>
                          </details>
                        </div>
                      ))
                    ) : (
                      <Empty text="The first discovery cycle has not finished yet." />
                    )}
                  </>
                ) : (
                  <>
                    <h2>System health</h2>
                    <pre>{JSON.stringify(data, null, 2)}</pre>
                  </>
                )}
              </section>
            </>
          ) : null}
        </main>
        <footer>
          ASMarr <span>·</span> Built for unhurried listening{" "}
          <span className="footer-right">v0.2.0</span>
        </footer>
      </div>
      {toast && (
        <div className="toast">
          <Check size={17} />
          {toast}
        </div>
      )}
    </div>
  );
}
function CommandList({ tick }: { tick: number }) {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => {
    api("commands")
      .then(setRows)
      .catch(() => {});
  }, [tick]);
  return (
    <div>
      {rows.slice(0, 15).map((c) => (
        <div className="command-row" key={c.id}>
          <div>
            <b>{c.name}</b>
            <small>{when(c.created)}</small>
          </div>
          <Badge value={c.state} />
          {(c.result || c.error) && (
            <details>
              <summary>Result</summary>
              <pre>
                {c.error || JSON.stringify(JSON.parse(c.result), null, 2)}
              </pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
