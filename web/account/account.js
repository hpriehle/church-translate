// hearchurch.com/account: sign in by e-mailed code, then the congregation's hours, billing, code, details, e-mails
// and Macs. Everything goes through the same hub routes the Mac app uses (/api/hc/billing, /topup, /emails, …).
"use strict";

const $ = (id) => document.getElementById(id);
let me = null, bill = null, acct = null;

HC.onSignedOut = () => { if (me) window.location.reload(); };

function brand(card) {
  const b = card.brand || "Card";
  const name = b === "amex" ? "Amex" : b.charAt(0).toUpperCase() + b.slice(1);
  return card.last4 ? `${name} ••${card.last4}` : name;
}

// A yes/no question in a dialog; resolves true for yes.
function ask(text, yes) {
  const d = $("confirm");
  $("confirm-text").textContent = text;
  $("confirm-yes").textContent = yes;
  return new Promise((resolve) => {
    const finish = (v) => { d.close(); resolve(v); };
    $("confirm-yes").onclick = () => finish(true);
    $("confirm-no").onclick = () => finish(false);
    d.oncancel = () => resolve(false);
    d.showModal();
  });
}

// ------------------------------------------------------------------ sign in

let loginEmail = "";
$("login-email").onsubmit = async (ev) => {
  ev.preventDefault();
  loginEmail = $("email").value.trim().toLowerCase();
  if (!HC.isEmail(loginEmail)) return HC.say($("login-msg"), "Enter your e-mail address.", "bad");
  $("send").disabled = true;
  try {
    const r = await HC.call("/api/hc/web/login-code", { email: loginEmail });
    $("login-sent").textContent = r.message;
    HC.say($("login-msg"), "");
    $("login-email").hidden = true;
    $("login-code").hidden = false;
    $("code").focus();
  } catch (e) {
    HC.say($("login-msg"), e.message, "bad");
  } finally {
    $("send").disabled = false;
  }
};
$("other-email").onclick = () => { $("login-code").hidden = true; $("login-email").hidden = false; HC.say($("login-msg"), ""); };
$("login-code").onsubmit = async (ev) => {
  ev.preventDefault();
  const code = $("code").value.replace(/\D/g, "");
  if (code.length !== 6) return HC.say($("login-msg"), "Enter the 6 digits from the e-mail.", "bad");
  $("verify").disabled = true;
  try {
    await HC.call("/api/hc/web/login", { email: loginEmail, code });
    window.location.reload();
  } catch (e) {
    HC.say($("login-msg"), e.message, "bad");
    $("verify").disabled = false;
  }
};

// ------------------------------------------------------------------ overview + billing

function renderOverview(usage) {
  const credits = bill.billing === "credits";
  $("ov-credits").hidden = !credits;
  $("ov-type").hidden = credits;
  $("ov-type").textContent = typeText();
  if (credits) {
    $("ov-free").textContent = HC.hours(bill.free_hours_left);
    $("ov-paid").textContent = HC.hours(Math.max(0, bill.paid_hours_left));
    $("ov-message").textContent = bill.out_of_credit ? bill.message : "Every month starts with a free hour.";
  }
  const rows = (usage.meetings || []).slice(0, 5);
  $("meetings").hidden = !rows.length;
  $("no-meetings").hidden = !!rows.length;
  $("meetings").tBodies[0].innerHTML = rows.map((m) =>
    `<tr><td>${HC.date(m.started_at)}</td><td>${HC.esc((m.languages || []).join(", "))}</td><td>${HC.hours(m.language_hours)}</td></tr>`).join("");
}

function typeText() {
  if (bill.billing === "internal") return "Included: no charge.";
  if (bill.billing === "own_key") return "Your congregation uses its own translation key. You enter the key in the Mac app's AI tab.";
  return "";
}

function price(usd) {
  const p = bill.discount && bill.discount.percent;
  return p ? Math.round(usd * (100 - p)) / 100 : usd;
}

