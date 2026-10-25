import {
  ButtonItem, DropdownItem, PanelSection, PanelSectionRow, TextField,
  ToggleField, Field, SliderField, staticClasses,
} from "@decky/ui";
import { addEventListener, removeEventListener, callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaBroadcastTower } from "react-icons/fa";
import { GameHelpSection, registerGameHelpPrefetch } from "./gamehelp";

type Platform = { id: string; label: string; needs_url: boolean };
type State = {
  platforms: Platform[]; presets: string[]; bitrates: Record<string, Record<string, number>>; platform: string; preset: string; mic: boolean; mic_gain: number; game_vol: number; av_offset: number;
  has_key: Record<string, boolean>; custom_urls: Record<string, string>;
  live: boolean; started_at: number | null; error: string; reconnecting?: number;
};
type Health = { mode?: string; fps?: number; kbps?: number; dropped?: number; duplicated?: number;
  q_overruns?: number; lag_ms?: number | null; gap_ms?: number };
type Status = { live: boolean; started_at: number | null; error: string; reconnecting?: number };

const getState = callable<[], State>("get_state");
const saveSettings = callable<[string, string, boolean, number, number, number], boolean>("save_settings");
const setStreamKey = callable<[string, string], boolean>("set_stream_key");
const setCustomUrl = callable<[string, string], boolean>("set_custom_url");
const startStream = callable<[], { ok: boolean; error?: string }>("start_stream");
type Diag = { ok: boolean; elements: Record<string, boolean>; nodes: string[]; node: string | null;
  encoder: string | null; file: string | null; size: number; error: string;
  game_source: string | null; mic_source: string | null; game_peak_db: number | null; av_offset?: number; mode?: string | null; seconds?: number;
  frames?: number | null; expected_frames?: number | null; file_duration?: number | null; max_gap_ms?: number | null;
  dropped?: number | null; duplicated?: number | null; src_fps?: number | null; media_duration?: number | null; wall_seconds?: number | null; warnings?: string[] };
const runDiagnostics = callable<[], Diag>("run_diagnostics");
const stopStream = callable<[], boolean>("stop_stream");

const fmt = (s: number) =>
  [Math.floor(s / 3600), Math.floor((s % 3600) / 60), Math.floor(s % 60)]
    .map((n) => String(n).padStart(2, "0")).join(":");

