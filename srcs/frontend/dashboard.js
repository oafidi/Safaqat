const MATCHING_API = "/api/matches";
const AUTH_API = "/api/auth";
const token = sessionStorage.getItem("access_token");
const MOROCCO_TIME_ZONE = "Africa/Casablanca";
let enterprise = readJson(sessionStorage.getItem("enterprise"));
document.querySelectorAll(".hidden").forEach((element) => {
  // Responsive utilities (hidden sm:inline) stay class-driven; only JS-toggled nodes convert.
  if (/\b(sm|md|lg|xl):(inline|block|flex|grid|inline-flex|inline-block)\b/.test(element.className)) return;
  element.hidden = true;
  element.classList.remove("hidden");
});
if (!token || !enterprise) window.location.replace("/");

const elements = {
  list:document.querySelector("#offers-list"), loading:document.querySelector("#offers-loading"), empty:document.querySelector("#offers-empty"),
  count:document.querySelector("#offers-count"), message:document.querySelector("#dashboard-message"), toast:document.querySelector("#toast"),
  search:document.querySelector("#search-input"), category:document.querySelector("#category-filter"), sort:document.querySelector("#sort-select"),
  refresh:document.querySelector("#refresh-button"), profileModal:document.querySelector("#profile-modal"), profileForm:document.querySelector("#profile-form")
};
let currentOffers = [];
let activeTrigger = null;
let dismissed = new Set(readJson(localStorage.getItem("safaqat_dismissed")) || []);
let saved = new Set(readJson(localStorage.getItem("safaqat_saved")) || []);
const editTags = { keywords:[], locations:[] };

function readJson(value) { try { return JSON.parse(value); } catch (_) { return null; } }
function normalize(value) { return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }
function escapeHtml(value) { const span=document.createElement("span"); span.textContent=value??""; return span.innerHTML; }
function parseList(value) { return [...new Set(String(value||"").split(",").map((item)=>item.trim()).filter(Boolean))]; }

function logout() { sessionStorage.clear(); window.location.replace("/"); }
function persistOfferState() { localStorage.setItem("safaqat_saved",JSON.stringify([...saved])); localStorage.setItem("safaqat_dismissed",JSON.stringify([...dismissed])); }

function setMessage(text="") {
  elements.message.textContent=text;
  elements.message.className="notice notice-error mt-6";
  elements.message.hidden=!text;
}

let toastTimer;
function showToast(message, undoId=null) {
  clearTimeout(toastTimer);
  elements.toast.innerHTML="";
  const text=document.createElement("span"); text.textContent=message; elements.toast.append(text);
  if (undoId) { const button=document.createElement("button"); button.className="ml-3 font-medium underline underline-offset-4"; button.textContent="Annuler"; button.addEventListener("click",()=>{dismissed.delete(undoId);persistOfferState();renderOffers();elements.toast.hidden=true;}); elements.toast.append(button); }
  elements.toast.hidden=false;
  toastTimer=setTimeout(()=>elements.toast.hidden=true,5000);
}

function renderIdentity() {
  document.querySelector("#welcome-name").textContent=enterprise.enterprise_name;
  document.querySelector("#header-enterprise-name").textContent=enterprise.enterprise_name;
  document.querySelector("#header-enterprise-email").textContent=enterprise.email;
  const groups=[["profile-keywords",enterprise.keywords],["profile-categories",enterprise.categories],["profile-locations",enterprise.locations]];
  groups.forEach(([id,values])=>{ const node=document.querySelector(`#${id}`); node.innerHTML=(values||[]).map((value)=>`<span class="chip chip-neutral">${escapeHtml(value)}</span>`).join("")||'<span class="text-small text-ink-faint">Non renseigné</span>'; });
}

function translateReason(reason) {
  return String(reason||"")
    .replace(/^Matched keyword:/i,"Mot-clé correspondant :")
    .replace(/^Matched category:/i,"Catégorie correspondante :")
    .replace(/^Matched location:/i,"Zone correspondante :")
    .replace(/^Keyword match:/i,"Mot-clé correspondant :")
    .replace(/^Category match:/i,"Catégorie correspondante :")
    .replace(/^Location match:/i,"Zone correspondante :")
    .replace(/^Semantic similarity(?: with enterprise keywords)?$/i,"Activité sémantiquement proche")
    .replace(/^Hybrid relevance match$/i,"Correspondance combinée");
}