function renderBilling(usage) {
  const credits = bill.billing === "credits";
  $("bill-credits").hidden = !credits;
  $("bill-type").hidden = credits;
  $("bill-type").innerHTML = credits ? "" : `<p class="lede">${HC.esc(typeText())}</p>`;
  if (credits) {
    const disc = bill.discount;
    $("discount").hidden = !disc;
    $("discount").textContent = disc ? `${disc.percent}% off your next bundle (from your code).` : "";
    $("bundles").innerHTML = bill.bundles.map((x) =>
      `<button type="button" data-usd="${x.usd}" ${bill.can_buy ? "" : "disabled"}><b>${HC.usd(price(x.usd))}</b>` +
      `<span>${HC.hours(x.hours)} hours</span><span class="quiet small">${HC.usd(x.usd_per_hour)}/hour</span></button>`).join("");
    $("bundles").querySelectorAll("button").forEach((b) => b.onclick = () => buy(Number(b.dataset.usd)));
    $("buy-hint").textContent = !bill.can_buy ? "Buying hours isn't available yet. Write to support@hearchurch.com."
      : bill.card ? `One click charges your saved card (${brand(bill.card)}). Hours don't expire.`
      : "Stripe's secure payment page opens. Hours don't expire.";

    const c = bill.card;
    $("card-row").innerHTML = c
      ? `<span class="grow">${HC.esc(brand(c))}${c.exp_month ? ` <span class="quiet small">expires ${String(c.exp_month).padStart(2, "0")}/${String(c.exp_year).slice(-2)}</span>` : ""}</span>` +
        '<button type="button" id="card-replace">Replace</button><button type="button" class="danger" id="card-remove">Remove</button>'
      : '<span class="quiet">No card saved.</span>' + (bill.can_save_card ? '<button type="button" id="card-add">Add a card</button>' : "");
    if ($("card-replace")) $("card-replace").onclick = addCard;
    if ($("card-add")) $("card-add").onclick = addCard;
    if ($("card-remove")) $("card-remove").onclick = removeCard;

    const a = bill.auto_topup;
    $("auto-on").checked = !!a.enabled;
    $("auto-on").disabled = !c;
    $("auto-usd").innerHTML = bill.bundles.map((x) => `<option value="${x.usd}">$${x.usd} (${HC.hours(x.hours)} hours)</option>`).join("");
    $("auto-usd").value = String(a.usd);
    $("auto-hint").textContent = c ? `After a meeting, when fewer than ${HC.hours(a.below_hours)} paid hour is left, your card is charged for this bundle (at most once a day). Off by default.`
      : "Save a card first.";
  }
  const pays = usage.charges || [];
  $("payments").hidden = !pays.length;
  $("no-payments").hidden = !!pays.length;
  const what = { top_up: "Hours bought", free_hours: "Free hours (code)", manual_credit: "Credit added", refund: "Refund" };
  $("payments").tBodies[0].innerHTML = pays.map((p) =>
    `<tr><td>${HC.date(p.at)}</td><td>${HC.esc(what[p.kind] || p.kind)}${p.hours ? ` · ${HC.hours(p.hours)} hours` : ""}</td>` +
    `<td>${p.amount_usd < 0 ? "−" : ""}$${Math.abs(p.amount_usd).toFixed(2)}</td>` +
    `<td>${p.receipt_url ? `<a href="${HC.esc(p.receipt_url)}" target="_blank" rel="noopener">Receipt</a>` : ""}</td></tr>`).join("");
}

async function buy(usd) {
  const x = bill.bundles.find((b) => b.usd === usd);
  const msg = $("buy-msg");
  HC.say(msg, "");
  try {
    if (bill.card && bill.can_save_card) {
      // 1-click always asks first.
      const ok = await ask(`Buy ${HC.hours(x.hours)} hours for ${HC.usd(price(usd))} on ${brand(bill.card)}?`, `Buy for ${HC.usd(price(usd))}`);
      if (!ok) return;
      HC.say(msg, `Charging ${brand(bill.card)}…`);
      let r;
      try {
        r = await HC.call("/api/hc/topup", { usd, request_id: crypto.randomUUID().replace(/[^A-Za-z0-9-]/g, "") });
      } catch (e) {
        if (e.status !== 409) throw e;
        HC.say(msg, e.message);   // no card after all, or the bank wants the full page: Stripe's page
        return checkout(usd);
      }
      if (r.status === "requires_action" && r.url) { window.location.href = r.url; return; }
      HC.say(msg, r.message || "Payment made. The hours show in a few seconds.", "good");
      refreshSoon();
      return;
    }
    await checkout(usd);
  } catch (e) {
    HC.say(msg, e.message, "bad");
  }
}

async function checkout(usd) {
  const r = await HC.call("/api/hc/checkout", { usd, save_card: !!bill.can_save_card });
  window.location.href = r.url;
}

async function addCard() {
  try {
    const r = await HC.call("/api/hc/card/setup", {});
    window.location.href = r.url;
  } catch (e) {
    HC.say($("buy-msg"), e.message, "bad");
  }
}

