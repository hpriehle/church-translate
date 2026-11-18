import { Redirect } from 'expo-router';
import { useEffect, useState } from 'react';
import { View } from 'react-native';

import { loadCongregation, SavedCongregation } from '../lib/storage';
import { C } from '../lib/theme';

// Opens straight into the congregation chosen last time; only asks when there isn't one.
export default function Index() {
  const [saved, setSaved] = useState<SavedCongregation | null | undefined>(undefined);
  useEffect(() => {
    loadCongregation().then(setSaved);
  }, []);
  if (saved === undefined) return <View style={{ flex: 1, backgroundColor: C.bg }} />;
  if (saved) return <Redirect href={{ pathname: '/c/[slug]', params: { slug: saved.slug, title: saved.title } }} />;
  return <Redirect href="/find" />;
}
