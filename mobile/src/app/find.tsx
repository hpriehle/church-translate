import * as Location from 'expo-location';
import { router } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { CongregationEntry, nearbyCongregations, searchCongregations } from '../lib/api';
import { fmt, T } from '../lib/i18n';
import { saveCongregation } from '../lib/storage';
import { C } from '../lib/theme';

export default function Find() {
  const [q, setQ] = useState('');
  const [results, setResults] = useState<CongregationEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const seq = useRef(0);

  // Search as they type (debounced); a newer search wins over a slower older one.
  useEffect(() => {
    const t = q.trim();
    if (t.length < 2) {
      if (!note) setResults(null);
      return;
    }
    const id = ++seq.current;
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await searchCongregations(t);
        if (id === seq.current) {
          setResults(r);
          setNote(r.length ? null : T.noResults);
        }
      } catch {
        if (id === seq.current) setNote(T.searchError);
      } finally {
        if (id === seq.current) setBusy(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [q]);

  const nearby = async () => {
    const id = ++seq.current;
    setQ('');
    setNote(T.locating);
    setBusy(true);
    try {
      const perm = await Location.requestForegroundPermissionsAsync();
      if (!perm.granted) {
        setNote(T.locationDenied);
        return;
      }
      const pos = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced });
      const r = await nearbyCongregations(pos.coords.latitude, pos.coords.longitude);
      if (id !== seq.current) return;
      setResults(r);
      setNote(r.length ? null : T.noneNearby);
    } catch {
      if (id === seq.current) setNote(T.searchError);
    } finally {
      if (id === seq.current) setBusy(false);
    }
  };

  const choose = async (c: CongregationEntry) => {
    await saveCongregation({ slug: c.slug, title: c.title });
    router.replace({ pathname: '/c/[slug]', params: { slug: c.slug, title: c.title } });
  };

  return (
    <SafeAreaView style={s.root} edges={['top', 'left', 'right']}>
      <Text style={s.brand}>{T.appName}</Text>
      <Text style={s.h1}>{T.findTitle}</Text>
      <TextInput
        style={s.input}
        value={q}
        onChangeText={setQ}
        placeholder={T.findHint}
        placeholderTextColor={C.dim}
        autoCorrect={false}
        returnKeyType="search"
        clearButtonMode="while-editing"
        accessibilityLabel={T.findHint}
      />
      <Pressable style={({ pressed }) => [s.locBtn, pressed && { opacity: 0.7 }]} onPress={nearby} accessibilityRole="button">
        <Text style={s.locText}>📍 {T.useLocation}</Text>
      </Pressable>
      {busy && <ActivityIndicator color={C.accent} style={{ marginTop: 16 }} />}
      {note && !busy && <Text style={s.note}>{note}</Text>}
      <FlatList
        data={results ?? []}
        keyExtractor={(c) => c.slug}
        keyboardShouldPersistTaps="handled"
        contentContainerStyle={{ paddingBottom: 40 }}
        renderItem={({ item }) => (
          <Pressable style={({ pressed }) => [s.row, pressed && { backgroundColor: C.border }]} onPress={() => choose(item)} accessibilityRole="button">
            <View style={{ flex: 1 }}>
              <Text style={s.rowTitle}>{item.title}</Text>
              <Text style={s.rowSub}>
                {item.city}
                {item.next_meeting && !item.live ? ' · ' + fmt(T.next, { label: item.next_meeting.label }) : ''}
              </Text>
            </View>
            {item.live ? <Text style={[s.badge, { color: C.ok }]}>● {T.liveNow}</Text> : item.starts_soon ? <Text style={[s.badge, { color: C.wait }]}>{T.startsSoon}</Text> : null}
          </Pressable>
        )}
      />
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
  brand: { color: C.accent, fontWeight: '600', fontSize: 15, marginTop: 12 },
  h1: { color: C.fg, fontSize: 28, fontWeight: '700', marginTop: 6, marginBottom: 18 },
  input: { backgroundColor: C.card, color: C.fg, borderRadius: 12, paddingHorizontal: 14, paddingVertical: 14, fontSize: 17, borderWidth: 1, borderColor: C.border },
  locBtn: { marginTop: 12, paddingVertical: 12, alignItems: 'center', borderRadius: 12, borderWidth: 1, borderColor: C.border },
  locText: { color: C.fg, fontSize: 16 },
  note: { color: C.dim, marginTop: 18, fontSize: 15, textAlign: 'center' },
  row: { flexDirection: 'row', alignItems: 'center', paddingVertical: 14, paddingHorizontal: 4, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border },
  rowTitle: { color: C.fg, fontSize: 17, fontWeight: '600' },
  rowSub: { color: C.dim, fontSize: 14, marginTop: 2 },
  badge: { fontSize: 13, marginLeft: 8 },
});
