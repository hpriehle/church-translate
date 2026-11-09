// Every language a listener can choose: OutputLanguage.all in mac/HearChurch/Sources/Profile.swift, plus English.
// Add one here only once the app offers it. Each needs a flag in public/download/media/flags/ (flag-icons, MIT).
export const LANGUAGES = [
  { code: "fr", name: "French", native: "Français", flag: "fr" },
  { code: "wo", name: "Wolof", native: "Wolof", flag: "sn" },
  { code: "es", name: "Spanish", native: "Español", flag: "es", also: "castellano" },
  { code: "ru", name: "Russian", native: "Русский", flag: "ru" },
  { code: "pt", name: "Portuguese", native: "Português", flag: "br", also: "brazilian" },
  { code: "uk", name: "Ukrainian", native: "Українська", flag: "ua" },
  { code: "fa", name: "Persian", native: "فارسی", flag: "ir", also: "farsi", rtl: true },
  { code: "ne", name: "Nepali", native: "नेपाली", flag: "np" },
  { code: "vi", name: "Vietnamese", native: "Tiếng Việt", flag: "vn" },
  { code: "en", name: "English", native: "English", flag: "us" },
];

const names = LANGUAGES.map((l) => l.name);
export const LANGUAGE_LIST = `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
