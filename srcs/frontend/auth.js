const AUTH_API = "/api/auth";
const signinForm = document.querySelector("#signin-form");
const signupForm = document.querySelector("#signup-form");
const signinTab = document.querySelector("#signin-tab");
const signupTab = document.querySelector("#signup-tab");
const formMessage = document.querySelector("#form-message");
const steps = [...document.querySelectorAll(".signup-step")];
const stepNames = ["Votre compte", "Votre activité", "Vos critères", "Vérification"];
let currentStep = 0;
const tags = { keywords: [], locations: [] };

document.querySelectorAll(".hidden").forEach((element) => {
  // Responsive utilities (hidden sm:inline) stay class-driven; only JS-toggled nodes convert.
  if (/\b(sm|md|lg|xl):(inline|block|flex|grid|inline-flex|inline-block)\b/.test(element.className)) return;
  element.hidden = true;
  element.classList.remove("hidden");
});

if (sessionStorage.getItem("access_token") && sessionStorage.getItem("enterprise")) window.location.replace("/dashboard.html");

function setMessage(text = "", type = "error") {
  formMessage.textContent = text;
  formMessage.className = `notice mb-6 ${type === "success" ? "notice-success" : "notice-error"}`;
  formMessage.hidden = !text;
}

function fieldError(field, message = "") {
  const error = field.closest("label")?.querySelector(".field-error");
  field.setAttribute("aria-invalid", message ? "true" : "false");
  if (error) { error.textContent = message; error.hidden = !message; }
}

function validateFields(container) {
  let valid = true;
  container.querySelectorAll("input[required], textarea[required]").forEach((field) => {
    let message = "";
    if (!field.value.trim()) message = "Ce champ est obligatoire.";
    else if (field.type === "email" && !field.validity.valid) message = "Saisissez une adresse e-mail valide.";
    else if (field.minLength > 0 && field.value.length < field.minLength) message = `Utilisez au moins ${field.minLength} caractères.`;
    if (field.id === "password-confirm" && field.value !== document.querySelector("#signup-password").value) message = "Les mots de passe ne correspondent pas.";
    fieldError(field, message);
    if (message) valid = false;
  });
  if (!valid) container.querySelector('[aria-invalid="true"]')?.focus();
  return valid;
}

function setSubmitting(form, busy) {
  const button = form.querySelector(".submit-button");
  button.disabled = busy;
  button.dataset.label ||= button.textContent.trim();
  button.textContent = busy ? "Connexion en cours…" : button.dataset.label;
}

async function authRequest(path, payload) {
  const response = await fetch(`${AUTH_API}/${path}`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
  let body = {};
  try { body = await response.json(); } catch (_) { /* Fallback below. */ }
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join(". ") : body.detail;
    const translations = { "Email or password is incorrect":"Adresse e-mail ou mot de passe incorrect.", "An enterprise with this email already exists":"Une entreprise utilise déjà cette adresse e-mail." };
    throw new Error(translations[detail] || detail || "Le service est momentanément indisponible. Réessayez dans quelques instants.");
  }
  return body;
}

function saveSession(data) {
  sessionStorage.removeItem("signup_draft");
  sessionStorage.setItem("access_token", data.access_token);
  sessionStorage.setItem("enterprise", JSON.stringify(data.enterprise));
  window.location.assign("/dashboard.html");
}

function toggleAuth(mode) {
  const signin = mode === "signin";
  signinForm.hidden = !signin;
  signupForm.hidden = signin;
  signinTab.setAttribute("aria-selected", String(signin));
  signupTab.setAttribute("aria-selected", String(!signin));
  signinTab.className = "chip-filter";
  signupTab.className = "chip-filter";
  document.querySelector("#auth-side-title").textContent = signin ? "Vos prochains marchés, déjà triés." : "Construisez une veille qui vous ressemble.";
  document.querySelector("#auth-side-copy").textContent = signin ? "Connectez-vous pour retrouver les opportunités actives qui correspondent à votre activité." : "En quatre étapes courtes, indiquez votre métier et les marchés qui comptent pour vous.";
  setMessage();
  (signin ? signinForm : signupForm).querySelector("input")?.focus();
}

function renderTags(kind) {
  const wrapper = document.querySelector(`#${kind}-tags`);
  const input = document.querySelector(`#${kind}-input`);
  wrapper.querySelectorAll("[data-tag]").forEach((node) => node.remove());
  tags[kind].forEach((value, index) => {
    const chip = document.createElement("span");
    chip.dataset.tag = "";
    chip.className = "chip chip-accent";
    chip.innerHTML = `<span></span><button type="button" class="chip-remove" aria-label="Supprimer ${value}">×</button>`;
    chip.querySelector("span").textContent = value;
    chip.querySelector("button").addEventListener("click", () => { tags[kind].splice(index,1); renderTags(kind); saveDraft(); input.focus(); });
    wrapper.insertBefore(chip, input);
  });
}

function addTag(kind) {
  const input = document.querySelector(`#${kind}-input`);
  const value = input.value.trim().replace(/,$/, "");
  if (value && !tags[kind].some((item) => item.toLocaleLowerCase("fr") === value.toLocaleLowerCase("fr"))) tags[kind].push(value);
  input.value = "";
  renderTags(kind);
  saveDraft();
}

