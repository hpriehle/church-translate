import { useKeepAwake } from 'expo-keep-awake';
import { router, useLocalSearchParams } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Language } from '../../lib/api';
import { LIVE_EN } from '../../lib/config';
import { fmt, T } from '../../lib/i18n';
import { forgetCongregation, loadLang, saveLang } from '../../lib/storage';
import { streamAudio } from '../../lib/streamAudio';
import { C } from '../../lib/theme';
import { liveEnglishSupported, playLiveEnglish, stopLiveEnglish, useLiveSenders, useLiveStats } from '../../lib/useLiveEnglish';
import { useListen } from '../../lib/useListen';

export default function Listen() {
  const { slug, title } = useLocalSearchParams<{ slug: string; title?: string }>();
  const [lang, setLang] = useState<string | null>(null); // null until the saved choice loads
  const [playing, setPlaying] = useState(false); // what the user asked for
  const [error, setError] = useState<string | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  const senders = useLiveSenders();
  const isLiveEn = lang === LIVE_EN;

  useEffect(() => {
    loadLang().then((l) => setLang(l));
  }, []);

  // Captions follow a real language; while on Live English, keep the socket on the last one for meeting state.
  const captionLang = useRef('');
  if (lang && lang !== LIVE_EN) captionLang.current = lang;
  const st = useListen(slug, lang === null ? '' : captionLang.current);
  const congName = st.congregationName || title || '';

  const languages: Language[] = useMemo(() => {
    const list = [...st.languages];
    if (liveEnglishSupported && senders.length) list.unshift({ code: LIVE_EN, name: T.liveEnglish });
    return list;
  }, [st.languages, senders.length]);

  // The server picks a language when we asked for none; remember it.
  useEffect(() => {
    if (lang === '' && st.lang) setLang(st.lang);
  }, [lang, st.lang]);

  const liveStats = useLiveStats(playing && isLiveEn);
  const langName = (code: string) => languages.find((l) => l.code === code)?.name ?? code;

  const start = async (code = lang) => {
    setError(null);
    if (!code) return;
    try {
      if (code === LIVE_EN) {
        streamAudio.stop();
        await playLiveEnglish(`${T.liveEnglish} · ${congName}`);
      } else {
        stopLiveEnglish();
        await streamAudio.play(slug, code, congName, `${T.appName} · ${langName(code)}`);
      }
      setPlaying(true);
    } catch (e: any) {
      setError(e?.message || T.audioError);
      setPlaying(false);
    }
  };

  const stop = () => {
    streamAudio.stop();
    stopLiveEnglish();
    setPlaying(false);
  };

  const pick = async (code: string) => {
    if (code === lang) return;
    setLang(code);
    await saveLang(code);
    if (playing) await start(code);
  };

  const change = async () => {
    stop();
    await forgetCongregation();
    router.replace('/find');
  };

  useEffect(() => () => stop(), []);

  const waitingText = (() => {
    if (isLiveEn) return null;
    const paused = st.live && st.speaker === lang && !st.passthrough;
    if (paused) return fmt(T.speaker, { name: langName(st.speaker) });
    if (!st.langOn) return T.off;
    if (st.live) return st.captions.length ? null : T.liveWait;
    if (st.captions.length) return T.done;
    return st.nextMeeting?.label ? fmt(T.beginAt, { label: st.nextMeeting.label }) : T.begin;
  })();

  const connText = st.conn === 'ok' ? (st.live ? T.live : T.waiting) : st.conn === 'connecting' ? T.connecting : T.reconnecting;
  const connColor = st.conn !== 'ok' ? C.bad : st.live ? C.ok : C.wait;

  return (
    <SafeAreaView style={s.root} edges={['top', 'left', 'right', 'bottom']}>
      {playing && <KeepAwake />}
      <View style={s.header}>
        <View style={{ flex: 1 }}>
          <Text style={s.title} numberOfLines={1}>{congName}</Text>
          <Text style={[s.pill, { color: connColor }]}>● {connText}</Text>
        </View>
        <Pressable onPress={change} hitSlop={12} accessibilityRole="button">
          <Text style={s.change}>{T.change}</Text>
        </Pressable>
      </View>

      {languages.length > 0 && (
        <View style={s.langs} accessibilityRole="radiogroup" accessibilityLabel={T.language}>
          {languages.map((l) => (
            <Pressable
              key={l.code}
              onPress={() => pick(l.code)}
              style={[s.lang, l.code === lang && s.langOn]}
              accessibilityRole="radio"
              accessibilityState={{ selected: l.code === lang }}>
              <Text style={[s.langText, l.code === lang && { color: C.onAccent }]}>{l.name}</Text>
            </Pressable>
          ))}
        </View>
      )}

      <ScrollView style={{ flex: 1 }} contentContainerStyle={s.body}>
        {isLiveEn ? (
          <View>
            <Text style={s.h2}>{T.liveEnglishTitle}</Text>
            <Text style={s.help}>{T.liveEnglishHelp}</Text>
            {playing && liveStats?.wireless && (
              <Text style={s.warn}>⚠️ {fmt(T.liveEnglishWireless, { device: liveStats.outputName })}</Text>
            )}
            {playing && liveStats?.phase === 'noAudio' && <Text style={s.warn}>{T.liveEnglishNoAudio}</Text>}
            {playing && liveStats?.active && (
              <Pressable onPress={() => setShowDetails((v) => !v)} style={{ marginTop: 20 }}>
                <Text style={s.detailsHead}>{T.details} {showDetails ? '▾' : '▸'}</Text>
                {showDetails && (
                  <View style={{ marginTop: 8 }}>
                    <Row k={T.delay} v={`${Math.round(liveStats.delayMs ?? 0)} ms${liveStats.wireless ? ' + Bluetooth' : ''}`} />
                    <Row k={T.buffer} v={`${Math.round(liveStats.bufferMs ?? 0)} ms`} />
                    <Row k={T.wobble} v={`${(liveStats.jitterMs ?? 0).toFixed(1)} ms`} />
                    <Row k={T.lost} v={`${(liveStats.lossPercent ?? 0).toFixed(1)}%`} />
                    <Row k={T.listeners} v={String(liveStats.listeners ?? 0)} />
                  </View>
                )}
              </Pressable>
            )}
          </View>
        ) : (
          <View>
            {st.captions.map((c, i) => (
              <Text key={c.seq} style={[s.caption, i >= st.captions.length - 2 && s.captionRecent]}>
                {c.text}
                {c.credit ? <Text style={s.credit}>{'\n' + c.credit}</Text> : null}
              </Text>
            ))}
            {waitingText && <Text style={s.waiting}>{waitingText}</Text>}
          </View>
        )}
      </ScrollView>

      <View style={s.footer}>
        {error && <Text style={s.error}>{error}</Text>}
        <Pressable
          onPress={() => (playing ? stop() : start())}
          disabled={!lang}
          style={({ pressed }) => [s.play, playing && s.stopBtn, pressed && { opacity: 0.8 }, !lang && { opacity: 0.4 }]}
          accessibilityRole="button">
          <Text style={[s.playText, playing && { color: C.fg }]}>{playing ? `■  ${T.stop}` : `▶  ${T.listen}`}</Text>
        </Pressable>
        <Text style={s.hint}>🎧 {T.useHeadphones}</Text>
      </View>
    </SafeAreaView>
  );
}

