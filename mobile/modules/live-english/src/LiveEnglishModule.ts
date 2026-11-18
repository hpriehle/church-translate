import { NativeModule, requireOptionalNativeModule } from 'expo';

import { LiveEnglishModuleEvents, LiveStats } from './LiveEnglish.types';

declare class LiveEnglishModule extends NativeModule<LiveEnglishModuleEvents> {
  isSupported(): boolean;
  startBrowsing(): void;
  stopBrowsing(): void;
  play(name: string | null, host: string | null, port: number | null, title: string | null): Promise<void>;
  stop(): void;
  getStats(): LiveStats;
}

// Optional so the JS still loads in Expo Go / web, where the native side isn't built in.
export default requireOptionalNativeModule<LiveEnglishModule>('LiveEnglish');