function Content() {
  const [st, setSt] = useState<State | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [urlInput, setUrlInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [diag, setDiag] = useState<Diag | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const [health, setHealth] = useState<Health>({});
  const [now, setNow] = useState(Date.now() / 1000);

  const refresh = async () => {
    const s = await getState();
    setSt(s);
    setUrlInput(s.custom_urls[s.platform] ?? "");
  };

  useEffect(() => {
    refresh();
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    const l = addEventListener<[Status]>("stream_status", (s) =>
      setSt((p) => (p ? { ...p, ...s } : p)));
    const h = addEventListener<[Health]>("stream_health", (x) => setHealth(x));
    return () => { clearInterval(t); removeEventListener("stream_status", l); removeEventListener("stream_health", h); };
  }, []);

  if (!st) return <PanelSection><PanelSectionRow>Loading…</PanelSectionRow></PanelSection>;
  const plat = st.platforms.find((p) => p.id === st.platform);

  const update = async (patch: Partial<State>) => {
    const n = { ...st, ...patch };
    setSt(n);
    await saveSettings(n.platform, n.preset, n.mic, n.mic_gain, n.game_vol, n.av_offset);
    if (patch.platform) { setKeyInput(""); setUrlInput(n.custom_urls[n.platform] ?? ""); }
  };

  const toggleLive = async () => {
    setBusy(true);
    try {
      if (st.live) await stopStream();
      else {
        const r = await startStream();
        if (!r.ok) toaster.toast({ title: "Deck Live", body: r.error ?? "Failed to start" });
      }
      await refresh();
    } finally { setBusy(false); }
  };

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field label={st.live ? (st.reconnecting ? `🟠 Reconnecting (${st.reconnecting})…` : "🔴 LIVE") : "Offline"}>
            {st.live && st.started_at ? fmt(now - st.started_at) : ""}
          </Field>
        </PanelSectionRow>
        {st.live && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: (health.gap_ms ?? 0) > 500 || (health.kbps ?? 1) === 0 ? "#fa0" : "#aaa" }}>
              {health.fps === undefined
                ? (health.mode === "gst-launch" ? "Health: basic (gst-launch mode)" : "Health: waiting…")
                : `Health: ${health.fps} fps · ${health.kbps} kbps · dropped ${(health.dropped ?? 0) + (health.q_overruns ?? 0)}` +
                  ` · dup ${health.duplicated ?? 0}` + ((health.gap_ms ?? 0) > 500 ? ` · STALL ${health.gap_ms} ms` : "")}
            </div>
          </PanelSectionRow>
        )}
        {st.error && <PanelSectionRow><div style={{ color: "#f66", fontSize: 12 }}>{st.error}</div></PanelSectionRow>}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={toggleLive}>
            {busy ? "…" : st.live ? "Stop Stream" : "Go Live"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <GameHelpSection />
      <PanelSection title="Destination">
        <PanelSectionRow>
          <DropdownItem label="Platform" disabled={st.live}
            rgOptions={st.platforms.map((p) => ({ data: p.id, label: p.label }))}
            selectedOption={st.platform} onChange={(o) => update({ platform: o.data })} />
        </PanelSectionRow>
        {plat?.needs_url && (
          <PanelSectionRow>
            <TextField label="Server URL" value={urlInput} disabled={st.live}
              onChange={(e) => setUrlInput(e.target.value)}
              onBlur={() => setCustomUrl(st.platform, urlInput).then(refresh)} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <TextField label={st.has_key[st.platform] ? "Stream key (saved)" : "Stream key"}
            bIsPassword value={keyInput} disabled={st.live}
            onChange={(e) => setKeyInput(e.target.value)} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!keyInput || st.live}
            onClick={async () => { await setStreamKey(st.platform, keyInput); setKeyInput(""); await refresh(); }}>
            Save Key
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Quality">
        <PanelSectionRow>
          <DropdownItem label="Preset" disabled={st.live}
            rgOptions={st.presets.map((p) => ({ data: p, label: p }))}
            selectedOption={st.preset} onChange={(o) => update({ preset: o.data })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Bitrate">
            {`${st.bitrates?.[st.platform]?.[st.preset] ?? "?"} kbps video + 160 audio (CBR)`}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label="Microphone" checked={st.mic} disabled={st.live}
            onChange={(v) => update({ mic: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField label="Game volume" value={st.game_vol} min={0} max={100} step={5}
            showValue disabled={st.live} onChange={(v) => update({ game_vol: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField label={`A/V sync: ${st.av_offset > 0 ? "+" : ""}${st.av_offset} ms`}
            description="− = audio earlier (fix lagging audio), + = audio later"
            value={st.av_offset} min={-500} max={500} step={20}
            disabled={st.live} onChange={(v) => update({ av_offset: v })} />
        </PanelSectionRow>
        {st.mic && (
          <PanelSectionRow>
            <SliderField label="Mic gain (50% = normal)" value={st.mic_gain} min={0} max={100} step={5}
              showValue disabled={st.live} onChange={(v) => update({ mic_gain: v })} />
          </PanelSectionRow>
        )}
      </PanelSection>
      <PanelSection title="Diagnostics">
        <PanelSectionRow>
          <Field label="Plugin version" description="Deck Live 2.0.0" />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={diagBusy || st.live}
            onClick={async () => { setDiagBusy(true); setDiag(null);
              try { setDiag(await runDiagnostics()); } finally { setDiagBusy(false); } }}>
            {diagBusy ? "Testing (20s)…" : "Run Diagnostics (20s test)"}
          </ButtonItem>
        </PanelSectionRow>
        {diag && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, lineHeight: 1.4, wordBreak: "break-all" }}>
              <div style={{ color: diag.ok ? "#6f6" : "#f66", fontWeight: "bold" }}>
                {diag.ok ? "✅ Capture test passed" : "❌ " + (diag.error || "Test failed")}
              </div>
              <div>Capture node: {diag.node ?? "not found"}</div>
              <div>Encoder: {diag.encoder ?? "none"}</div>
              {(diag.warnings ?? []).map((w, i) => <div key={i} style={{ color: "#fa0" }}>Warning: {w}</div>)}
              <div>Frames: {diag.frames ?? "n/a"} / {diag.expected_frames ?? "?"} expected · max gap {diag.max_gap_ms ?? "n/a"} ms</div>
              <div>Capture (gamescope) {diag.src_fps ?? "n/a"} fps unique · encoded/duplicated by videorate {diag.duplicated ?? "n/a"}</div>
              <div>Dropped {diag.dropped ?? "n/a"} · file {diag.file_duration ?? "n/a"} s / wall {diag.wall_seconds ?? "n/a"} s · runner {diag.mode ?? "?"}</div>
              <div>A/V offset: {diag.av_offset ?? 0} ms — check lip-sync in the test file</div>
              <div>Game audio: {diag.game_source ?? "—"}</div>
              <div>Mic: {diag.mic_source ?? "—"}</div>
              <div style={{ color: diag.game_peak_db == null || diag.game_peak_db < -60 ? "#fa0" : undefined }}>
                Game audio peak: {diag.game_peak_db == null ? "no data" : `${diag.game_peak_db} dB`}
                {diag.game_peak_db != null && diag.game_peak_db < -60 ? " (silent? play some game audio)" : ""}
              </div>
              {diag.file && <div>Test file: {diag.file} ({Math.round(diag.size / 1024)} KB)</div>}
              {diag.nodes.length > 0 && <div>Video sources: {diag.nodes.join("; ")}</div>}
              <div>OK: {Object.keys(diag.elements).filter((k) => diag.elements[k]).join(", ") || "—"}</div>
              <div>Missing: {Object.keys(diag.elements).filter((k) => !diag.elements[k]).join(", ") || "—"}</div>
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const unregisterPrefetch = registerGameHelpPrefetch();
  return {
  name: "Deck Live",
  titleView: <div className={staticClasses.Title}>Deck Live</div>,
  content: <Content />,
  icon: <FaBroadcastTower />,
  onDismount() { unregisterPrefetch(); },
  };
});