["keywords","locations"].forEach((kind) => {
  const input = document.querySelector(`#${kind}-input`);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === ",") { event.preventDefault(); addTag(kind); } });
  input.addEventListener("blur", () => addTag(kind));
});

function validatePreferences() {
  const categories = new FormData(signupForm).getAll("categories");
  const checks = [["keywords", tags.keywords.length, "Ajoutez au moins un mot-clé."], ["locations", tags.locations.length, "Ajoutez au moins une ville ou zone."]];
  let valid = true;
  checks.forEach(([id, count, message]) => { const el=document.querySelector(`#${id}-error`); el.textContent=count?"":message; el.hidden=Boolean(count); if(!count) valid=false; });
  const categoryError = document.querySelector("#categories-error");
  categoryError.textContent = categories.length ? "" : "Choisissez au moins une catégorie.";
  categoryError.hidden = Boolean(categories.length);
  return valid && Boolean(categories.length);
}

function updateStep() {
  steps.forEach((step,index) => step.hidden = index !== currentStep);
  document.querySelector("#step-label").textContent = `Étape ${currentStep+1} sur 4 · ${stepNames[currentStep]}`;
  document.querySelector("#step-percent").textContent = `${(currentStep+1)*25} %`;
  document.querySelector("#signup-progress").style.width = `${(currentStep+1)*25}%`;
  const progress = document.querySelector('[role="progressbar"]');
  progress.setAttribute("aria-valuenow", String(currentStep+1));
  document.querySelector("#signup-back").hidden = currentStep === 0;
  document.querySelector("#signup-next").hidden = currentStep === 3;
  document.querySelector("#signup-submit").hidden = currentStep !== 3;
  if (currentStep === 3) renderReview();
  document.querySelector("#step-label").focus?.();
}

function renderReview() {
  const data = new FormData(signupForm);
  const items = [["Entreprise",data.get("enterprise_name")],["E-mail",data.get("email")],["Activité",data.get("description")],["Mots-clés",tags.keywords.join(", ")],["Catégories",data.getAll("categories").join(", ")],["Zones",tags.locations.join(", ")]];
  document.querySelector("#signup-review").innerHTML = items.map(([label,value]) => `<div><p class="label">${label}</p><p class="mt-1.5 break-words text-small"></p></div>`).join("");
  [...document.querySelector("#signup-review").children].forEach((node,index) => node.lastElementChild.textContent = items[index][1]);
}

function saveDraft() {
  const data = Object.fromEntries(new FormData(signupForm));
  delete data.password; delete data.password_confirm;
  sessionStorage.setItem("signup_draft", JSON.stringify({ data, tags, categories:new FormData(signupForm).getAll("categories") }));
}

function restoreDraft() {
  try {
    const draft = JSON.parse(sessionStorage.getItem("signup_draft"));
    if (!draft) return;
    Object.entries(draft.data || {}).forEach(([name,value]) => { const field=signupForm.elements.namedItem(name); if(field && typeof field.value !== "undefined") field.value=value; });
    tags.keywords = draft.tags?.keywords || []; tags.locations = draft.tags?.locations || [];
    (draft.categories || []).forEach((value) => { const box=signupForm.querySelector(`[name="categories"][value="${value}"]`); if(box) box.checked=true; });
    renderTags("keywords"); renderTags("locations");
  } catch (_) { sessionStorage.removeItem("signup_draft"); }
}

signinForm.addEventListener("submit", async (event) => {
  event.preventDefault(); setMessage();
  if (!validateFields(signinForm)) return;
  setSubmitting(signinForm,true);
  const data = new FormData(signinForm);
  try { saveSession(await authRequest("login", {email:data.get("email"),password:data.get("password")})); }
  catch (error) { setMessage(error.message); setSubmitting(signinForm,false); }
});

document.querySelector("#signup-next").addEventListener("click", () => {
  if ((currentStep < 2 && !validateFields(steps[currentStep])) || (currentStep === 2 && !validatePreferences())) return;
  currentStep += 1; updateStep(); saveDraft();
});
document.querySelector("#signup-back").addEventListener("click", () => { currentStep -= 1; updateStep(); });
signupForm.addEventListener("input", saveDraft);
signupForm.addEventListener("submit", async (event) => {
  event.preventDefault(); setMessage();
  const data = new FormData(signupForm);
  const payload = { email:data.get("email"), password:data.get("password"), enterprise_name:data.get("enterprise_name"), phone:data.get("phone")||null, legal_identifier:data.get("legal_identifier")||null, description:data.get("description"), keywords:tags.keywords, categories:data.getAll("categories"), locations:tags.locations };
  setSubmitting(signupForm,true);
  try { saveSession(await authRequest("signup",payload)); }
  catch (error) { setMessage(error.message); setSubmitting(signupForm,false); }
});

signinTab.addEventListener("click", () => toggleAuth("signin"));
signupTab.addEventListener("click", () => toggleAuth("signup"));
document.querySelector('[role="tablist"]').addEventListener("keydown", (event) => { if (["ArrowLeft","ArrowRight"].includes(event.key)) { event.preventDefault(); toggleAuth(document.activeElement===signinTab?"signup":"signin"); } });
restoreDraft(); updateStep();
