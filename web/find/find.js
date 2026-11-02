/* Find my congregation: the phone asks the browser for its position, sends a rounded copy to /api/live
 * (which returns a rough box of live congregations and never stores or logs it) and works out the exact
 * distances itself. If location is declined, or nothing is live nearby, the search box takes over.
 */
(() => {
  "use strict";
  const TEXT = {
    en: { title: "Find my congregation", intro: "Listen to a meeting in your language, live.",
          button: "Find my congregation", finding: "Finding your location…", privacy: "Your location stays on your phone.",
          searchLabel: "Search by name", placeholder: "Ward, branch or parish name", searchLink: "Search by name instead",
          denied: "Location is off for this page. Search for your congregation by name instead.",
          unavailable: "We could not get your location. Search for your congregation by name instead.",
          none: "No live meetings near you right now. Search for your congregation by name.",
          noMatch: "No congregation found with that name.", checking: "Looking for live meetings…",
          wider: "Nothing within 1 mile, so this shows meetings within {r} miles.", error: "Something went wrong. Try again.",
          live: "LIVE", startsAt: "starts {t}", since: "since {t}", mi: "mi", next: "Next: {d}" },
    fr: { title: "Trouver ma congrégation", intro: "Écoutez une réunion dans votre langue, en direct.",
          button: "Trouver ma congrégation", finding: "Recherche de votre position…", privacy: "Votre position reste sur votre téléphone.",
          searchLabel: "Chercher par nom", placeholder: "Nom de la paroisse, branche ou ward", searchLink: "Chercher par nom",
          denied: "La localisation est désactivée pour cette page. Cherchez votre congrégation par son nom.",
          unavailable: "Impossible d'obtenir votre position. Cherchez votre congrégation par son nom.",
          none: "Aucune réunion en direct près de vous pour le moment. Cherchez votre congrégation par son nom.",
          noMatch: "Aucune congrégation trouvée avec ce nom.", checking: "Recherche des réunions en direct…",
          wider: "Rien à moins de 1 mile ; voici les réunions à moins de {r} miles.", error: "Un problème est survenu. Réessayez.",
          live: "EN DIRECT", startsAt: "début {t}", since: "depuis {t}", mi: "mi", next: "Prochaine : {d}" },
    es: { title: "Encontrar mi congregación", intro: "Escuche una reunión en su idioma, en vivo.",
          button: "Encontrar mi congregación", finding: "Buscando su ubicación…", privacy: "Su ubicación se queda en su teléfono.",
          searchLabel: "Buscar por nombre", placeholder: "Nombre del barrio, rama o parroquia", searchLink: "Buscar por nombre",
          denied: "La ubicación está desactivada para esta página. Busque su congregación por nombre.",
          unavailable: "No pudimos obtener su ubicación. Busque su congregación por nombre.",
          none: "No hay reuniones en vivo cerca de usted ahora. Busque su congregación por nombre.",
          noMatch: "No se encontró ninguna congregación con ese nombre.", checking: "Buscando reuniones en vivo…",
          wider: "No hay nada a menos de 1 milla; se muestran reuniones a menos de {r} millas.", error: "Algo salió mal. Inténtelo de nuevo.",
          live: "EN VIVO", startsAt: "empieza {t}", since: "desde {t}", mi: "mi", next: "Próxima: {d}" },
    ru: { title: "Найти мою общину", intro: "Слушайте собрание на вашем языке в прямом эфире.",
          button: "Найти мою общину", finding: "Определяем ваше местоположение…", privacy: "Ваше местоположение остаётся на вашем телефоне.",
          searchLabel: "Поиск по названию", placeholder: "Название прихода, ветви или общины", searchLink: "Искать по названию",
          denied: "Доступ к местоположению выключен. Найдите свою общину по названию.",
          unavailable: "Не удалось определить местоположение. Найдите свою общину по названию.",
          none: "Рядом сейчас нет собраний в прямом эфире. Найдите свою общину по названию.",
          noMatch: "Община с таким названием не найдена.", checking: "Ищем собрания в прямом эфире…",
          wider: "В радиусе 1 мили ничего нет; показаны собрания в радиусе {r} миль.", error: "Что-то пошло не так. Попробуйте ещё раз.",
          live: "В ЭФИРЕ", startsAt: "начало {t}", since: "с {t}", mi: "миль", next: "Следующее: {d}" },
  };
  const lang = (navigator.language || "en").slice(0, 2);
  const T = TEXT[lang] || TEXT.en;
  const $ = (id) => document.getElementById(id);
  const fmt = (s, v) => s.replace(/\{(\w+)\}/g, (_, k) => v[k]);
  document.documentElement.lang = TEXT[lang] ? lang : "en";
  document.title = T.title;
  $("title").textContent = T.title;
  $("intro").textContent = T.intro;
  $("locate").textContent = T.button;
  $("privacy").textContent = T.privacy;
  $("searchLabel").textContent = T.searchLabel;
  $("q").placeholder = T.placeholder;
  $("showSearch").textContent = T.searchLink;

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

  function row(c) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = "/c/" + encodeURIComponent(c.slug);
    const add = (cls, text) => { const s = document.createElement("span"); s.className = cls; s.textContent = text; a.appendChild(s); return s; };
    add("name", c.name);
    const dist = add("dist", "");
    if (here && c.lat != null && c.lng != null) {
      const d = miles(here, c);
      dist.textContent = (d < 10 ? d.toFixed(1) : Math.round(d)) + " " + T.mi;
    }
    add("church", [c.church, c.stake].filter(Boolean).join(" · "));
    const when = add("when", "");
    if (c.live) {
      const b = document.createElement("span");
      b.className = "badge"; b.textContent = T.live;
      when.appendChild(b);
      if (c.starts_at) when.appendChild(document.createTextNode(" " + fmt(T.since, { t: clock(c.starts_at) })));
    } else if (c.starts_soon && c.starts_at) {
      when.classList.add("soon");
      when.textContent = fmt(T.startsAt, { t: clock(c.starts_at) });
    } else if (c.next_meeting) {
      when.textContent = nextLabel(c.next_meeting);
    }
    li.appendChild(a);
    return li;
  }

  const rank = (c) => (c.live ? 0 : c.starts_soon ? 1 : 2);
  function sorted(list) {
    const d = (c) => (here && c.lat != null ? miles(here, c) : 0);
    return [...list].sort((a, b) => rank(a) - rank(b) || d(a) - d(b) || (a.starts_at || 0) - (b.starts_at || 0));
  }
  function show(ul, list) {
    ul.replaceChildren(...sorted(list).map(row));
  }

  const status = (msg) => { $("status").textContent = msg || ""; };
  function openSearch() {
    $("searchForm").hidden = false;
    $("searchLink").hidden = true;
  }
  $("showSearch").onclick = () => { openSearch(); $("q").focus(); };

  async function lookup(pos) {
    here = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    status(T.checking);
    try {
      // ~100 m precision is plenty for a mile-wide search and tells the server less
      const r = await fetch(`/api/live?lat=${here.lat.toFixed(3)}&lng=${here.lng.toFixed(3)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      show($("results"), data.results);
      if (!data.results.length) { status(T.none); openSearch(); }
      else status(data.radius_mi > 1 ? fmt(T.wider, { r: data.radius_mi }) : "");
    } catch {
      status(T.error);
      openSearch();
    }
    $("locate").disabled = false;
    $("locate").textContent = T.button;
  }

  $("locate").onclick = () => {
    $("results").replaceChildren();
    if (!navigator.geolocation) { status(T.unavailable); openSearch(); return; }
    $("locate").disabled = true;
    status(T.finding);
    navigator.geolocation.getCurrentPosition(lookup, (err) => {
      status(err && err.code === 1 ? T.denied : T.unavailable);
      openSearch();
      $("locate").disabled = false;
    }, { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 });
  };

  let timer = null, seq = 0;
  $("q").addEventListener("input", () => {
    clearTimeout(timer);
    const q = $("q").value.trim();
    if (q.length < 2) { $("found").replaceChildren(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await fetch("/api/hc/search?q=" + encodeURIComponent(q), { cache: "no-store" });
        if (!r.ok) throw new Error(r.status);
        const list = (await r.json()).results;
        if (mine !== seq) return;
        show($("found"), list);
        status(list.length ? "" : T.noMatch);
      } catch { if (mine === seq) status(T.error); }
    }, 300);
  });
  $("searchForm").addEventListener("submit", (e) => e.preventDefault());
})();
