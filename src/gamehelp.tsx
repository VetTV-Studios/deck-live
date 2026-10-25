// Game Help add-on: YouTube help videos for the running game (spec 04).
// Uses undocumented Steam/Decky internals defensively: every access is guarded.
import {
  ButtonItem, DropdownItem, PanelSection, PanelSectionRow, TextField, ToggleField, Field,
  Router, Navigation,
} from "@decky/ui";
import { callable } from "@decky/api";
import { useEffect, useState } from "react";
import { resolveSearchGame, type RunningGame } from "./searchGame";

type Item = { id: string; title: string; channel: string; duration?: number | null; views?: number | null; pinned?: boolean };
type Result = { query?: string; provider: string | null; items: Item[]; cached?: boolean; offline?: boolean;
  note?: string; error?: string; quota_used?: number; quota_limit?: number; ytdlp?: boolean };
type GhSettings = { enabled: boolean; has_key: boolean; pin_channel: string; quota_used: number;
  quota_limit: number; ytdlp: boolean; topics: string[] };
type Ach = { id: string; name: string; pct?: number };

const ghGetSettings = callable<[], GhSettings>("gh_get_settings");
const ghSetSettings = callable<[boolean, string], boolean>("gh_set_settings");
const ghSetApiKey = callable<[string], boolean>("gh_set_api_key");
export const ghSearch = callable<[string, string, string, boolean], Result>("gh_search");
const ghInstallYtdlp = callable<[], { ok: boolean; error?: string }>("gh_install_ytdlp");

const TOPIC_LABELS: Record<string, string> = {
  walkthrough: "Walkthrough", boss: "Boss guide", achievements: "Achievements",
  settings: "Best settings (handheld)", next: "What to do next",
};

export type { RunningGame };

/** Current foreground game, or null. Router.MainRunningApp is typed in @decky/ui but undocumented. */
export function getRunningGame(): RunningGame | null {
  try {
    const app: any = (Router as any)?.MainRunningApp;
    if (app && app.appid && app.display_name) return { appid: String(app.appid), name: String(app.display_name) };
  } catch { /* fall through */ }
  try {
    const apps: any[] = (Router as any)?.RunningApps ?? [];
    const a = apps.find((x) => x?.display_name);
    if (a) return { appid: String(a.appid), name: String(a.display_name) };
  } catch { /* ignore */ }
  return null;
}

/** Locked achievements via SteamClient.Apps.GetMyAchievementsForApp (undocumented). Non-Steam: []. */
async function getLockedAchievements(appid: string): Promise<Ach[]> {
  try {
    const sc: any = (window as any).SteamClient;
    const fn = sc?.Apps?.GetMyAchievementsForApp;
    if (!fn) return [];
    const res: any = await fn.call(sc.Apps, String(appid));
    const list: any[] = res?.data?.rgAchievements ?? [];
    return list.filter((a) => a && !a.bAchieved && a.strName)
      .map((a) => ({ id: String(a.strID), name: String(a.strName),
        pct: typeof a.flAchieved === "number" ? a.flAchieved : undefined }))
      .sort((a, b) => (b.pct ?? 0) - (a.pct ?? 0));
  } catch {
    return [];
  }
}

function openVideo(id: string) {
  const url = `https://www.youtube.com/watch?v=${id}`;
  try { Navigation.CloseSideMenus?.(); } catch { /* ignore */ }
  try { Navigation.NavigateToExternalWeb(url); return; } catch { /* fall through */ }
  try { (Router as any).NavigateToExternalWeb(url); return; } catch { /* fall through */ }
  try { (window as any).SteamClient?.URL?.ExecuteSteamURL?.(`steam://openurl/${url}`); } catch { /* ignore */ }
}