async function removeCard() {
  if (!(await ask("Remove the saved card? Buying opens Stripe's page again, and automatic top-up turns off.", "Remove card"))) return;
  try {
    await HC.call("/api/hc/card/remove", {});
    await load();
  } catch (e) {
    HC.say($("buy-msg"), e.message, "bad");
  }
}

async function setAuto() {
  try {
    const r = await HC.call("/api/hc/auto-topup", { enabled: $("auto-on").checked, usd: Number($("auto-usd").value) });
    bill.auto_topup = r.auto_topup;
    HC.say($("auto-msg"), r.auto_topup.enabled ? `Automatic top-up is on: $${r.auto_topup.usd}.` : "Automatic top-up is off.", "good");
  } catch (e) {
    HC.say($("auto-msg"), e.message, "bad");
    $("auto-on").checked = !!bill.auto_topup.enabled;
  }
}
$("auto-on").onchange = setAuto;
$("auto-usd").onchange = setAuto;

function refreshSoon() {   // credit arrives with Stripe's webhook, within seconds
  let n = 0;
  const t = setInterval(async () => { n += 1; await load().catch(() => {}); if (n >= 4) clearInterval(t); }, 4000);
}

// ------------------------------------------------------------------ code

$("code-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const code = $("promo").value.trim();
  if (!code) return;
  try {
    const r = await HC.call("/api/hc/codes/redeem", { code });
    HC.say($("code-msg"), r.type === "own_key" ? "Code accepted: your congregation uses its own translation key. You enter it in the Mac app's AI tab." : r.message, "good");
    $("promo").value = "";
    await load();
  } catch (e) {
    HC.say($("code-msg"), e.message, "bad");
  }
};

// ------------------------------------------------------------------ congregation

const times = HC.timesEditor($("c-times-box"));
const picker = HC.addressPicker($("c-place-box"));

function renderCongregation() {
  const c = me.congregation;
  $("c-name").value = c.name;
  $("c-stake-box").hidden = c.church !== HC.LDS;
  $("c-stake").value = c.stake || "";
  times.set(c.meeting_times);
  picker.set(c.address, c.lat, c.lng);
  $("slug-host").textContent = location.host + "/";
  $("slug").value = acct.slug;
  renderAliases();
}

function renderAliases() {
  $("aliases").textContent = acct.aliases && acct.aliases.length
    ? `Old links that still lead here: ${acct.aliases.map((a) => location.host + "/" + a).join(", ")}.`
    : "Printed QR codes keep working after a change: the old link redirects to the new one.";
}

$("cong-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const body = { name: $("c-name").value.trim(), meeting_times: times.get() };
  if (me.congregation.church === HC.LDS) body.stake = $("c-stake").value.trim();
  try { body.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined; } catch (e) { /* none */ }
  try {
    await HC.call("/api/hc/congregation", body);
    HC.say($("cong-msg"), "Saved.", "good");
    await refreshMe();
  } catch (e) {
    HC.say($("cong-msg"), e.message, "bad");
  }
};

$("save-place").onclick = async () => {
  const p = picker.state;
  const body = p.lat != null ? { lat: p.lat, lng: p.lng, address: (p.address || "").trim() } : { address: (p.address || "").trim() };
  if (!body.address && body.lat == null) return HC.say($("place-msg"), "Type the address, or click the map.", "bad");
  try {
    const r = await HC.call("/api/hc/location", body);
    picker.set(r.address, r.lat, r.lng);
    HC.say($("place-msg"), "Address saved.", "good");
  } catch (e) {
    HC.say($("place-msg"), e.message, "bad");
  }
};

$("slug-form").onsubmit = async (ev) => {
  ev.preventDefault();
  try {
    const r = await HC.call("/api/hc/slug", { slug: $("slug").value.trim().toLowerCase() });
    acct.slug = r.slug;
    acct.aliases = r.aliases;
    renderAliases();
    HC.say($("slug-msg"), `Your listener link is now ${r.url.replace(/^https?:\/\//, "")}.`, "good");
  } catch (e) {
    HC.say($("slug-msg"), e.message, "bad");
  }
};

// ------------------------------------------------------------------ e-mails

function renderEmails() {
  $("email-list").innerHTML = acct.emails.map((e, i) =>
    `<li><span class="grow">${HC.esc(e.email)} ${e.main ? '<span class="tag">Main</span>' : ""}</span>` +
    (e.main ? "" : `<button type="button" data-main="${i}">Make main</button><button type="button" class="danger" data-remove="${i}">Remove</button>`) +
    "</li>").join("");
  $("email-list").querySelectorAll("[data-main]").forEach((b) => b.onclick = () =>
    emailAction("/api/hc/emails/main", acct.emails[b.dataset.main].email));
  $("email-list").querySelectorAll("[data-remove]").forEach((b) => b.onclick = async () => {
    const e = acct.emails[b.dataset.remove].email;
    if (await ask(`Remove ${e}? It can no longer sign in here.`, "Remove")) emailAction("/api/hc/emails/remove", e);
  });
}

