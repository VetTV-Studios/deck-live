// Shared chat panel: connect status, the last 20 messages, and a poll.
import {
  ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, TextField,
} from "@decky/ui";
import { addEventListener, callable, removeEventListener } from "@decky/api";
import { useEffect, useState } from "react";

type ChatMsg = { id: string; platform: string; user_name: string; text: string; ts: number };
type PollSnap = { open: boolean; voters: number; options: { label: string; count: number }[] } | null;
type ChatState = {
  state: string; platform: string; channel: string; detail: string;
  messages: ChatMsg[]; poll: PollSnap; overlay_url: string;
  has_yt_key: boolean; has_oauth: boolean; has_login: boolean; quota_used: number;
};
type ChatEvent = {
  type: string; state?: string; platform?: string; channel?: string; detail?: string;
  message?: ChatMsg; poll?: PollSnap;
};

const chatGetState = callable<[], ChatState>("chat_get_state");
const chatConnect = callable<[string, string], { ok: boolean; error?: string }>("chat_connect");
const chatDisconnect = callable<[], boolean>("chat_disconnect");
const chatSetYtKey = callable<[string], boolean>("chat_set_yt_key");
const chatSetOauth = callable<[string, string], boolean>("chat_set_oauth");
const chatStartPoll = callable<[string], PollSnap>("chat_start_poll");
const chatStopPoll = callable<[], PollSnap>("chat_stop_poll");
const chatSend = callable<[string], { ok: boolean; error?: string }>("chat_send");

function applyChat(prev: ChatState | null, ev: ChatEvent): ChatState | null {
  if (!prev) return prev;
  if (ev.type === "status") {
    return {
      ...prev,
      state: ev.state || prev.state,
      detail: ev.detail ?? "",
      platform: ev.platform || prev.platform,
      channel: ev.channel || prev.channel,
    };
  }
  if (ev.type === "message" && ev.message) {
    return { ...prev, messages: [...prev.messages, ev.message].slice(-20) };
  }
  if (ev.type === "poll") return { ...prev, poll: ev.poll ?? null };
  return prev;
}

const statusLabel = (state: string) =>
  state === "live" ? "Connected"
    : state === "connecting" ? "Connecting…"
    : state === "quota" ? "Quota pause"
    : state === "error" ? "Error"
    : "Offline";

export function ChatSection() {
  const [st, setSt] = useState<ChatState | null>(null);
  const [platform, setPlatform] = useState("twitch");
  const [channel, setChannel] = useState("");
  const [busy, setBusy] = useState(false);
  const [ytKey, setYtKey] = useState("");
  const [oauth, setOauth] = useState("");
  const [login, setLogin] = useState("");
  const [draft, setDraft] = useState("");

  const refresh = async () => {
    const s = await chatGetState();
    setSt(s);
    return s;
  };

  useEffect(() => {
    let alive = true;
    chatGetState().then((s) => {
      if (!alive) return;
      setSt(s);
      setPlatform(s.platform || "twitch");
      setChannel(s.channel || "");
    });
    const h = addEventListener<[ChatEvent]>("chat_event", (ev) => setSt((prev) => applyChat(prev, ev)));
    return () => { alive = false; removeEventListener("chat_event", h); };
  }, []);

  if (!st) return <PanelSection title="Chat"><PanelSectionRow>Loading…</PanelSectionRow></PanelSection>;
  const connected = st.state === "live" || st.state === "connecting";

  const toggle = async () => {
    setBusy(true);
    try {
      if (connected) await chatDisconnect();
      else {
        const r = await chatConnect(platform, channel.trim());
        if (!r.ok && r.error) setSt((prev) => (prev ? { ...prev, state: "error", detail: r.error || "" } : prev));
      }
      await refresh();
    } finally { setBusy(false); }
  };

  const togglePoll = async () => {
    setBusy(true);
    try {
      const poll = st.poll?.open ? await chatStopPoll() : await chatStartPoll("");
      setSt((prev) => (prev ? { ...prev, poll } : prev));
    } finally { setBusy(false); }
  };

  return (
    <PanelSection title="Chat">
      <PanelSectionRow>
        <DropdownItem label="Platform" rgOptions={[
          { data: "twitch", label: "Twitch" }, { data: "youtube", label: "YouTube" }]}
          selectedOption={platform} disabled={connected}
          onChange={(o) => setPlatform(String(o.data))} />
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label={platform === "youtube" ? "Video or live chat id" : "Channel"}
          value={channel} disabled={connected} onChange={(e) => setChannel(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || (!connected && !channel.trim())} onClick={toggle}>
          {busy ? "…" : connected ? "Disconnect" : "Connect"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label={statusLabel(st.state)}>
          {st.detail || (st.state === "live" ? st.channel : "")}
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: 12, lineHeight: 1.35, maxHeight: 220, overflow: "auto", width: "100%" }}>
          {st.messages.length === 0 && <div style={{ color: "#888" }}>No messages yet</div>}
          {st.messages.map((m) => (
            <div key={m.id}><b>{m.user_name}</b> {m.text}</div>
          ))}
        </div>
      </PanelSectionRow>
      {st.poll && (
        <PanelSectionRow>
          <div style={{ fontSize: 12 }}>
            {st.poll.options.map((o) => (
              <div key={o.label}>{o.label}: {o.count}</div>
            ))}
            <div>{st.poll.voters} voter{st.poll.voters === 1 ? "" : "s"}{st.poll.open ? "" : " · closed"}</div>
          </div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={togglePoll}>
          {st.poll?.open ? "Stop poll" : "Start poll"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="OBS browser source" description="127.0.0.1 only">
          {st.overlay_url || "—"}
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label={st.has_yt_key ? "YouTube API key (saved)" : "YouTube API key"}
          bIsPassword value={ytKey} onChange={(e) => setYtKey(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || !ytKey.trim()} onClick={async () => {
          await chatSetYtKey(ytKey); setYtKey(""); await refresh();
        }}>Save YouTube key</ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label={st.has_login ? "Twitch login (saved)" : "Twitch login"}
          value={login} onChange={(e) => setLogin(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label={st.has_oauth ? "OAuth token (saved)" : "OAuth token"}
          bIsPassword value={oauth} onChange={(e) => setOauth(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || (!oauth.trim() && !login.trim())} onClick={async () => {
          await chatSetOauth(oauth, login); setOauth(""); await refresh();
        }}>Save OAuth</ButtonItem>
      </PanelSectionRow>
      {st.has_oauth && (
        <>
          <PanelSectionRow>
            <TextField label="Send" value={draft} onChange={(e) => setDraft(e.target.value)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy || !draft.trim()} onClick={async () => {
              const r = await chatSend(draft.trim());
              if (!r.ok && r.error) setSt((prev) => (prev ? { ...prev, detail: r.error || "" } : prev));
              else setDraft("");
            }}>Send</ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
}
