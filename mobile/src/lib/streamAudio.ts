import { AudioPlayer, createAudioPlayer, setAudioModeAsync } from 'expo-audio';

import { SERVER } from './config';

/**
 * The translated voice: one never-ending MP3 per language (/c/<slug>/stream/<lang>.mp3), like
 * internet radio. Plays in the background with lock-screen controls. A stalled stream is
 * restarted at the live point.
 */
class StreamAudio {
  private player: AudioPlayer | null = null;
  private url = '';
  private meta = { title: '', artist: '' };
  private wanted = false;
  private lastProgress = 0;
  private lastTime = -1;
  private watchdog: ReturnType<typeof setInterval> | undefined;
  onPlayingChange?: (playing: boolean) => void;

  async play(slug: string, lang: string, title: string, artist: string) {
    await setAudioModeAsync({ playsInSilentMode: true, shouldPlayInBackground: true, interruptionMode: 'doNotMix' });
    this.wanted = true;
    this.meta = { title, artist };
    this.url = `${SERVER}/c/${encodeURIComponent(slug)}/stream/${encodeURIComponent(lang)}.mp3`;
    this.load();
    clearInterval(this.watchdog);
    this.watchdog = setInterval(() => this.check(), 2000);
  }

  private load() {
    const source = { uri: `${this.url}?t=${Date.now()}` }; // always join at the live point
    if (!this.player) {
      this.player = createAudioPlayer(source, { updateInterval: 1000 });
      this.player.addListener('playbackStatusUpdate', (s) => {
        if (s.playing && s.currentTime !== this.lastTime) {
          this.lastTime = s.currentTime;
          this.lastProgress = Date.now();
        }
        this.onPlayingChange?.(s.playing);
      });
    } else {
      this.player.replace(source);
    }
    this.lastProgress = Date.now();
    this.player.play();
    try {
      this.player.setActiveForLockScreen(true, this.meta, { isLiveStream: true });
    } catch {}
  }

  /** The stream stops advancing when the network drops or the server restarts: reload it. */
  private check() {
    if (!this.wanted || !this.player) return;
    if (Date.now() - this.lastProgress > 8000) this.load();
  }

  stop() {
    this.wanted = false;
    clearInterval(this.watchdog);
    if (this.player) {
      try {
        this.player.clearLockScreenControls();
      } catch {}
      this.player.pause();
      this.player.remove();
      this.player = null;
    }
    this.onPlayingChange?.(false);
  }

  get active() {
    return this.wanted;
  }
}

export const streamAudio = new StreamAudio();
