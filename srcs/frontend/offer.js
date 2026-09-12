const OFFERS_API = "/api/offers";
const AUTH_API = "/api/auth";
const token = sessionStorage.getItem("access_token");
let enterprise = readJson(sessionStorage.getItem("enterprise"));
const offerId = new URLSearchParams(window.location.search).get("id");
let viewerUrl = null;
let viewerTrigger = null;
const MOROCCO_TIME_ZONE = "Africa/Casablanca";

document.querySelectorAll(".hidden").forEach((element) => {
  // Responsive utilities (hidden sm:inline) stay class-driven; only JS-toggled nodes convert.
  if (/\b(sm|md|lg|xl):(inline|block|flex|grid|inline-flex|inline-block)\b/.test(element.className)) return;
  element.hidden = true;
  element.classList.remove("hidden");
});

if (!token || !enterprise) window.location.replace("/");

const elements = {
  loading: document.querySelector("#offer-loading"),
  content: document.querySelector("#offer-content"),
  error: document.querySelector("#offer-error"),
  errorCopy: document.querySelector("#offer-error-copy"),
  documents: document.querySelector("#documents-list"),
  documentsEmpty: document.querySelector("#documents-empty"),
  documentsCount: document.querySelector("#documents-count"),
  documentsMessage: document.querySelector("#documents-message"),
  viewer: document.querySelector("#document-viewer"),
  frame: document.querySelector("#pdf-frame"),
  consent: document.querySelector("#portal-consent"),
  consentCheckbox: document.querySelector("#portal-consent-checkbox"),
  consentButton: document.querySelector("#portal-consent-button"),
};

function readJson(value) {
  try { return JSON.parse(value); } catch (_) { return null; }
}

function logout() {
  sessionStorage.clear();
  window.location.replace("/");
}

function deadlineInfo(raw) {
  const match = String(raw || "").match(/(\d{2})\/(\d{2})\/(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
  if (!match) return { label: raw || "Non précisée", urgency: "" };
  const date = dateInTimeZone(Number(match[3]), Number(match[2]), Number(match[1]), Number(match[4] || 23), Number(match[5] || 59));
  const days = Math.ceil((date - Date.now()) / 86400000);
  const label = new Intl.DateTimeFormat("fr-MA", { timeZone:MOROCCO_TIME_ZONE, day:"numeric", month:"long", year:"numeric", hour:"2-digit", minute:"2-digit" }).format(date);
  const urgency = days <= 1 ? "Dernier jour" : days <= 7 ? `${days} jours restants` : days <= 30 ? `${days} jours restants` : "Offre active";
  return { label, urgency };
}

function dateInTimeZone(year, month, day, hour, minute) {
  const guess = Date.UTC(year, month - 1, day, hour, minute);
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone:MOROCCO_TIME_ZONE, year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23" })
    .formatToParts(new Date(guess)).reduce((result, part) => ({ ...result, [part.type]:part.value }), {});
  const represented = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute));
  return new Date(guess - (represented - guess));
}

function localizedError(status, fallback) {
  if (status === 404) return "Cette offre ou ce document n’est plus disponible.";
  if (status === 413) return "Ce document est trop volumineux pour être ouvert ici.";
  if (status === 415) return "Ce type de document doit être consulté sur le portail officiel.";
  if (status >= 500) return "Le service est momentanément indisponible.";
  return fallback;
}

function portalFallbackMessage(message) {
  return `${message || "Le dossier ne peut pas être préparé automatiquement."} Utilisez « Télécharger sur le portail » pour continuer.`;
}

function setDocumentMessage(message = "", type = "info") {
  elements.documentsMessage.textContent = message;
  elements.documentsMessage.className = `notice mt-6 ${type === "error" ? "notice-error" : "notice-warning"}`;
  elements.documentsMessage.hidden = !message;
}

function metadataItem(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.className = "label";
  description.className = "mt-1.5 text-small";
  term.textContent = label;
  description.textContent = value || "Non renseigné";
  wrapper.append(term, description);
  return wrapper;
}

function documentIcon(type) {
  const wrapper = document.createElement("span");
  wrapper.className = "grid size-9 shrink-0 place-items-center rounded-sm bg-canvas-sunken text-accent-hover";
  wrapper.innerHTML = type === "pdf"
    ? '<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M6 2h8l4 4v16H6zM14 2v5h5M9 14h6M9 18h4"/></svg>'
    : '<svg class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 7h6l2 2h8v11H4zM4 7V4h6l2 3"/></svg>';
  return wrapper;
}

function fileTypeLabel(item) {
  if (item.requires_portal) return "Dossier protégé — téléchargement officiel";
  if (item.media_type === "application/pdf") return `PDF · ${formatSize(item.size)}`;
  if (item.media_type?.startsWith("image/")) return `Image · ${formatSize(item.size)}`;
  if (item.media_type?.startsWith("text/")) return `Texte · ${formatSize(item.size)}`;
  if (item.file_type === "dossier") return "Dossier de consultation";
  if (item.file_type === "notice") return "Avis de publicité";
  if (item.size !== undefined) {
    const extension = String(item.name || "").split(".").pop();
    return `${(extension && extension !== item.name ? extension : "Fichier").toUpperCase()} · ${formatSize(item.size)}`;
  }
  return `${item.file_type || item.media_type || "Document"}`.toUpperCase();
}

