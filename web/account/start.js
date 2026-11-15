// hearchurch.com/start: sign up, prove the e-mail by a code, maybe save a card, then "download the app and sign in".
"use strict";

const $ = (id) => document.getElementById(id);
const views = { form: $("form-view"), code: $("code-view"), done: $("done-view") };
function view(name) {
  for (const [k, el] of Object.entries(views)) el.hidden = k !== name;
  window.scrollTo(0, 0);
  if (name === "form") picker.show();
}

// ---- church, name
const church = $("church");
church.insertAdjacentHTML("beforeend",
  HC.CHURCHES.map((c) => `<option>${HC.esc(c)}</option>`).join("") + '<option value="other">Other</option>');
function churchChanged() {
  const lds = church.value === HC.LDS;
  $("other-box").hidden = church.value !== "other";
  $("lds-box").hidden = $("stake-box").hidden = !lds;
  $("name-label").textContent = lds ? "Ward or branch name" : "Congregation name";
  $("name").placeholder = lds ? "e.g. Maple Grove Ward" : "As members know it";
}
church.onchange = churchChanged;

// ---- meeting times, address
const times = HC.timesEditor($("times-box"));
times.set([{ day: "sun", time: "10:00" }]);
const picker = HC.addressPicker($("place-box"));

// ---- e-mails
$("add-email").onclick = () => {
  const n = $("more-emails").children.length;
  if (n >= 9) return;
  $("more-emails").insertAdjacentHTML("beforeend",
    `<div class="row"><input type="email" class="more" maxlength="120" aria-label="Another e-mail" placeholder="another@example.org"></div>`);
  $("more-emails").lastElementChild.querySelector("input").focus();
};

// ---- code
function codeText(r) {
  // The hub's own-key message names the provider; say it plainly here instead.
  return r.type === "own_key" ? "Code accepted: your congregation uses its own translation key. You enter it in the Mac app's AI tab." : r.message;
}
$("check-promo").onclick = async () => {
  const code = $("promo").value.trim();
  if (!code) return HC.say($("promo-msg"), "");
  try {
    const r = await HC.call("/api/hc/codes/check", { code });
    HC.say($("promo-msg"), codeText(r), "good");
    $("card-box").hidden = r.type === "own_key";
  } catch (e) {
    HC.say($("promo-msg"), e.message, "bad");
  }
};

// ---- the sign-up
let body = null;

function collect() {
  const lds = church.value === HC.LDS;
  const name = $("name").value.trim();
  const churchName = church.value === "other" ? $("church-other").value.trim() : church.value;
  const email = $("email").value.trim().toLowerCase();
  const more = [...document.querySelectorAll("input.more")].map((i) => i.value.trim().toLowerCase()).filter(Boolean);
  if (!churchName) return "Choose your church.";
  if (name.length < 2) return "Enter the congregation's name.";
  if (!HC.isEmail(email)) return "Enter your e-mail address.";
  const bad = more.find((m) => !HC.isEmail(m));
  if (bad) return `"${bad}" isn't an e-mail address.`;
  const p = picker.state;
  let tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* none */ }
  body = {
    church: churchName, name, email,
    unit_type: lds ? document.querySelector('input[name="unit"]:checked').value : "congregation",
    stake: lds ? ($("stake").value.trim() || null) : null,
    address: (p.address || "").trim(), lat: p.lat, lng: p.lng,
    meeting_times: times.get(), timezone: tz,
    emails: [...new Set(more.filter((m) => m !== email))],
    code: $("promo").value.trim(),
  };
  return null;
}

async function sendCode() {
  await HC.call("/api/hc/web/signup-code", { email: body.email });
}

$("signup").onsubmit = async (ev) => {
  ev.preventDefault();
  const problem = collect();
  if (problem) return HC.say($("form-msg"), problem, "bad");
  $("continue").disabled = true;
  HC.say($("form-msg"), "");
  try {
    if (body.code) {   // a wrong code is told now, not after the e-mail step
      try { await HC.call("/api/hc/codes/check", { code: body.code }); } catch (e) {
        HC.say($("promo-msg"), e.message, "bad");
        throw new Error("Check the code, or leave it empty.");
      }
    }
    await sendCode();
    $("code-email").textContent = body.email;
    $("email-code").value = "";
    HC.say($("code-msg"), "");
    view("code");
    $("email-code").focus();
  } catch (e) {
    HC.say($("form-msg"), e.message, "bad");
  } finally {
    $("continue").disabled = false;
  }
};

$("resend").onclick = async () => {
  try {
    await sendCode();
    HC.say($("code-msg"), "A new code is on its way.", "good");
  } catch (e) {
    HC.say($("code-msg"), e.message, "bad");
  }
};
$("back").onclick = () => view("form");

async function addCard() {
  const r = await HC.call("/api/hc/card/setup?back=start", {});
  window.location.href = r.url;
}

$("code-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const code = $("email-code").value.replace(/\D/g, "");
  if (code.length !== 6) return HC.say($("code-msg"), "Enter the 6 digits from the e-mail.", "bad");
  $("create").disabled = true;
  try {
    const r = await HC.call("/api/hc/web/signup", { ...body, email_code: code });
    HC.csrf = r.csrf;
    if ($("add-card").checked && !$("card-box").hidden && r.can_save_card) {
      try { return await addCard(); } catch (e) { /* the done page offers it again */ }
    }
    window.location.href = "/start/done";
  } catch (e) {
    HC.say($("code-msg"), e.message, "bad");
    $("create").disabled = false;
  }
};

// ---- the last page: /start/done (also where Stripe's card page comes back to)
async function done() {
  let me;
  try { me = await HC.call("/api/hc/web/me"); } catch (e) { me = { signed_in: false }; }
  if (!me.signed_in || !me.congregation) { window.location.replace("/account"); return; }
  HC.csrf = me.csrf;
  $("done-email").textContent = me.email;
  const link = `${location.origin}/${me.congregation.slug}`;
  $("done-link").href = link;
  $("done-link").textContent = link.replace(/^https?:\/\//, "");
  view("done");
  if (me.congregation.billing !== "credits") return;
  try {
    const b = await HC.call("/api/hc/billing");
    if (b.card) HC.say($("card-note"), `Card saved: ${b.card.brand} ••${b.card.last4}.`, "good");
    else if (b.can_save_card) $("card-offer").hidden = false;
  } catch (e) { /* billing shows under /account */ }
}
$("card-now").onclick = () => addCard().catch((e) => HC.say($("card-note"), e.message, "bad"));

churchChanged();
if (location.pathname === "/start/done") done(); else view("form");
