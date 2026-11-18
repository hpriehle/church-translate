import AsyncStorage from '@react-native-async-storage/async-storage';

export type SavedCongregation = { slug: string; title: string };

const CONG = 'congregation';
const LANG = 'lang';

export async function loadCongregation(): Promise<SavedCongregation | null> {
  try {
    const v = await AsyncStorage.getItem(CONG);
    return v ? (JSON.parse(v) as SavedCongregation) : null;
  } catch {
    return null;
  }
}

export async function saveCongregation(c: SavedCongregation): Promise<void> {
  await AsyncStorage.setItem(CONG, JSON.stringify(c));
}

export async function forgetCongregation(): Promise<void> {
  await AsyncStorage.removeItem(CONG);
}

export async function loadLang(): Promise<string> {
  return (await AsyncStorage.getItem(LANG)) || '';
}

export async function saveLang(code: string): Promise<void> {
  await AsyncStorage.setItem(LANG, code);
}