async function emailAction(path, email) {
  try {
    const r = await HC.call(path, { email });
    acct = { ...acct, ...r };
    renderEmails();
    HC.say($("email-msg"), "");
  } catch (e) {
    HC.say($("email-msg"), e.message, "bad");
  }
}

$("email-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const email = $("new-email").value.trim().toLowerCase();
  if (!HC.isEmail(email)) return HC.say($("email-msg"), "Enter an e-mail address.", "bad");
  await emailAction("/api/hc/emails", email);
  $("new-email").value = "";
};

// ------------------------------------------------------------------ devices

function renderDevices() {
  const ds = acct.devices || [];
  $("no-devices").hidden = !!ds.length;
  $("device-list").innerHTML = ds.map((d, i) =>
    `<li><span class="grow">${HC.esc(d.name)}<br><span class="quiet small">` +
    `${d.last_seen ? "Last seen " + HC.date(d.last_seen) : "Connected " + HC.date(d.created_at)}` +
    `${d.app_version ? " · app " + HC.esc(d.app_version) : ""}</span></span>` +
    `<button type="button" class="danger" data-revoke="${i}">Revoke</button></li>`).join("");
  $("device-list").querySelectorAll("[data-revoke]").forEach((b) => b.onclick = async () => {
    const d = ds[b.dataset.revoke];
    if (!(await ask(`Revoke ${d.name}? It is signed out, and a meeting it runs ends.`, "Revoke"))) return;
    try {
      const r = await HC.call("/api/hc/devices/revoke", { id: d.id });
      acct = { ...acct, ...r };
      renderDevices();
    } catch (e) {
      HC.say($("device-msg"), e.message, "bad");
    }
  });
}

$("pair").onclick = async () => {
  try {
    const r = await HC.call("/api/hc/pairing-code", {});
    $("pair-code").textContent = r.code;
    $("pair-code").hidden = false;
    HC.say($("device-msg"), `Works until ${new Date(r.expires_at * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}.`);
  } catch (e) {
    HC.say($("device-msg"), e.message, "bad");
  }
};

// ------------------------------------------------------------------ sign out, switch

async function logout(everywhere) {
  try { await HC.call("/api/hc/web/logout", { everywhere }); } catch (e) { /* the cookie goes anyway */ }
  window.location.href = "/account";
}
$("logout").onclick = () => logout(false);
$("logout-all").onclick = () => logout(true);

$("switch").onchange = async () => {
  await HC.call("/api/hc/web/switch", { id: Number($("switch").value) });
  window.location.reload();
};

// ------------------------------------------------------------------ load

async function refreshMe() {
  me = await HC.call("/api/hc/web/me");
  HC.csrf = me.csrf;
  $("title").textContent = me.congregation.display;
}

async function load() {
  const [b, u, a] = await Promise.all([HC.call("/api/hc/billing"), HC.call("/api/hc/usage?meetings=5&charges=50"),
                                       HC.call("/api/hc/account")]);
  bill = b;
  acct = a;
  renderOverview(u);
  renderBilling(u);
  renderEmails();
  renderDevices();
}

async function init() {
  try {
    me = await HC.call("/api/hc/web/me");
  } catch (e) {
    $("loading").textContent = e.message;
    return;
  }
  $("loading").hidden = true;
  if (!me.signed_in || !me.congregation) {
    me = null;
    $("login-view").hidden = false;
    $("email").focus();
    return;
  }
  HC.csrf = me.csrf;
  $("title").textContent = me.congregation.display;
  if (me.congregations.length > 1) {
    $("switch").innerHTML = me.congregations.map((c) => `<option value="${c.id}">${HC.esc(c.display)}</option>`).join("");
    $("switch").value = String(me.congregation.id);
    $("switch-box").hidden = false;
  }
  $("app-view").hidden = false;
  await load();
  renderCongregation();
  picker.show();
  const q = new URLSearchParams(location.search);
  if (q.get("paid")) { HC.say($("buy-msg"), "Thank you. If the bank approved the payment, the hours show here within a minute.", "good"); refreshSoon(); }
  if (q.get("card")) { HC.say($("buy-msg"), "Your card shows here within a minute.", "good"); refreshSoon(); }
  if (location.hash) document.querySelector(location.hash)?.scrollIntoView();
}

init();