function deadlineInfo(raw) {
  const match=String(raw||"").match(/(\d{2})\/(\d{2})\/(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
  if (!match) return {timestamp:Number.MAX_SAFE_INTEGER,label:raw||"Non précisée",urgency:""};
  const date=dateInTimeZone(Number(match[3]),Number(match[2]),Number(match[1]),Number(match[4]||23),Number(match[5]||59));
  const days=Math.ceil((date-Date.now())/86400000);
  let urgency=days<=1?"Dernier jour":days<=30?`${days} jours restants`:"";
  const tone=days<=1?"chip-error":days<=7?"chip-warning":"chip-neutral";
  return {timestamp:date.getTime(),label:new Intl.DateTimeFormat("fr-MA",{timeZone:MOROCCO_TIME_ZONE,day:"numeric",month:"short",year:"numeric"}).format(date),urgency,tone};
}

function dateInTimeZone(year,month,day,hour,minute) {
  const guess=Date.UTC(year,month-1,day,hour,minute);
  const parts=new Intl.DateTimeFormat("en-CA",{timeZone:MOROCCO_TIME_ZONE,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(new Date(guess)).reduce((result,part)=>({...result,[part.type]:part.value}),{});
  const represented=Date.UTC(Number(parts.year),Number(parts.month)-1,Number(parts.day),Number(parts.hour),Number(parts.minute));
  return new Date(guess-(represented-guess));
}

function filteredOffers() {
  const query=normalize(elements.search.value);
  const category=elements.category.value;
  let offers=currentOffers.map((offer,index)=>({...offer,originalRank:index+1})).filter((offer)=>{
    if (dismissed.has(offer.offer_id)) return false;
    if (category && normalize(offer.category)!==category) return false;
    return !query || normalize(`${offer.objet} ${offer.buyer} ${offer.reference}`).includes(query);
  });
  if (elements.sort.value==="deadline") offers.sort((a,b)=>deadlineInfo(a.deadline).timestamp-deadlineInfo(b.deadline).timestamp);
  if (elements.sort.value==="saved") offers.sort((a,b)=>Number(saved.has(b.offer_id))-Number(saved.has(a.offer_id)) || a.originalRank-b.originalRank);
  return offers;
}

function offerCard(offer) {
  const deadline=deadlineInfo(offer.deadline);
  const reasons=(offer.reasons||[]).slice(0,3).map((reason)=>`<span class="chip chip-neutral">${escapeHtml(translateReason(reason))}</span>`).join("");
  const isSaved=saved.has(offer.offer_id);
  const rank=String(offer.originalRank).padStart(2,"0");
  return `<article class="offer-row" data-id="${escapeHtml(offer.offer_id)}">
    <div class="flex flex-col gap-9 lg:flex-row lg:items-start lg:justify-between">
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-3">
          <span class="numeric text-ink-faint">${rank}</span>
          <span class="chip chip-accent">${escapeHtml(offer.category||"Offre")}</span>
          ${offer.reference?`<span class="numeric text-ink-faint">Réf. ${escapeHtml(offer.reference)}</span>`:""}
        </div>
        <h3 class="mt-6 max-w-measure text-balance">${escapeHtml(offer.objet||"Objet non renseigné")}</h3>
        <p class="mt-3 max-w-measure text-ink-muted">${escapeHtml(offer.buyer||"Acheteur non renseigné")}</p>
        <div class="mt-6 flex flex-wrap gap-1.5">${reasons}</div>
      </div>
      <dl class="grid shrink-0 gap-6 border-t border-line pt-6 lg:w-64 lg:border-l lg:border-t-0 lg:pl-9 lg:pt-0">
        <div><dt class="label">Lieu</dt><dd class="mt-1.5 line-clamp-3 text-small" title="${escapeHtml(offer.location||"Non précisé")}">${escapeHtml(offer.location||"Non précisé")}</dd></div>
        <div><dt class="label">Date limite</dt><dd class="numeric mt-1.5">${escapeHtml(deadline.label)}</dd>${deadline.urgency?`<dd class="mt-1.5"><span class="chip ${deadline.tone}">${escapeHtml(deadline.urgency)}</span></dd>`:""}</div>
      </dl>
    </div>
    <div class="mt-9 flex flex-wrap items-center justify-end gap-3 border-t border-line pt-6">
      <button class="button-ghost" type="button" data-action="dismiss">Masquer</button>
      <button class="button-secondary" type="button" data-action="save" aria-pressed="${isSaved}"><svg class="size-4" viewBox="0 0 24 24" fill="${isSaved?"currentColor":"none"}" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M6 3h12v18l-6-4-6 4z"/></svg>${isSaved?"Enregistrée":"Enregistrer"}</button>
      ${offer.source_url?`<a class="button-secondary" href="${escapeHtml(offer.source_url)}" target="_blank" rel="noopener noreferrer">Source</a>`:""}
      <a class="button-primary" href="/offer.html?id=${encodeURIComponent(offer.offer_id)}">Voir l’offre</a>
    </div>
  </article>`;
}

function renderOffers() {
  const offers=filteredOffers();
  elements.list.innerHTML=offers.map(offerCard).join("");
  elements.empty.hidden=offers.length!==0;
  const total=currentOffers.length;
  elements.count.textContent=offers.length===total?`${total} offre${total===1?"":"s"} classée${total===1?"":"s"} par pertinence`:`${offers.length} offre${offers.length===1?"":"s"} affichée${offers.length===1?"":"s"} sur ${total}`;
  document.querySelector("#empty-copy").textContent=total?"Aucune offre ne correspond à vos filtres actuels.":"Élargissez vos critères de profil ou revenez après la prochaine mise à jour.";
}

async function loadOffers() {
  elements.loading.hidden=false; elements.list.innerHTML=""; elements.empty.hidden=true; setMessage();
  elements.refresh.disabled=true; document.querySelector("#refresh-icon").classList.add("animate-spin"); document.querySelector("#refresh-label").textContent="Actualisation…";
  try {
    const response=await fetch(`${MATCHING_API}/top?limit=10`,{headers:{Authorization:`Bearer ${token}`}});
    let body={}; try{body=await response.json();}catch(_){/* fallback */}
    if(response.status===401){logout();return;}
    if(!response.ok) throw new Error(body.detail||"Le classement est momentanément indisponible.");
    currentOffers=body.matches||[]; renderOffers();
  } catch(error) { setMessage(`${error.message} Vérifiez votre connexion puis réessayez.`); elements.count.textContent="Impossible de charger les offres"; }
  finally { elements.loading.hidden=true; elements.refresh.disabled=false; document.querySelector("#refresh-icon").classList.remove("animate-spin"); document.querySelector("#refresh-label").textContent="Actualiser"; }
}

function getOfferCard(button) { const card=button.closest("[data-id]"); return {card,offer:currentOffers.find((item)=>item.offer_id===card?.dataset.id)}; }
elements.list.addEventListener("click",(event)=>{
  const button=event.target.closest("[data-action]"); if(!button)return;
  const {card,offer}=getOfferCard(button); if(!offer)return;
  if(button.dataset.action==="save"){saved.has(offer.offer_id)?saved.delete(offer.offer_id):saved.add(offer.offer_id);persistOfferState();renderOffers();showToast(saved.has(offer.offer_id)?"Offre enregistrée.":"Offre retirée des favoris.");}
  if(button.dataset.action==="dismiss"){dismissed.add(offer.offer_id);persistOfferState();card.remove();renderOffers();showToast("Offre masquée de cette liste.",offer.offer_id);}
});

function focusable(modal) { return [...modal.querySelectorAll('button,[href],input,textarea,select,[tabindex]:not([tabindex="-1"])')].filter((item)=>!item.disabled&&!item.hidden); }
function openDialog(modal,trigger) { activeTrigger=trigger; modal.hidden=false; modal.classList.add("flex"); document.body.style.overflow="hidden"; focusable(modal)[0]?.focus(); }
function closeDialog(modal) { modal.hidden=true; modal.classList.remove("flex"); document.body.style.overflow=""; activeTrigger?.focus(); activeTrigger=null; }
function trapDialog(event,modal) { if(event.key!=="Tab")return; const list=focusable(modal); if(!list.length)return; const first=list[0],last=list.at(-1); if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();} }

function openProfile(trigger) {
  const form=elements.profileForm;
  ["enterprise_name","description","phone","legal_identifier"].forEach((name)=>form.elements[name].value=enterprise[name]||"");
  editTags.keywords=[...(enterprise.keywords||[])]; editTags.locations=[...(enterprise.locations||[])]; renderEditTags("keywords"); renderEditTags("locations");
  form.querySelectorAll('[name="categories"]').forEach((box)=>box.checked=(enterprise.categories||[]).includes(box.value));
  document.querySelector("#profile-message").hidden=true; openDialog(elements.profileModal,trigger);
}

function renderEditTags(kind) {
  const wrapper=document.querySelector(`#edit-${kind}-tags`); const input=document.querySelector(`#edit-${kind}-input`);
  wrapper.querySelectorAll("[data-edit-tag]").forEach((node)=>node.remove());
  editTags[kind].forEach((value,index)=>{const chip=document.createElement("span");chip.dataset.editTag="";chip.className="chip chip-accent";const label=document.createElement("span");label.textContent=value;const remove=document.createElement("button");remove.type="button";remove.className="chip-remove";remove.setAttribute("aria-label",`Supprimer ${value}`);remove.textContent="×";remove.addEventListener("click",()=>{editTags[kind].splice(index,1);renderEditTags(kind);input.focus();});chip.append(label,remove);wrapper.insertBefore(chip,input);});
}

function addEditTag(kind) {
  const input=document.querySelector(`#edit-${kind}-input`);const value=input.value.trim().replace(/,$/,"");
  if(value&&!editTags[kind].some((item)=>normalize(item)===normalize(value)))editTags[kind].push(value);
  input.value="";renderEditTags(kind);
}

["keywords","locations"].forEach((kind)=>{const input=document.querySelector(`#edit-${kind}-input`);input.addEventListener("keydown",(event)=>{if(event.key==="Enter"||event.key===","){event.preventDefault();addEditTag(kind);}});input.addEventListener("blur",()=>addEditTag(kind));});

elements.profileForm.addEventListener("submit",async(event)=>{
  event.preventDefault(); const data=new FormData(elements.profileForm); const button=document.querySelector("#profile-submit"); const profileMessage=document.querySelector("#profile-message");
  addEditTag("keywords"); addEditTag("locations");
  const payload={enterprise_name:data.get("enterprise_name").trim(),description:data.get("description").trim(),phone:data.get("phone").trim()||null,legal_identifier:data.get("legal_identifier").trim()||null,keywords:editTags.keywords,categories:data.getAll("categories"),locations:editTags.locations};
  if(!payload.keywords.length||!payload.categories.length||!payload.locations.length){profileMessage.textContent="Ajoutez au moins un mot-clé, une catégorie et une zone.";profileMessage.className="notice notice-error mt-6";profileMessage.hidden=false;return;}
  button.disabled=true;button.textContent="Enregistrement…";
  try { const response=await fetch(`${AUTH_API}/profile`,{method:"PATCH",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify(payload)}); let body={};try{body=await response.json();}catch(_){/* fallback */}if(response.status===401){logout();return;}if(!response.ok)throw new Error(body.detail||"La modification a échoué.");enterprise=body.enterprise;sessionStorage.setItem("enterprise",JSON.stringify(enterprise));renderIdentity();closeDialog(elements.profileModal);showToast("Profil mis à jour. Les offres vont être recalculées.");await loadOffers(); }
  catch(error){profileMessage.textContent=`${error.message} Vérifiez les champs puis réessayez.`;profileMessage.className="notice notice-error mt-6";profileMessage.hidden=false;}
  finally{button.disabled=false;button.textContent="Enregistrer et actualiser";}
});

elements.search.addEventListener("input",renderOffers); elements.category.addEventListener("change",renderOffers); elements.sort.addEventListener("change",renderOffers);
document.querySelector("#clear-filters-button").addEventListener("click",()=>{elements.search.value="";elements.category.value="";elements.sort.value="rank";renderOffers();elements.search.focus();});
document.querySelector("#logout-button").addEventListener("click",logout); elements.refresh.addEventListener("click",loadOffers);
[document.querySelector("#edit-profile-button"),document.querySelector("#empty-edit-button")].forEach((button)=>button.addEventListener("click",()=>openProfile(button)));
document.querySelector("#profile-close").addEventListener("click",()=>closeDialog(elements.profileModal)); document.querySelector("#profile-cancel").addEventListener("click",()=>closeDialog(elements.profileModal));
elements.profileModal.addEventListener("mousedown",(event)=>{if(event.target===elements.profileModal)closeDialog(elements.profileModal);}); elements.profileModal.addEventListener("keydown",(event)=>{if(event.key==="Escape")closeDialog(elements.profileModal);trapDialog(event,elements.profileModal);});

if(token&&enterprise){renderIdentity();loadOffers();}
