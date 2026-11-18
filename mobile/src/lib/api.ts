import { SERVER } from './config';

export type Language = { code: string; name: string };

export type CongregationEntry = {
  slug: string;
  name: string;
  title: string;
  city: string;
  church: string;
  unit_type: string;
  stake: string;
  live: boolean;
  starts_soon: boolean;
  languages: Language[];
  next_meeting: { label: string; day: string; time: string } | null;
};

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(SERVER + path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()) as T;
}

export async function searchCongregations(q: string): Promise<CongregationEntry[]> {
  const t = q.trim();
  if (t.length < 2) return [];
  return (await getJSON<{ results: CongregationEntry[] }>(`/api/hc/search?q=${encodeURIComponent(t)}`)).results;
}

// The position is rounded to about 100 m and only used to find congregations nearby.
export async function nearbyCongregations(lat: number, lng: number): Promise<CongregationEntry[]> {
  return (
    await getJSON<{ results: CongregationEntry[] }>(`/api/live?lat=${lat.toFixed(3)}&lng=${lng.toFixed(3)}`)
  ).results;
}