function viewableType(contentType) {
  return contentType === "application/pdf"
    || contentType === "text/plain"
    || contentType.startsWith("image/");
}

function formatSize(size) {
  if (!Number.isFinite(Number(size))) return "";
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} Ko`;
  return `${(size / (1024 * 1024)).toFixed(1)} Mo`;
}

function renderDocuments(documents, extracted = false) {
  elements.documents.innerHTML = "";
  elements.documentsCount.textContent = `${documents.length} pièce${documents.length === 1 ? "" : "s"}`;
  elements.documentsEmpty.hidden = documents.length !== 0;
  elements.documents.hidden = documents.length === 0;

  documents.forEach((item) => {
    const row = document.createElement("article");
    row.className = "list-row flex-col items-start gap-3 sm:flex-row sm:items-center sm:gap-6";
    const identity = window.document.createElement("div");
    identity.className = "flex min-w-0 flex-1 items-center gap-3";
    const copy = window.document.createElement("div");
    copy.className = "min-w-0";
    const title = window.document.createElement("h3");
    title.className = "break-words text-small font-medium leading-6";
    title.textContent = item.name;
    const type = window.document.createElement("p");
    type.className = "numeric mt-0.5 text-ink-faint";
    type.textContent = fileTypeLabel(item);
    copy.append(title, type);
    identity.append(documentIcon(item.media_type === "application/pdf" ? "pdf" : item.file_type), copy);

    const actions = window.document.createElement("div");
    actions.className = "flex shrink-0 flex-wrap gap-3 sm:justify-end";
    if (extracted) {
      const open = window.document.createElement("button");
      open.type = "button";
      open.className = "button-secondary";
      open.textContent = "Voir le contenu";
      open.addEventListener("click", () => openDocument(item, open, true));
      actions.append(open);
    } else if (item.requires_portal && item.portal_url) {
      const download = window.document.createElement("a");
      download.className = "button-secondary";
      download.href = item.portal_url;
      download.target = "_blank";
      download.rel = "noopener noreferrer";
      download.textContent = "Télécharger sur le portail";
      actions.append(download);
    } else {
      const open = window.document.createElement("button");
      open.type = "button";
      open.className = "button-secondary";
      open.textContent = "Ouvrir";
      open.addEventListener("click", () => openDocument(item, open));
      actions.append(open);
    }
    if (item.portal_url && !item.requires_portal) {
      const portal = window.document.createElement("a");
      portal.className = "button-ghost";
      portal.href = item.portal_url;
      portal.target = "_blank";
      portal.rel = "noopener noreferrer";
      portal.textContent = "Portail officiel";
      actions.append(portal);
    }
    row.append(identity, actions);
    elements.documents.append(row);
  });
}

async function openDocument(item, button, extracted = false) {
  setDocumentMessage();
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "Ouverture…";
  try {
    const resource = extracted ? "files" : "documents";
    const response = await fetch(`${OFFERS_API}/${encodeURIComponent(offerId)}/${resource}/${item.id}/content`, {
      headers: { Authorization:`Bearer ${token}` },
    });
    if (response.status === 401) { logout(); return; }
    if (response.status === 409) {
      setDocumentMessage("Le portail officiel demande vos coordonnées et votre accord avant de fournir ce dossier. Utilisez « Portail officiel » pour continuer.");
      return;
    }
    if (!response.ok) {
      throw new Error(localizedError(response.status, "Le document ne peut pas être ouvert pour le moment."));
    }

    const blob = await response.blob();
    const contentType = (response.headers.get("content-type") || blob.type || "").split(";")[0].trim().toLowerCase();
    if (viewableType(contentType)) {
      if (viewerUrl) URL.revokeObjectURL(viewerUrl);
      // The frame renders same-origin, so the preview is pinned to the type we
      // recognized instead of whatever the archive claimed the file was.
      viewerUrl = URL.createObjectURL(new Blob([blob], { type: contentType }));
      elements.frame.src = viewerUrl;
      document.querySelector("#viewer-title").textContent = item.name;
      elements.viewer.hidden = false;
      viewerTrigger = button;
      elements.viewer.scrollIntoView({ behavior:"smooth", block:"start" });
      document.querySelector("#close-viewer").focus({ preventScroll:true });
    } else {
      const downloadUrl = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = downloadUrl;
      link.download = item.name || `document-${item.id}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
    }
  } catch (error) {
    setDocumentMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function renderOffer(offer) {
  const deadline = deadlineInfo(offer.deadline);
  document.title = `${offer.reference || "Offre"} — Safaqat`;
  document.querySelector("#offer-category").textContent = offer.category || "Offre";
  document.querySelector("#offer-reference").textContent = offer.reference ? `Réf. ${offer.reference}` : "Référence non renseignée";
  document.querySelector("#offer-title").textContent = offer.object || "Objet non renseigné";
  document.querySelector("#offer-buyer").textContent = offer.buyer || "Acheteur non renseigné";
  document.querySelector("#offer-deadline").textContent = deadline.label;
  document.querySelector("#offer-urgency").textContent = deadline.urgency;
  const official = document.querySelector("#official-offer-link");
  official.href = offer.source_url || "#";
  official.hidden = !offer.source_url;

  const metadata = document.querySelector("#offer-metadata");
  metadata.innerHTML = "";
  [
    ["Acheteur public", offer.buyer],
    ["Lieu d’exécution", offer.location],
    ["Procédure", offer.procedure],
    ["Catégorie", offer.category],
    ["Date de publication", offer.publication_date],
    ["Référence", offer.reference],
  ].forEach(([label, value]) => metadata.append(metadataItem(label, value)));
}

async function loadDocuments() {
  elements.documentsCount.textContent = "Préparation des pièces…";
  elements.documents.setAttribute("aria-busy", "true");
  setDocumentMessage();
  try {
    const response = await fetch(`${OFFERS_API}/${encodeURIComponent(offerId)}/documents`, {
      headers: { Authorization:`Bearer ${token}` },
    });
    if (response.status === 401) { logout(); return; }
    if (response.status === 409) {
      setTimeout(loadDocuments, 3000);
      return;
    }
    if (!response.ok) throw new Error(localizedError(response.status, "Les documents ne peuvent pas être chargés pour le moment."));
    const body = await response.json();
    elements.consent.hidden = !body.consent_required;
    renderDocuments(body.files?.length ? body.files : (body.documents || []), Boolean(body.files?.length));
    if (body.preparation_error) {
      setDocumentMessage(portalFallbackMessage(body.preparation_error), "error");
    }
  } catch (error) {
    setDocumentMessage(`${error.message} Vous pouvez encore consulter l’annonce officielle.`, "error");
  } finally {
    elements.documents.removeAttribute("aria-busy");
  }
}

async function acceptPortalConsent() {
  if (!elements.consentCheckbox.checked) return;
  elements.consentButton.disabled = true;
  elements.consentButton.textContent = "Enregistrement…";
  try {
    const response = await fetch(`${AUTH_API}/portal-consent`, {
      method:"POST",
      headers:{ "Content-Type":"application/json", Authorization:`Bearer ${token}` },
      body:JSON.stringify({ accepted:true }),
    });
    if (response.status === 401) { logout(); return; }
    if (!response.ok) throw new Error("Le consentement n’a pas pu être enregistré.");
    const body = await response.json();
    enterprise = body.enterprise;
    sessionStorage.setItem("enterprise", JSON.stringify(enterprise));
    elements.consent.hidden = true;
    setDocumentMessage("Téléchargement et extraction du dossier en cours…");
    await loadDocuments();
  } catch (error) {
    setDocumentMessage(`${error.message} Réessayez dans un instant.`, "error");
  } finally {
    elements.consentButton.textContent = "Accepter et préparer les documents";
    elements.consentButton.disabled = !elements.consentCheckbox.checked;
  }
}

async function loadOffer() {
  elements.loading.hidden = false;
  elements.content.hidden = true;
  elements.error.hidden = true;
  if (!offerId) {
    elements.loading.hidden = true;
    elements.errorCopy.textContent = "Aucun identifiant d’offre n’a été fourni.";
    elements.error.hidden = false;
    return;
  }
  try {
    const response = await fetch(`${OFFERS_API}/${encodeURIComponent(offerId)}`, {
      headers: { Authorization:`Bearer ${token}` },
    });
    let body = {};
    try { body = await response.json(); } catch (_) { /* Generic message below. */ }
    if (response.status === 401) { logout(); return; }
    if (!response.ok) throw new Error(localizedError(response.status, "L’offre est indisponible ou sa date limite est dépassée."));
    renderOffer(body);
    elements.content.hidden = false;
    loadDocuments();
  } catch (error) {
    elements.errorCopy.textContent = `${error.message} Revenez au tableau de bord ou réessayez.`;
    elements.error.hidden = false;
  } finally {
    elements.loading.hidden = true;
  }
}

document.querySelector("#header-enterprise-name").textContent = enterprise?.enterprise_name || "";
document.querySelector("#header-enterprise-email").textContent = enterprise?.email || "";
document.querySelector("#logout-button").addEventListener("click", logout);
document.querySelector("#retry-button").addEventListener("click", loadOffer);
elements.consentCheckbox.addEventListener("change", () => {
  elements.consentButton.disabled = !elements.consentCheckbox.checked;
});
elements.consentButton.addEventListener("click", acceptPortalConsent);
document.querySelector("#close-viewer").addEventListener("click", () => {
  elements.viewer.hidden = true;
  elements.frame.src = "about:blank";
  if (viewerUrl) URL.revokeObjectURL(viewerUrl);
  viewerUrl = null;
  viewerTrigger?.focus();
  viewerTrigger = null;
});
window.addEventListener("pagehide", () => { if (viewerUrl) URL.revokeObjectURL(viewerUrl); });

loadOffer();