function KeepAwake() {
  useKeepAwake();
  return null;
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <View style={{ flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 3 }}>
      <Text style={{ color: C.dim, fontSize: 15 }}>{k}</Text>
      <Text style={{ color: C.fg, fontSize: 15, fontVariant: ['tabular-nums'] }}>{v}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 20, paddingTop: 8, paddingBottom: 12 },
  title: { color: C.fg, fontSize: 20, fontWeight: '700' },
  pill: { fontSize: 14, marginTop: 2 },
  change: { color: C.accent, fontSize: 16 },
  langs: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 20, paddingBottom: 8 },
  lang: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 999, borderWidth: 1, borderColor: C.border, backgroundColor: C.card },
  langOn: { backgroundColor: C.accent, borderColor: C.accent },
  langText: { color: C.fg, fontSize: 15 },
  body: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 24, flexGrow: 1, justifyContent: 'flex-end' },
  caption: { color: C.dim, fontSize: 22, lineHeight: 30, marginBottom: 14 },
  captionRecent: { color: C.fg },
  credit: { color: C.dim, fontSize: 13 },
  waiting: { color: C.dim, fontSize: 18, textAlign: 'center', marginVertical: 24 },
  h2: { color: C.fg, fontSize: 22, fontWeight: '700' },
  help: { color: C.dim, fontSize: 16, marginTop: 8, lineHeight: 22 },
  warn: { color: C.fg, backgroundColor: C.warnBg, borderRadius: 12, padding: 12, marginTop: 16, fontSize: 15, lineHeight: 21 },
  detailsHead: { color: C.accent, fontSize: 16 },
  footer: { paddingHorizontal: 20, paddingBottom: 12, paddingTop: 8, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.border },
  play: { backgroundColor: C.accent, borderRadius: 16, paddingVertical: 18, alignItems: 'center' },
  stopBtn: { backgroundColor: C.card, borderWidth: 1, borderColor: C.border },
  playText: { color: C.onAccent, fontSize: 20, fontWeight: '700' },
  hint: { color: C.dim, textAlign: 'center', marginTop: 8, fontSize: 14 },
  error: { color: C.bad, textAlign: 'center', marginBottom: 8 },
});