const fmtDur = (s?: number | null) => {
  if (!s) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
};
const fmtViews = (v?: number | null) =>
  v == null ? "" : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M views` : v >= 1e3 ? `${Math.round(v / 1e3)}K views` : `${v} views`;

export function GameHelpSection() {
  const [game, setGame] = useState<RunningGame | null>(getRunningGame());
  const [cfg, setCfg] = useState<GhSettings | null>(null);
  const [topic, setTopic] = useState("walkthrough");
  const [custom, setCustom] = useState("");
  const [achs, setAchs] = useState<Ach[]>([]);
  const [ach, setAch] = useState<string>("");
  const [res, setRes] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [pinInput, setPinInput] = useState("");

  const loadCfg = async () => {
    try {
      const c = await ghGetSettings();
      setCfg(c);
      setPinInput(c.pin_channel);
    } catch {
      setCfg({ enabled: false, has_key: false, pin_channel: "", quota_used: 0, quota_limit: 0, ytdlp: false, topics: [] });
    }
  };

  const run = async (t: string, extra = "", refresh = false, detected: RunningGame | null = null) => {
    const target = resolveSearchGame(detected, game);
    if (!target) return;
    setBusy(true);
    try { setRes(await ghSearch(target.name, t, extra, refresh)); } finally { setBusy(false); loadCfg(); }
  };

  useEffect(() => {
    loadCfg();
    const detected = getRunningGame();
    setGame(detected);
    if (detected) {
      run("walkthrough", "", false, detected);
      getLockedAchievements(detected.appid).then(setAchs);
    }
  }, []);

  if (cfg && !cfg.enabled) {
    return (
      <PanelSection title="Game Help">
        <PanelSectionRow>
          <ToggleField label="Enable Game Help" checked={false}
            onChange={async (v) => { await ghSetSettings(v, cfg.pin_channel); loadCfg(); }} />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const mode = res?.provider === "api" ? "YouTube API" : res?.provider === "yt-dlp" ? "No-key mode (yt-dlp)" : res?.provider ?? "";
  return (
    <PanelSection title="Game Help">
      {!game ? (
        <PanelSectionRow><div style={{ fontSize: 12 }}>Start a game to see help videos for it.</div></PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <Field label={game.name} description={
              [mode, res?.cached ? "cached" : "", res?.offline ? "OFFLINE" : "",
               cfg?.has_key ? `${cfg.quota_used}/${cfg.quota_limit} searches today` : ""].filter(Boolean).join(" · ")} />
          </PanelSectionRow>
          <PanelSectionRow>
            <DropdownItem label="Topic" rgOptions={Object.keys(TOPIC_LABELS).map((k) => ({ data: k, label: TOPIC_LABELS[k] }))}
              selectedOption={topic} onChange={(o) => { setTopic(o.data); setAch(""); run(o.data); }} />
          </PanelSectionRow>
          {achs.length > 0 && (
            <PanelSectionRow>
              <DropdownItem label={`Locked achievements (${achs.length})`}
                rgOptions={[{ data: "", label: "Pick an achievement…" },
                  ...achs.slice(0, 100).map((a) => ({ data: a.name, label: a.pct != null ? `${a.name} (${a.pct.toFixed(1)}%)` : a.name }))]}
                selectedOption={ach} onChange={(o) => { setAch(o.data); if (o.data) run("achievement", o.data); }} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <TextField label="Search" value={custom} onChange={(e) => setCustom(e.target.value)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy || !custom.trim()} onClick={() => run("custom", custom.trim())}>
              Search “{game.name} {custom.trim()}”
            </ButtonItem>
          </PanelSectionRow>
          {busy && <PanelSectionRow><div style={{ fontSize: 12 }}>Searching…</div></PanelSectionRow>}
          {res?.error && <PanelSectionRow><div style={{ color: "#f66", fontSize: 11 }}>{res.error}</div></PanelSectionRow>}
          {res?.note && !res.error && <PanelSectionRow><div style={{ color: "#fa0", fontSize: 11 }}>{res.note}</div></PanelSectionRow>}
          {(res?.items ?? []).map((v) => (
            <PanelSectionRow key={v.id}>
              <ButtonItem layout="below" onClick={() => openVideo(v.id)}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", textAlign: "left" }}>
                  <img src={`https://i.ytimg.com/vi/${v.id}/mqdefault.jpg`} style={{ width: 96, height: 54, objectFit: "cover", flexShrink: 0 }} />
                  <div style={{ fontSize: 11, lineHeight: 1.25, overflow: "hidden" }}>
                    <div style={{ fontWeight: "bold" }}>{v.pinned ? "[Pinned] " : ""}{v.title}</div>
                    <div style={{ opacity: 0.7 }}>{[v.channel, fmtDur(v.duration), fmtViews(v.views)].filter(Boolean).join(" · ")}</div>
                  </div>
                </div>
              </ButtonItem>
            </PanelSectionRow>
          ))}
          {res && res.items.length > 0 && (
            <PanelSectionRow><div style={{ fontSize: 10, opacity: 0.6 }}>Results from YouTube. Opens youtube.com in the Steam browser.</div></PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(ach ? "achievement" : topic, ach, true)}>Refresh</ButtonItem>
          </PanelSectionRow>
        </>
      )}
      <PanelSectionRow>
        <ToggleField label="Game Help settings" checked={showSettings} onChange={setShowSettings} />
      </PanelSectionRow>
      {showSettings && cfg && (
        <>
          <PanelSectionRow>
            <ToggleField label="Enable Game Help" checked={cfg.enabled}
              onChange={async (v) => { await ghSetSettings(v, cfg.pin_channel); loadCfg(); }} />
          </PanelSectionRow>
          <PanelSectionRow>
            <TextField label={cfg.has_key ? "YouTube API key (saved)" : "YouTube API key (optional)"} bIsPassword
              value={keyInput} onChange={(e) => setKeyInput(e.target.value)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={async () => { await ghSetApiKey(keyInput); setKeyInput(""); loadCfg(); }}>
              {keyInput ? "Save API key" : "Clear API key"}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <TextField label="Pinned channel ID (UC…), e.g. VetTV" value={pinInput} onChange={(e) => setPinInput(e.target.value)}
              onBlur={() => ghSetSettings(cfg.enabled, pinInput).then(loadCfg)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="No-key fallback" description={cfg.ytdlp ? "yt-dlp found" : "yt-dlp not installed"} />
          </PanelSectionRow>
          {!cfg.ytdlp && (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={async () => { const r = await ghInstallYtdlp(); if (!r.ok) setRes({ provider: null, items: [], error: r.error }); loadCfg(); }}>
                Download pinned yt-dlp (checksum verified)
              </ButtonItem>
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
  );
}

/** Pre-fetch the default topic when a game starts (one cached search, after the game settles). */
export function registerGameHelpPrefetch(): () => void {
  try {
    const sc: any = (window as any).SteamClient;
    const reg = sc?.GameSessions?.RegisterForAppLifetimeNotifications;
    if (!reg) return () => {};
    const h = reg.call(sc.GameSessions, (n: any) => {
      if (!n?.bRunning) return;
      setTimeout(() => {
        const g = getRunningGame();
        if (g && String(g.appid) === String(n.unAppID)) {
          ghGetSettings().then((c) => { if (c.enabled) ghSearch(g.name, "walkthrough", "", false); }).catch(() => {});
        }
      }, 20000);
    });
    return () => { try { h?.unregister?.(); } catch { /* ignore */ } };
  } catch {
    return () => {};
  }
}
