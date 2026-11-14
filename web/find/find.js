/* hearchurch.com: find the right congregation before listening, three ways:
 *  1. on opening, the phone's location (asked at once) → the congregations within a mile, normally just one;
 *  2. location declined or nothing near → type the congregation's name: "Har" lists "Harlem First Ward — New York, NY";
 *  3. (elsewhere) the congregation's own link or QR code, hearchurch.com/<slug>.
 * The position is rounded and sent only to /api/live, which returns a rough box and never stores or logs it;
 * exact distances are worked out here.
 */
(() => {
  "use strict";
  const TEXT = {
    en: { intro: "Listen to your meeting in your language.", finding: "Finding meetings near you…",
          privacy: "Your approximate location is used only to find nearby congregations and is never stored.", privacyLink: "Privacy", locate: "Use my location", searchLink: "Search by name instead",
          searchLabel: "Your congregation's name", placeholder: "Ward, branch or parish name",
          denied: "Type your congregation's name.", unavailable: "We couldn't get your location. Type your congregation's name.",
          none: "No congregation within a mile of you. Type its name.", noMatch: "No congregation found with that name.",
          error: "Something went wrong. Try again.", live: "LIVE", startsAt: "starts {t}", since: "since {t}", mi: "mi",
          next: "Next: {d}", pick: "Tap your congregation." },
    fr: { intro: "Écoutez votre réunion dans votre langue.", finding: "Recherche des réunions près de vous…",
          privacy: "Votre position approximative sert uniquement à trouver les paroisses proches et n'est jamais conservée.", privacyLink: "Confidentialité", locate: "Utiliser ma position", searchLink: "Chercher par nom",
          searchLabel: "Le nom de votre paroisse", placeholder: "Nom de la paroisse ou de la branche",
          denied: "Tapez le nom de votre paroisse.", unavailable: "Impossible d'obtenir votre position. Tapez le nom de votre paroisse.",
          none: "Aucune paroisse à moins d'un mile. Tapez son nom.", noMatch: "Aucune paroisse trouvée avec ce nom.",
          error: "Un problème est survenu. Réessayez.", live: "EN DIRECT", startsAt: "début {t}", since: "depuis {t}", mi: "mi",
          next: "Prochaine : {d}", pick: "Touchez votre paroisse." },
    es: { intro: "Escuche su reunión en su idioma.", finding: "Buscando reuniones cerca de usted…",
          privacy: "Su ubicación aproximada solo se usa para encontrar congregaciones cercanas y nunca se guarda.", privacyLink: "Privacidad", locate: "Usar mi ubicación", searchLink: "Buscar por nombre",
          searchLabel: "El nombre de su congregación", placeholder: "Nombre del barrio, rama o parroquia",
          denied: "Escriba el nombre de su congregación.", unavailable: "No pudimos obtener su ubicación. Escriba el nombre de su congregación.",
          none: "No hay congregaciones a menos de una milla. Escriba su nombre.", noMatch: "No se encontró ninguna congregación con ese nombre.",
          error: "Algo salió mal. Inténtelo de nuevo.", live: "EN VIVO", startsAt: "empieza {t}", since: "desde {t}", mi: "mi",
          next: "Próxima: {d}", pick: "Toque su congregación." },
    ru: { intro: "Слушайте собрание на вашем языке.", finding: "Ищем собрания рядом с вами…",
          privacy: "Ваше примерное местоположение используется только для поиска ближайших общин и нигде не хранится.", privacyLink: "Конфиденциальность", locate: "Использовать моё местоположение", searchLink: "Искать по названию",
          searchLabel: "Название вашей общины", placeholder: "Название прихода или ветви",
          denied: "Введите название вашей общины.", unavailable: "Не удалось определить местоположение. Введите название вашей общины.",
          none: "В радиусе мили общин нет. Введите название.", noMatch: "Община с таким названием не найдена.",
          error: "Что-то пошло не так. Попробуйте ещё раз.", live: "В ЭФИРЕ", startsAt: "начало {t}", since: "с {t}", mi: "миль",
          next: "Следующее: {d}", pick: "Выберите свою общину." },
  };
  const lang = (navigator.language || "en").slice(0, 2);
  const T = TEXT[lang] || TEXT.en;
  const $ = (id) => document.getElementById(id);
  const fmt = (s, v) => s.replace(/\{(\w+)\}/g, (_, k) => v[k]);
  document.documentElement.lang = TEXT[lang] ? lang : "en";
  $("intro").textContent = T.intro;
  $("privacy").textContent = T.privacy + " ";
  const pl = document.createElement("a");
  pl.href = "/privacy"; pl.textContent = T.privacyLink;
  $("privacy").appendChild(pl);
  $("locate").textContent = T.locate;
  $("showSearch").textContent = T.searchLink;
  $("searchLabel").textContent = T.searchLabel;
  $("q").placeholder = T.placeholder;

  let here = null;   // {lat, lng}: kept in memory only, never sent anywhere except (rounded) to /api/live

  const miles = (a, b) => {
    const R = 3958.8, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  };
  const DAY_INDEX = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const clock = (epoch) => {
    try { return new Date(epoch * 1000).toLocaleTimeString(navigator.language, { hour: "numeric", minute: "2-digit" }); }
    catch { return new Date(epoch * 1000).toTimeString().slice(0, 5); }
  };
  const nextLabel = (n) => {
    let day = n.label;
    try {
      day = new Intl.DateTimeFormat(navigator.language, { weekday: "long" }).format(new Date(2024, 0, 1 + DAY_INDEX.indexOf(n.day))) + " " + n.time;
    } catch {}
    return fmt(T.next, { d: day });
  };
  const href = (c) => "/" + encodeURIComponent(c.slug) + location.hash;   // keeps a #french / #wolof choice

  function span(cls, text) { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }

  /** A congregation near the phone: name, town, distance and whether it is live. */
  function card(c) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = href(c);
    a.append(span("name", c.title || c.name));
    const dist = a.appendChild(span("dist", ""));
    if (here && c.lat != null && c.lng != null) {
      const d = miles(here, c);
      dist.textContent = d.toFixed(1) + " " + T.mi;
    }
    a.append(span("church", c.city || [c.church, c.stake].filter(Boolean).join(" · ")));
    const when = a.appendChild(span("when", ""));
    if (c.live) {
      when.append(span("badge", T.live));
      if (c.starts_at) when.append(" " + fmt(T.since, { t: clock(c.starts_at) }));
    } else if (c.starts_soon && c.starts_at) {
      when.classList.add("soon");
      when.textContent = fmt(T.startsAt, { t: clock(c.starts_at) });
    } else if (c.next_meeting) {
      when.textContent = nextLabel(c.next_meeting);
    }
    if (c.live && c.languages && c.languages.length) {
      // the meeting's translation languages: the same pills as on its own page
      const langs = a.appendChild(span("langs", ""));
      for (const l of c.languages) {
        const p = langs.appendChild(span("lang", l.name));
        p.dataset.code = l.code;
      }
    }
    li.append(a);
    return li;
  }

  const status = (msg) => { $("status").textContent = msg || ""; };

  function openSearch(focus) {
    $("searchForm").hidden = false;
    $("showSearch").hidden = true;
    if (focus) $("q").focus();
  }
  $("showSearch").onclick = () => openSearch(true);

  // ------------------------------------------------------------------ 1. location
  async function lookup(pos) {
    here = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    try {
      // ~100 m precision is plenty for a mile-wide search and tells the server less
      const r = await fetch(`/api/live?lat=${here.lat.toFixed(3)}&lng=${here.lng.toFixed(3)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const list = (await r.json()).results;
      $("results").replaceChildren(...list.map(card));
      if (!list.length) { status(T.none); openSearch(true); }
      else { status(list.length > 1 ? T.pick : ""); $("showSearch").hidden = false; }
    } catch {
      status(T.error);
      openSearch(false);
    }
    $("locate").hidden = true;
  }

  function locate() {
    $("results").replaceChildren();
    if (!navigator.geolocation) { status(T.unavailable); openSearch(true); return; }
    status(T.finding);
    navigator.geolocation.getCurrentPosition(lookup, (err) => {
      // 2. declined (or no fix): search by name instead, and offer to try the location again
      status(err && err.code === 1 ? T.denied : T.unavailable);
      openSearch(true);
      $("locate").hidden = false;
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  }
  $("locate").onclick = locate;

  // ------------------------------------------------------------------ 2. name, as a dropdown while typing
  const q = $("q"), found = $("found");
  let timer = null, seq = 0, items = [], active = -1;

  function closeList() { found.hidden = true; q.setAttribute("aria-expanded", "false"); active = -1; }
  function mark(i) {
    active = i;
    [...found.children].forEach((li, k) => li.setAttribute("aria-selected", String(k === i)));
  }
  function showList(list) {
    items = list;
    found.replaceChildren(...list.map((c, i) => {
      const li = document.createElement("li");
      li.id = "opt" + i;
      li.setAttribute("role", "option");
      const a = document.createElement("a");
      a.href = href(c);
      a.append(span("opt-name", c.title || c.name));
      if (c.city) a.append(span("opt-city", " — " + c.city));
      if (c.live) a.append(span("badge", T.live));
      li.append(a);
      return li;
    }));
    found.hidden = !list.length;
    q.setAttribute("aria-expanded", String(!!list.length));
    active = -1;
  }

  q.addEventListener("input", () => {
    clearTimeout(timer);
    const text = q.value.trim();
    if (text.length < 2) { closeList(); status(""); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await fetch("/api/hc/search?q=" + encodeURIComponent(text), { cache: "no-store" });
        if (!r.ok) throw new Error(r.status);
        const list = (await r.json()).results;
        if (mine !== seq) return;
        showList(list);
        status(list.length ? "" : T.noMatch);
      } catch { if (mine === seq) status(T.error); }
    }, 150);
  });
  q.addEventListener("keydown", (e) => {
    if (found.hidden || !items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); mark((active + 1) % items.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); mark((active - 1 + items.length) % items.length); }
    else if (e.key === "Escape") closeList();
  });
  $("searchForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const pick = items[active >= 0 ? active : 0];
    if (pick && !found.hidden) location.href = href(pick);
  });
  document.addEventListener("click", (e) => { if (!$("searchForm").contains(e.target)) closeList(); });

  locate();   // 1. ask for the location straight away
})();
