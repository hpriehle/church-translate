import { useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import type { Language } from './api';
import { WS_SERVER } from './config';

export type Caption = { seq: number; text: string; credit?: string };

export type ListenState = {
  conn: 'connecting' | 'ok' | 'reconnecting';
  live: boolean;
  langOn: boolean;
  speaker: string;
  passthrough: boolean;
  languages: Language[];
  captions: Caption[];
  congregationName: string | null;
  nextMeeting: { label: string } | null;
  lang: string; // what the server says we're on (it may pick one when we asked for none)
};

const KEEP = 6;

/** Captions and meeting state for one congregation and language, over /c/<slug>/ws/listen. */
export function useListen(slug: string, lang: string): ListenState {
  const [state, setState] = useState<ListenState>({
    conn: 'connecting',
    live: false,
    langOn: true,
    speaker: 'en',
    passthrough: false,
    languages: [],
    captions: [],
    congregationName: null,
    nextMeeting: null,
    lang,
  });
  const ref = useRef({ boot: null as string | null, session: null as string | null, lastSeq: -1 });

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let delay = 1000;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let ping: ReturnType<typeof setInterval> | undefined;
    const r = ref.current;
    r.lastSeq = -1;
    r.session = null;
    setState((s) => ({ ...s, captions: [], lang }));

    const update = (f: (s: ListenState) => ListenState) => {
      if (!closed) setState(f);
    };
    const clear = () => {
      r.lastSeq = -1;
      update((s) => ({ ...s, captions: [] }));
    };
    const add = (m: any) => {
      if (typeof m?.seq !== 'number' || m.seq <= r.lastSeq) return;
      r.lastSeq = m.seq;
      const c: Caption = { seq: m.seq, text: m.text || m.fr || '', credit: m.credit?.notice };
      update((s) => ({ ...s, captions: [...s.captions, c].slice(-KEEP) }));
    };

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(`${WS_SERVER}/c/${encodeURIComponent(slug)}/ws/listen`);
      ws.onopen = () => {
        delay = 1000;
        ws?.send(JSON.stringify({ type: 'hello', last_seq: r.lastSeq, boot: r.boot, session: r.session, lang, mode: 'stream' }));
        update((s) => ({ ...s, conn: 'ok' }));
      };
      ws.onmessage = (ev) => {
        let m: any;
        try {
          m = JSON.parse(String(ev.data));
        } catch {
          return;
        }
        switch (m.type) {
          case 'init':
            if (m.boot !== r.boot || m.session !== r.session) clear();
            r.boot = m.boot;
            r.session = m.session;
            update((s) => ({
              ...s,
              lang: m.lang || s.lang,
              congregationName: m.congregation?.name ?? s.congregationName,
              nextMeeting: m.next_meeting ?? null,
              langOn: m.on !== false,
              speaker: m.speaker || 'en',
              passthrough: !!m.passthrough,
              languages: m.languages || [],
              live: !!m.live,
            }));
            (m.history || []).forEach(add);
            break;
          case 'reload':
            reconnect(0);
            break;
          case 'session':
            if (m.session !== r.session) clear();
            r.session = m.session;
            update((s) => ({ ...s, live: true }));
            break;
          case 'caption':
            if (m.session && r.session && m.session !== r.session) clear();
            if (m.session) r.session = m.session;
            add(m);
            break;
          case 'languages':
            update((s) => ({ ...s, languages: m.languages || [] }));
            break;
          case 'lang_state':
            update((s) => ({ ...s, langOn: !!m.on }));
            break;
          case 'speaker':
            update((s) => ({ ...s, speaker: m.speaker || 'en', passthrough: !!m.passthrough }));
            break;
          case 'status':
            update((s) => ({ ...s, live: !!m.live }));
            if (!m.live) {
              r.session = null;
              reconnect(2500); // pick up the next session when it starts
            }
            break;
        }
      };
      ws.onclose = () => {
        if (closed) return;
        update((s) => ({ ...s, conn: 'reconnecting' }));
        retry = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 8000);
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {}
      };
    };

    const reconnect = (after: number) => {
      if (ws) {
        ws.onclose = null;
        try {
          ws.close();
        } catch {}
        ws = null;
      }
      clearTimeout(retry);
      retry = setTimeout(connect, after);
    };

    connect();
    ping = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping', t: Date.now() }));
    }, 20000);
    // Coming back from the background: the socket may have died while suspended.
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active' && ws?.readyState !== WebSocket.OPEN) reconnect(0);
    });

    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(ping);
      sub.remove();
      if (ws) {
        ws.onclose = null;
        try {
          ws.close();
        } catch {}
      }
    };
  }, [slug, lang]);

  return state;
}
