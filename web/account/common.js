// Shared by hearchurch.com/start and /account: calls to the hub (with the CSRF token), the address field with
// suggestions (Photon, photon.komoot.io: OpenStreetMap data, no key) and its map (Leaflet, OpenStreetMap tiles),
// and the meeting-times editor.
"use strict";

const HC = {
  csrf: document.querySelector('meta[name="csrf"]').content,

  // The church choices: the same list as the Mac app's onboarding (mac/HearChurch/Sources/Profile.swift, Churches.all).
  LDS: "The Church of Jesus Christ of Latter-day Saints",
  CHURCHES: ["The Church of Jesus Christ of Latter-day Saints", "Catholic", "Baptist", "Methodist", "Non-denominational"],
  DAYS: [["sun", "Sunday"], ["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"],
         ["fri", "Friday"], ["sat", "Saturday"]],

  // POST (with body) or GET; resolves to the JSON answer, throws an Error with the hub's message.
  async call(path, body, opts = {}) {
    const init = { method: body === undefined ? "GET" : "POST", credentials: "same-origin",
                   headers: { "X-CSRF-Token": HC.csrf } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(path, init);
    } catch (e) {
      throw new Error("We couldn't reach hearchurch.com. Check your connection and try again.");
    }
    let data = {};
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok && !(opts.ok || []).includes(r.status)) {
      const d = data.detail;
      let text = data.message || (d && typeof d === "object" ? d.message : d);
      if (Array.isArray(d)) text = "Please check the fields: " + d.map((x) => (x.loc || []).slice(-1)[0]).join(", ") + ".";
      if (r.status === 429 && !text) text = "Too many tries: wait a few minutes and try again.";
      const err = new Error(text || "Something went wrong. Try again.");
      err.status = r.status;
      err.data = data;
      if (r.status === 401 && HC.onSignedOut) HC.onSignedOut();
      throw err;
    }
    return data;
  },

  say(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
    el.hidden = !text;
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  usd(n) {
    return "$" + (Number.isInteger(n) ? n : n.toFixed(2));
  },

  hours(h) {
    const v = Math.round(h * 10) / 10;
    return (Number.isInteger(v) ? String(v) : v.toFixed(1));
  },

  date(t) {
    return new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  },

  isEmail(s) {
    return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s.trim());
  },

  // Meeting times: rows of day + time inside `box`. Returns {get(), set(list)}.
  timesEditor(box) {
    const list = box.querySelector(".times");
    function row(t = { day: "sun", time: "10:00" }) {
      const r = document.createElement("div");
      r.className = "row";
      r.innerHTML = '<select class="day" aria-label="Day">' +
        HC.DAYS.map(([v, n]) => `<option value="${v}">${n}</option>`).join("") +
        '</select><input type="time" aria-label="Time" required><button type="button" class="link">Remove</button>';
      r.querySelector("select").value = t.day;
      r.querySelector("input").value = t.time;
      r.querySelector("button").onclick = () => r.remove();
      list.append(r);
    }
    box.querySelector(".add-time").onclick = () => row();
    return {
      get: () => [...list.querySelectorAll(".row")].map((r) => ({ day: r.querySelector("select").value,
                                                                 time: r.querySelector("input").value }))
        .filter((t) => /^\d\d:\d\d$/.test(t.time)),
      set: (times) => { list.innerHTML = ""; (times || []).forEach(row); },
    };
  },

  // The address field: suggestions as you type (debounced, Photon) and a map with a pin that can be dragged.
  // box holds input.address, ul.suggestions and div.map. onChange({address, lat, lng}) after a pick or a drag.
  addressPicker(box, onChange) {
    const input = box.querySelector("input.address");
    const ul = box.querySelector("ul.suggestions");
    const state = { address: input.value, lat: null, lng: null };
    let timer = null, ctrl = null, items = [], active = -1, map = null, marker = null;

    function ensureMap() {
      if (map || !window.L) return;
      map = L.map(box.querySelector(".map"), { scrollWheelZoom: false }).setView([25, 0], 2);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        // OSM's tile policy blocks requests without a Referer; our pages send Referrer-Policy: same-origin.
        referrerPolicy: "strict-origin-when-cross-origin",
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(map);
      map.on("click", (e) => place(e.latlng.lat, e.latlng.lng, true));
    }
    function place(lat, lng, moved) {
      ensureMap();
      state.lat = lat;
      state.lng = lng;
      if (!map) { onChange && onChange({ ...state }); return; }
      if (!marker) {
        marker = L.marker([lat, lng], { draggable: true, keyboard: true, title: "The building" }).addTo(map);
        marker.on("dragend", () => { const p = marker.getLatLng(); state.lat = p.lat; state.lng = p.lng; onChange && onChange({ ...state }); });
      } else {
        marker.setLatLng([lat, lng]);
      }
      if (!moved) map.setView([lat, lng], 17);
      onChange && onChange({ ...state });
    }
    function label(p) {
      const street = [p.housenumber, p.street].filter(Boolean).join(" ");
      const parts = [p.name && p.name !== street ? p.name : null, street || null, p.city || p.town || p.village || p.county,
                     p.state, p.postcode, p.country];
      return [...new Set(parts.filter(Boolean))].join(", ");
    }
    function close() { ul.hidden = true; ul.innerHTML = ""; items = []; active = -1; input.setAttribute("aria-expanded", "false"); }
    function show() {
      ul.innerHTML = items.map((it, i) => `<li role="option" id="sug-${i}" aria-selected="${i === active}">${HC.esc(it.label)}</li>`).join("");
      ul.hidden = !items.length;
      input.setAttribute("aria-expanded", String(!!items.length));
      [...ul.children].forEach((li, i) => li.onmousedown = (e) => { e.preventDefault(); pick(i); });
    }
    function pick(i) {
      const it = items[i];
      if (!it) return;
      input.value = it.label;
      state.address = it.label;
      close();
      place(it.lat, it.lng, false);
    }
    async function search(q) {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      try {
        const r = await fetch("https://photon.komoot.io/api/?limit=5&q=" + encodeURIComponent(q), { signal: ctrl.signal });
        const data = await r.json();
        items = (data.features || []).map((f) => ({ label: label(f.properties || {}), lng: f.geometry.coordinates[0],
                                                    lat: f.geometry.coordinates[1] })).filter((x) => x.label);
        active = -1;
        show();
      } catch (e) { /* aborted or offline: keep typing works */ }
    }
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", ul.id);
    input.addEventListener("input", () => {
      state.address = input.value;
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 3) { close(); return; }
      timer = setTimeout(() => search(q), 350);
    });
    input.addEventListener("keydown", (e) => {
      if (ul.hidden) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        show();
        input.setAttribute("aria-activedescendant", "sug-" + active);
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        pick(active);
      } else if (e.key === "Escape") {
        close();
      }
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
    return {
      state,
      show() { ensureMap(); if (map) setTimeout(() => map.invalidateSize(), 0); },
      set(address, lat, lng) {
        input.value = address || "";
        state.address = input.value;
        if (lat != null && lng != null) place(lat, lng, false); else ensureMap();
      },
    };
  },
};
