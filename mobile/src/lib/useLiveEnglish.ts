import { useEffect, useState } from 'react';

import LiveEnglish, { LiveSender, LiveStats } from '../../modules/live-english';

export const liveEnglishSupported = !!LiveEnglish && LiveEnglish.isSupported();

/** Office senders found on this Wi-Fi (Bonjour). Empty where Live English isn't supported. */
export function useLiveSenders(): LiveSender[] {
  const [senders, setSenders] = useState<LiveSender[]>([]);
  useEffect(() => {
    if (!liveEnglishSupported || !LiveEnglish) return;
    const sub = LiveEnglish.addListener('onSenders', (e) => setSenders(e.senders));
    LiveEnglish.startBrowsing();
    return () => sub.remove();
  }, []);
  return senders;
}

/** Polls the player's readout while it plays. */
export function useLiveStats(active: boolean): LiveStats | null {
  const [stats, setStats] = useState<LiveStats | null>(null);
  useEffect(() => {
    if (!active || !LiveEnglish) {
      setStats(null);
      return;
    }
    const tick = () => setStats(LiveEnglish!.getStats());
    tick();
    const t = setInterval(tick, 500);
    return () => clearInterval(t);
  }, [active]);
  return stats;
}

export async function playLiveEnglish(title: string): Promise<void> {
  if (!LiveEnglish) throw new Error('Live English is not available on this phone.');
  await LiveEnglish.play(null, null, null, title);
}

export function stopLiveEnglish() {
  LiveEnglish?.stop();
}
